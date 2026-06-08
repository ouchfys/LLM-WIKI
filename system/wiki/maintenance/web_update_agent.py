from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.storage import get_object_storage, get_storage_layout
from system.search.web_fetch import WebFetchTool
from system.search.web_search import WebSearchResult, WebSearchTool
from system.wiki.maintenance.candidates import MaintenanceCandidateStore
from system.wiki.paper_pipeline.distiller import parse_json_object
from system.wiki.wiki_builder import sanitize_wiki_text
from system.wiki.wiki_store import WikiStore


WEB_UPDATE_PROMPT = """\
You are the Web Update Agent for a self-maintained LLM research wiki.
Return strict JSON only. Do not write markdown.

Your job:
- Review web_search and optional web_fetch observations.
- Decide which web findings deserve to become source candidates.
- Do not update official wiki pages directly.
- Prefer primary sources: papers, repos, benchmark pages, official docs.
- Reject shallow SEO pages, inaccessible pages, duplicates, or weak evidence.

Allowed source_type values:
- paper
- repo
- blog
- benchmark
- documentation

Allowed expected_wiki_impact values:
- new_card
- update_card
- background_only

Return this shape:
{
  "topic": "",
  "candidates": [
    {
      "source_type": "paper",
      "title": "",
      "url": "",
      "why_relevant": "",
      "expected_wiki_impact": "new_card",
      "confidence": "high"
    }
  ],
  "rejected": [
    {
      "url": "",
      "reason": ""
    }
  ]
}

Input:
{payload}
"""


class WebUpdateAgent:
    """Discover web source candidates without mutating official wiki pages."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        llm: Any = None,
        web_search: WebSearchTool | None = None,
        web_fetch: WebFetchTool | None = None,
    ):
        repo_root = Path(__file__).resolve().parents[3]
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self.llm = llm
        self.web_search = web_search or WebSearchTool()
        self.web_fetch = web_fetch or WebFetchTool()
        self.wiki_store = WikiStore(db_path=self.db_path)
        self.candidates = MaintenanceCandidateStore(db_path=self.db_path)
        self.layout = get_storage_layout()
        self.storage = get_object_storage()

    def discover(
        self,
        *,
        topic: str,
        limit: int = 5,
        fetch_top: int = 2,
        upload: bool = True,
    ) -> dict[str, Any]:
        topic = sanitize_wiki_text(topic).strip()
        if not topic:
            return {"ok": False, "error": "topic is required", "items": []}
        search_results = self.web_search.search(topic, limit=limit)
        fetch_results = []
        for result in search_results[: max(0, fetch_top)]:
            fetched = self.web_fetch.fetch(result.url, query=topic)
            fetch_results.append(_fetch_result_dict(fetched))
        context = {
            "topic": topic,
            "existing_wiki_cards": self._existing_cards(topic),
            "web_search_results": [_search_result_dict(item) for item in search_results],
            "web_fetch_results": fetch_results,
        }
        decision = self._decide(context) if self.llm else self._deterministic_decision(topic, search_results)
        normalized = self._normalize_decision(decision, topic)
        staged = []
        for item in normalized.get("candidates", []):
            title = item.get("title") or item.get("url") or topic
            source_candidate_uri = self._write_source_candidate(
                topic=topic,
                item=item,
                context=context,
                upload=upload,
            )
            item["source_candidate_uri"] = source_candidate_uri
            payload = {
                "status": "candidate_ready",
                "candidate_type": "web_source_candidate",
                "topic": topic,
                "source": item,
                "search_context": context,
                "review_notes": item.get("why_relevant", ""),
            }
            staged.append(
                self.candidates.add(
                    source_type="web_update_agent",
                    source_id=topic,
                    candidate_type="web_source_candidate",
                    title=title,
                    payload=payload,
                    status="candidate_ready",
                    upload=upload,
                )
            )
        return {
            "ok": True,
            "topic": topic,
            "searched": len(search_results),
            "fetched": len(fetch_results),
            "candidate_count": len(staged),
            "candidates": staged,
            "rejected": normalized.get("rejected", []),
        }

    def _write_source_candidate(
        self,
        *,
        topic: str,
        item: dict[str, Any],
        context: dict[str, Any],
        upload: bool,
    ) -> str:
        slug_basis = item.get("title") or item.get("url") or topic
        path = self.layout.source_dir("web") / "candidates" / f"{self.layout.slug(slug_basis) or 'source'}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "topic": topic,
            "source": item,
            "discovered_by": "web_update_agent",
            "search_results": context.get("web_search_results", []),
            "fetch_results": context.get("web_fetch_results", []),
        }
        markdown = _source_candidate_markdown(payload)
        path.write_text(markdown, encoding="utf-8")
        key = self.storage.key_for_local_path(path)
        if not upload:
            return f"local://{key}"
        return self.storage.upload_text(key, markdown, content_type="text/markdown; charset=utf-8")

    def _decide(self, context: dict[str, Any]) -> dict[str, Any]:
        prompt = WEB_UPDATE_PROMPT.format(
            payload=json.dumps(context, ensure_ascii=False, indent=2)
        )
        raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=2400)
        parsed = parse_json_object(raw)
        if not parsed:
            raise ValueError("web update agent returned invalid JSON")
        return parsed

    def _existing_cards(self, topic: str) -> list[dict[str, Any]]:
        return [
            {
                "id": card.get("id", ""),
                "title": card.get("title", ""),
                "page_type": card.get("page_type", ""),
                "summary": sanitize_wiki_text(card.get("summary", ""))[:360],
                "source_urls": card.get("source_urls", [])[:4],
            }
            for card in self.wiki_store.search_cards(topic, limit=8)
        ]

    @staticmethod
    def _deterministic_decision(topic: str, results: list[WebSearchResult]) -> dict[str, Any]:
        candidates = []
        rejected = []
        for result in results:
            url = result.url
            lower = (result.title + " " + url).lower()
            if any(domain in lower for domain in ["arxiv.org", "openreview.net", "github.com", "paperswithcode.com"]):
                candidates.append({
                    "source_type": _guess_source_type(url, result.title),
                    "title": result.title,
                    "url": url,
                    "why_relevant": result.snippet or f"Search result for {topic}",
                    "expected_wiki_impact": "update_card",
                    "confidence": "medium",
                })
            else:
                rejected.append({"url": url, "reason": "not a primary or high-signal source"})
        return {"topic": topic, "candidates": candidates[:5], "rejected": rejected}

    @staticmethod
    def _normalize_decision(decision: dict[str, Any], topic: str) -> dict[str, Any]:
        allowed_types = {"paper", "repo", "blog", "benchmark", "documentation"}
        allowed_impact = {"new_card", "update_card", "background_only"}
        allowed_confidence = {"high", "medium", "low"}
        candidates = []
        for item in decision.get("candidates") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = sanitize_wiki_text(str(item.get("title") or ""))[:240]
            if not url or not title:
                continue
            source_type = str(item.get("source_type") or _guess_source_type(url, title))
            if source_type not in allowed_types:
                source_type = _guess_source_type(url, title)
            impact = str(item.get("expected_wiki_impact") or "background_only")
            if impact not in allowed_impact:
                impact = "background_only"
            confidence = str(item.get("confidence") or "low")
            if confidence not in allowed_confidence:
                confidence = "low"
            candidates.append({
                "source_type": source_type,
                "title": title,
                "url": url,
                "why_relevant": sanitize_wiki_text(str(item.get("why_relevant") or ""))[:1000],
                "expected_wiki_impact": impact,
                "confidence": confidence,
            })
        rejected = [
            {
                "url": str(item.get("url") or ""),
                "reason": sanitize_wiki_text(str(item.get("reason") or ""))[:500],
            }
            for item in decision.get("rejected") or []
            if isinstance(item, dict)
        ]
        return {"topic": str(decision.get("topic") or topic), "candidates": candidates, "rejected": rejected}


def _search_result_dict(item: WebSearchResult) -> dict[str, str]:
    return {"title": item.title, "url": item.url, "snippet": item.snippet}


def _fetch_result_dict(item: Any) -> dict[str, Any]:
    return {
        "title": getattr(item, "title", ""),
        "url": getattr(item, "url", ""),
        "site": getattr(item, "site", ""),
        "snippet": getattr(item, "snippet", ""),
        "text_excerpt": getattr(item, "text_excerpt", "")[:1600],
        "passages": getattr(item, "passages", [])[:4],
        "published_at": getattr(item, "published_at", ""),
        "author": getattr(item, "author", ""),
        "status": getattr(item, "status", ""),
        "error": getattr(item, "error", ""),
    }


def _guess_source_type(url: str, title: str = "") -> str:
    lower = (url + " " + title).lower()
    if "github.com" in lower:
        return "repo"
    if any(token in lower for token in ["arxiv.org", "openreview.net", "paper", "proceedings"]):
        return "paper"
    if any(token in lower for token in ["benchmark", "leaderboard", "paperswithcode"]):
        return "benchmark"
    if any(token in lower for token in ["docs", "documentation", "readthedocs"]):
        return "documentation"
    return "blog"


def _source_candidate_markdown(payload: dict[str, Any]) -> str:
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    lines = [
        "---",
        "schema_version: web-source-candidate-v1",
        f"title: {source.get('title', '')}",
        f"url: {source.get('url', '')}",
        f"source_type: {source.get('source_type', '')}",
        f"topic: {payload.get('topic', '')}",
        "status: candidate_ready",
        "discovered_by: web_update_agent",
        "---",
        "",
        f"# {source.get('title') or payload.get('topic') or 'Web Source Candidate'}",
        "",
        f"- URL: {source.get('url', '')}",
        f"- Source type: {source.get('source_type', '')}",
        f"- Expected wiki impact: {source.get('expected_wiki_impact', '')}",
        f"- Confidence: {source.get('confidence', '')}",
        "",
        "## Why Relevant",
        "",
        source.get("why_relevant", ""),
        "",
        "## Discovery Context",
        "",
        "```json",
        json.dumps(payload, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)
