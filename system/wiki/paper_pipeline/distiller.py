from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from system.conversation.context_budget import default_counter
from system.core.llm_call import invoke_structured
from system.storage import get_object_storage

from system.wiki.paper_pipeline.models import CandidateClaim, DistilledCandidate, SourcePacket
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore, normalize_alias
from system.wiki.wiki_builder import sanitize_wiki_text


PAPER_PAGE_PROMPT = """\
You compile one research paper into a detailed, self-contained PaperWiki page.
The page must let a reader understand the paper's motivation, design, execution,
evaluation and limitations without reopening the PDF for ordinary questions.

Output language:
- Write all free-text explanations in Simplified Chinese.
- Keep paper titles, model names, method names, metrics, datasets, and equations
  in their original language.

Hard rules:
- Return valid JSON only. No markdown fences and no prose outside JSON.
- Do not invent authors, datasets, metrics, numbers, figure contents, or conclusions.
- Cover every substantive part of the paper. Do not summarize only the abstract or introduction.
- Explain causal links: what problem each design choice solves and how components interact.
- For empirical papers, state datasets/models/baselines/metrics/settings before reporting results.
- selected_table_ids may contain only IDs shown in the source. Select 2-5 tables that best
  explain the main results, comparisons or ablations. Python will insert their exact Markdown;
  never copy or rewrite table cells into another field.
- figure_notes may contain only figure_ids shown in the source. Describe each useful figure
  from its caption and nearby author-written discussion. State trends, axes, conditions and
  key values only when the supplied text explicitly supports them. Never infer unseen pixels.
- Every claim must include evidence copied or tightly paraphrased from the source,
  a section_id, and only evidence_ids shown in that source section header. If an exact
  evidence ID is unavailable, leave evidence_ids empty; the verifier will bind by text.
- Describe each claim with subject/aspect/predicate/value/scope. Do not decide
  whether it supports, challenges, or supersedes the Wiki; the merge stage owns
  all cross-source relation decisions.
- Produce 6-12 high-value claims distributed across method, results and limitations when supported.
- Use arrays of named objects for components, experiment settings and results so the rendered
  page remains readable. Use "论文未明确报告" for a genuinely absent limitation; do not guess.

Return this exact JSON shape:
{
  "paper_page": {
    "candidate_type": "paper_page",
    "page_type": "PaperPage",
    "title": "...",
    "aliases": [],
    "summary": "...",
    "content_json": {
      "schema_version": "paper-wiki-v2",
      "compile_status": "llm_refined",
      "paper_type": "empirical/system/theory/survey/other",
      "research_problem": "",
      "motivation": "",
      "contributions": [""],
      "method_overview": "",
      "method_components": [{"name":"", "purpose":"", "mechanism":"", "details":""}],
      "execution_flow": [""],
      "experiment_setup": [{"name":"Datasets / Models / Baselines / Metrics / Hardware", "details":""}],
      "key_results": [{"finding":"", "evidence":"", "conditions":"", "table_id":""}],
      "selected_table_ids": ["tbl-..."],
      "figure_notes": [{"figure_id":"fig-...", "description":"", "trend":"", "conditions":"", "key_values":[]}],
      "ablations": [{"factor":"", "finding":"", "implication":"", "table_id":""}],
      "limitations": "",
      "comparison_to_prior_work": "",
      "key_takeaways": [""],
      "interview_notes": [],
      "notes": ""
    },
    "claims": [{
      "claim": "...",
      "subject": "the knowledge entity being discussed",
      "aspect": "the specific property being asserted",
      "predicate": "requires / improves / uses / equals / ...",
      "value": "normalized value when applicable",
      "scope": {"version": "", "model": "", "dataset": "", "task": "", "setting": ""},
      "qualifiers": [],
      "evidence": "...",
      "section_id": "...",
      "page_start": 0,
      "evidence_ids": ["ev-..."]
    }],
    "related_topics": [],
    "source_level": "primary"
  }
}

Paper title: __TITLE__

Source packet (section IDs and artifact IDs are authoritative):
__SOURCE_CONTEXT__
"""


TOPIC_PROMPT = """\
You create reusable TopicPage candidates from a compiled paper page. Return JSON only.
Write explanations in Simplified Chinese while preserving technical names in their original language.
Create 0-4 cards. Create a card only for a durable concept or method that future papers can extend;
do not split incidental paper details into separate cards. Every claim must reuse source-grounded
evidence and section IDs from the compiled paper. Do not invent facts.

Return:
{
  "knowledge_cards": [
    {
      "candidate_type": "concept_card",
      "page_type": "TopicPage",
      "title": "...",
      "aliases": ["..."],
      "summary": "...",
      "content_json": {
        "schema_version": "topic-wiki-v1",
        "compile_status": "llm_refined",
        "definition": "",
        "mechanism": "",
        "method": "",
        "findings": "",
        "limitations": "",
        "key_takeaways": []
      },
      "claims": [{
        "claim": "...",
        "subject": "the card concept or method",
        "aspect": "the specific property being asserted",
        "predicate": "requires / improves / uses / equals / ...",
        "value": "normalized value when applicable",
        "scope": {"version": "", "model": "", "dataset": "", "task": "", "setting": ""},
        "qualifiers": [],
        "evidence": "...",
        "section_id": "...",
        "page_start": 0,
        "evidence_ids": ["ev-..."]
      }],
      "related_topics": [],
      "source_level": "primary"
    }
  ]
}

Compiled paper page:
__PAPER_PAGE__
"""


REVISION_PROMPT = """\
You are revising a PaperWiki page that failed deterministic coverage checks.
Return the complete paper_page JSON object in the same paper-wiki-v2 schema.
Fix every listed issue using only the supplied source. Preserve correct detail and evidence.
Do not invent missing facts, numbers, tables, figures or limitations.

Coverage issues:
__ISSUES__

Current candidate:
__CANDIDATE__

Source:
__SOURCE_CONTEXT__
"""


class PaperDistiller:
    def __init__(self, llm=None, store: PaperWikiPipelineStore | None = None):
        self.llm = llm
        self.store = store

    def distill(self, packet: SourcePacket) -> list[DistilledCandidate]:
        candidates = self._llm_distill(packet) if self.llm else []
        if not candidates:
            candidates = self._fallback_candidates(packet)
        for candidate in candidates:
            self._attach_source_artifacts(candidate, packet)
        deduped = self._dedupe_candidates(candidates)
        for candidate in deduped:
            candidate.source_packet_id = packet.source_id
            candidate.id = candidate.id or str(uuid.uuid4())
            candidate.content_json.setdefault("source_packet_id", packet.source_id)
            candidate.content_json.setdefault("raw_source_path", packet.raw_source_path)
            candidate.content_json.setdefault("pdf_storage_uri", packet.pdf_storage_uri)
            candidate.content_json.setdefault("compiler_model", getattr(self.llm, "model", "") if self.llm else "")
            if self.store:
                self.store.insert_candidate(candidate)
        return deduped

    def _llm_distill(self, packet: SourcePacket) -> list[DistilledCandidate]:
        source_context = self._source_context_for_prompt(packet)
        prompt = PAPER_PAGE_PROMPT.replace("__TITLE__", packet.title).replace("__SOURCE_CONTEXT__", source_context)
        try:
            raw = invoke_structured(
                self.llm,
                prompt,
                temperature=0.0,
                max_tokens=int(os.getenv("PAPERWIKI_PAPER_OUTPUT_TOKENS", "12000")),
            )
        except Exception as exc:
            print(f"[paper_pipeline.distiller] LLM distill failed: {exc}")
            return []
        payload = parse_json_object(raw)
        if not payload:
            return []
        paper = payload.get("paper_page")
        candidate = self._candidate_from_payload(paper) if isinstance(paper, dict) else None
        if not candidate:
            return []
        self._attach_source_artifacts(candidate, packet)

        issues = paper_coverage_issues(candidate, packet)
        if issues:
            revised = self._revise_paper(candidate, packet, source_context, issues)
            if revised:
                self._attach_source_artifacts(revised, packet)
                if len(paper_coverage_issues(revised, packet)) <= len(issues):
                    candidate = revised

        return [candidate] + self._llm_topic_candidates(candidate)

    def _revise_paper(
        self,
        candidate: DistilledCandidate,
        packet: SourcePacket,
        source_context: str,
        issues: list[str],
    ) -> DistilledCandidate | None:
        prompt = (
            REVISION_PROMPT
            .replace("__ISSUES__", "\n".join(f"- {issue}" for issue in issues))
            .replace("__CANDIDATE__", _candidate_prompt_json(candidate))
            .replace("__SOURCE_CONTEXT__", source_context)
        )
        try:
            raw = invoke_structured(
                self.llm,
                prompt,
                temperature=0.0,
                max_tokens=int(os.getenv("PAPERWIKI_PAPER_OUTPUT_TOKENS", "12000")),
            )
        except Exception as exc:
            print(f"[paper_pipeline.distiller] Paper coverage revision failed: {exc}")
            return None
        payload = parse_json_object(raw) or {}
        item = payload.get("paper_page") if isinstance(payload.get("paper_page"), dict) else payload
        revised = self._candidate_from_payload(item) if isinstance(item, dict) else None
        return revised if revised and revised.candidate_type == "paper_page" else None

    def _llm_topic_candidates(self, paper: DistilledCandidate) -> list[DistilledCandidate]:
        prompt = TOPIC_PROMPT.replace("__PAPER_PAGE__", _candidate_prompt_json(paper, include_artifacts=False))
        try:
            raw = invoke_structured(
                self.llm,
                prompt,
                temperature=0.0,
                max_tokens=int(os.getenv("PAPERWIKI_TOPIC_OUTPUT_TOKENS", "4500")),
            )
        except Exception as exc:
            print(f"[paper_pipeline.distiller] Topic distill failed: {exc}")
            return []
        payload = parse_json_object(raw) or {}
        candidates = []
        for item in payload.get("knowledge_cards") or []:
            if not isinstance(item, dict):
                continue
            candidate = self._candidate_from_payload(item)
            if candidate:
                candidates.append(candidate)
        return candidates

    def _candidate_from_payload(self, item: dict[str, Any]) -> DistilledCandidate | None:
        candidate_type = item.get("candidate_type")
        page_type = item.get("page_type")
        if candidate_type not in {"paper_page", "concept_card", "method_card"}:
            return None
        if page_type not in {"PaperPage", "TopicPage", "ConceptPage", "MethodPage"}:
            return None
        if candidate_type in {"concept_card", "method_card"}:
            # Concept/Method remain ingestion hints. Both compile to one clean,
            # reusable topic article instead of parallel user-facing card types.
            page_type = "TopicPage"
        title = sanitize_wiki_text(str(item.get("title") or "")).strip()
        if not title:
            return None
        claims = []
        for claim in item.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            claim_text = sanitize_wiki_text(str(claim.get("claim") or ""))
            evidence = sanitize_wiki_text(str(claim.get("evidence") or ""))
            if claim_text and evidence:
                scope_payload = claim.get("scope") if isinstance(claim.get("scope"), dict) else {}
                claims.append(CandidateClaim(
                    claim=claim_text,
                    evidence=evidence[:1000],
                    subject=sanitize_wiki_text(str(claim.get("subject") or title)),
                    aspect=sanitize_wiki_text(str(claim.get("aspect") or "")),
                    predicate=sanitize_wiki_text(str(claim.get("predicate") or "")),
                    value=sanitize_wiki_text(str(claim.get("value") or "")),
                    scope={
                        sanitize_wiki_text(str(key)): sanitize_wiki_text(str(value))
                        for key, value in scope_payload.items()
                        if str(key).strip() and str(value).strip()
                    },
                    qualifiers=[
                        sanitize_wiki_text(str(value))
                        for value in claim.get("qualifiers") or [] if str(value).strip()
                    ],
                    section_id=str(claim.get("section_id") or ""),
                    page_start=int(claim.get("page_start") or 0),
                    evidence_ids=[str(value) for value in claim.get("evidence_ids") or [] if str(value)],
                    relation=str(claim.get("relation") or "supports") if str(claim.get("relation") or "supports") in {"supports", "challenges", "supersedes"} else "supports",
                    confidence=float(claim.get("confidence") or 1.0),
                ))
        return DistilledCandidate(
            candidate_type=candidate_type,
            page_type=page_type,
            title=title,
            aliases=[sanitize_wiki_text(str(alias)) for alias in item.get("aliases") or [] if str(alias).strip()],
            summary=sanitize_wiki_text(str(item.get("summary") or "")),
            content_json=item.get("content_json") if isinstance(item.get("content_json"), dict) else {},
            claims=claims,
            related_topics=[sanitize_wiki_text(str(topic)) for topic in item.get("related_topics") or [] if str(topic).strip()],
            source_level=item.get("source_level") or "primary",
        )

    def _fallback_candidates(self, packet: SourcePacket) -> list[DistilledCandidate]:
        evidence = _best_evidence(packet)
        paper = DistilledCandidate(
            candidate_type="paper_page",
            page_type="PaperPage",
            title=packet.title,
            summary=packet.abstract[:400],
            content_json={
                "schema_version": "paper-wiki-v1",
                "compile_status": "distilled_local",
                "problem": _fallback_multiline(packet.abstract, 4),
                "key_idea": _fallback_multiline(packet.abstract, 6),
                "method": _fallback_multiline(evidence["text"], 6),
                "results": "",
                "limitations": "",
                "key_takeaways": [packet.abstract[:220]] if packet.abstract else [],
                "notes": "LLM distillation unavailable; local paper candidate generated from abstract.",
            },
            claims=[CandidateClaim(claim=packet.abstract[:240] or packet.title, evidence=evidence["text"], section_id=evidence["section_id"], page_start=evidence["page_start"], evidence_ids=evidence["evidence_ids"])],
            related_topics=[],
        )
        return [paper]

    def _seed_reusable_candidates(self, packet: SourcePacket) -> list[DistilledCandidate]:
        text = " ".join([packet.title, packet.abstract] + [section.heading + " " + section.text[:1000] for section in packet.sections[:8]])
        lowered = text.lower()
        seeds: list[tuple[str, str, list[str], str]] = []
        if "difficulty" in lowered and ("question" in lowered or "problem" in lowered) and "llm" in lowered:
            seeds.append((
                "LLM-perceived question difficulty",
                "ConceptPage",
                [
                    "LLM perceived question difficulty",
                    "perceived difficulty",
                    "question difficulty perception",
                    "LLM question difficulty",
                    "difficulty perception",
                    "Question Difficulty",
                ],
                "LLM 对问题难度的内部感知或估计，可通过隐藏表示、输出概率或探针信号被外部建模。",
            ))
        if "difficulty perception" in lowered:
            seeds.append((
                "Question difficulty perception",
                "ConceptPage",
                ["difficulty perception", "difficulty perception mechanism", "LLM perceived question difficulty"],
                "模型对问题难度形成判断的内部机制，常用于解释模型为什么会把某些问题视为更难。",
            ))
        if "hidden representation" in lowered or "hidden representations" in lowered:
            seeds.append((
                "Hidden representations",
                "ConceptPage",
                [
                    "Hidden Representations of LLMs",
                    "hidden states",
                    "internal representations",
                    "token-level hidden representation",
                ],
                "LLM 中间层的隐藏表示，可用于探测模型内部状态、任务感知和推理过程。",
            ))
        if "probing" in lowered or "probe" in lowered:
            seeds.append((
                "Probing LLM internal mechanisms",
                "MethodPage",
                ["probing", "mechanistic probing", "LLM probing"],
                "通过探针或表示分析研究 LLM 内部机制的方法，用于把模型内部信号映射到可解释变量。",
            ))
        if "estimate" in lowered and "difficulty" in lowered:
            seeds.append((
                "Difficulty estimation",
                "MethodPage",
                [
                    "Difficulty Estimation via Hidden Representations",
                    "question difficulty estimation",
                    "difficulty prediction",
                ],
                "估计问题对模型而言难易程度的方法，可用于样本筛选、推理预算分配和自适应解题策略。",
            ))

        evidence = _best_evidence(packet)
        candidates = []
        for title, page_type, aliases, definition in seeds:
            candidate_type = "method_card" if page_type == "MethodPage" else "concept_card"
            candidates.append(DistilledCandidate(
                candidate_type=candidate_type,
                page_type="TopicPage",
                title=title,
                aliases=aliases + [title],
                summary=definition,
                content_json={
                    "schema_version": "paper-wiki-v1",
                    "compile_status": "seeded_candidate",
                    "definition": definition,
                    "mechanism": "",
                    "method": "",
                    "findings": "",
                    "limitations": "",
                    "key_takeaways": [definition],
                },
                claims=[CandidateClaim(
                    claim=definition,
                    evidence=evidence["text"],
                    section_id=evidence["section_id"],
                    page_start=evidence["page_start"],
                    evidence_ids=evidence["evidence_ids"],
                )],
                related_topics=["LLM", "difficulty perception"],
            ))
        return candidates

    @staticmethod
    def _dedupe_candidates(candidates: list[DistilledCandidate]) -> list[DistilledCandidate]:
        alias_index: dict[tuple[str, str], int] = {}
        deduped: list[DistilledCandidate] = []
        for candidate in candidates:
            keys = _candidate_alias_keys(candidate)
            existing_index = next((alias_index[key] for key in keys if key in alias_index), None)
            if existing_index is None:
                existing_index = next(
                    (
                        index
                        for index, existing in enumerate(deduped)
                        if existing.page_type == candidate.page_type
                        and _candidate_aliases_overlap(existing, candidate)
                    ),
                    None,
                )
            if existing_index is not None:
                existing = deduped[existing_index]
                merged = _merge_duplicate_candidate(existing, candidate)
                deduped[existing_index] = merged
                for key in _candidate_alias_keys(merged):
                    alias_index[key] = existing_index
                continue
            deduped.append(candidate)
            new_index = len(deduped) - 1
            for key in keys:
                alias_index[key] = new_index
        return deduped

    def _attach_source_artifacts(self, candidate: DistilledCandidate, packet: SourcePacket) -> None:
        """Resolve model-selected IDs and attach exact parser output.

        The LLM chooses relevance and writes explanations. Python owns identity
        and bytes: table cells are copied from SourceTable.markdown verbatim, so
        no model transcription can change a metric.
        """
        if candidate.candidate_type != "paper_page":
            return
        content = candidate.content_json
        requested = content.get("selected_table_ids") or []
        if isinstance(requested, str):
            requested = re.findall(r"tbl-[0-9a-z-]+", requested, flags=re.IGNORECASE)
        table_map = {table.table_id: table for table in packet.tables}
        selected = [str(value) for value in requested if str(value) in table_map]
        if not selected and packet.tables:
            selected = [table.table_id for table in _rank_key_tables(packet.tables)]
        selected = list(dict.fromkeys(selected))[: int(os.getenv("PAPERWIKI_MAX_CARD_TABLES", "5"))]
        content["selected_table_ids"] = selected
        content["key_tables"] = [
            {
                "table_id": table_id,
                "caption": table_map[table_id].caption,
                "section": " / ".join(table_map[table_id].section_path),
                "page": table_map[table_id].page,
                "markdown": table_map[table_id].markdown,
            }
            for table_id in selected
        ]

        figure_map = {str(item.get("figure_id") or ""): item for item in packet.figures if isinstance(item, dict)}
        requested_notes = content.get("figure_notes") or []
        if isinstance(requested_notes, dict):
            requested_notes = [requested_notes]
        notes = []
        for item in requested_notes:
            if not isinstance(item, dict):
                continue
            figure_id = str(item.get("figure_id") or "")
            source = figure_map.get(figure_id)
            if not source:
                continue
            notes.append(_resolved_figure_note(source, item))
        if not notes and figure_map:
            for source in list(figure_map.values())[: int(os.getenv("PAPERWIKI_MAX_CARD_FIGURES", "6"))]:
                notes.append(_resolved_figure_note(source, {}))
        content["figure_notes"] = notes[: int(os.getenv("PAPERWIKI_MAX_CARD_FIGURES", "6"))]

    @staticmethod
    def _source_context_for_prompt(packet: SourcePacket) -> str:
        return build_source_context(packet)

    @staticmethod
    def _sections_for_prompt(packet: SourcePacket) -> str:
        """Compatibility wrapper for callers/tests using the old helper name."""
        return build_source_context(packet)


def build_source_context(packet: SourcePacket) -> str:
    """Build a token-budgeted, section-balanced view of the complete paper."""
    counter = default_counter()
    budget = max(16_000, int(os.getenv("PAPERWIKI_PAPER_INPUT_TOKENS", "320000")))
    outline = "\n".join(
        f"- section_id={section.section_id}; heading={section.heading}; page={section.page_start or 'unknown'}"
        for section in packet.sections
    )
    section_parts = []
    section_weights = []
    for section in packet.sections:
        text = sanitize_wiki_text(section.text)
        if not text:
            continue
        evidence_ids = section.evidence_ids[:24]
        evidence_suffix = f" (+{len(section.evidence_ids) - 24} more)" if len(section.evidence_ids) > 24 else ""
        section_parts.append(
            f"[SECTION section_id={section.section_id}; heading={section.heading}; "
            f"page={section.page_start or 'unknown'}; evidence_ids={','.join(evidence_ids)}{evidence_suffix}]\n{text}"
        )
        section_weights.append(_section_weight(section.heading))

    if not section_parts and packet.raw_source_path:
        raw = _strip_source_frontmatter(get_object_storage().read_text(packet.raw_source_path))
        if raw:
            section_parts = [f"[SECTION section_id=full-paper; heading={packet.title}; page=unknown]\n{raw}"]
            section_weights = [1.0]

    artifact_parts = []
    for table in packet.tables:
        artifact_parts.append(
            f"[TABLE table_id={table.table_id}; heading={' / '.join(table.section_path)}; "
            f"page={table.page or 'unknown'}; element_id={table.element_id}]\n"
            f"Caption: {table.caption or '(caption unavailable)'}\n{table.markdown}"
        )
    for figure in packet.figures:
        if not isinstance(figure, dict):
            continue
        artifact_parts.append(
            f"[FIGURE figure_id={figure.get('figure_id', '')}; heading={' / '.join(figure.get('section_path') or [])}; "
            f"page={figure.get('page') or 'unknown'}; asset={figure.get('asset_path', '')}]\n"
            f"Caption: {figure.get('caption') or '(caption unavailable)'}\n"
            f"Nearby paper discussion: {figure.get('source_text') or '(unavailable)'}"
        )

    header = (
        f"Title: {packet.title}\nAbstract: {sanitize_wiki_text(packet.abstract)}\n\n"
        f"Complete section outline:\n{outline or '- unavailable'}"
    )
    full = "\n\n---\n\n".join([header] + section_parts + artifact_parts)
    if counter.count(full) <= budget:
        return full

    separator_cost = counter.count("\n\n---\n\n")
    remaining = max(0, budget - counter.count(header) - separator_cost * (len(section_parts) + len(artifact_parts)))
    artifact_need = sum(counter.count(part) for part in artifact_parts)
    artifact_budget = min(artifact_need, max(2_000, int(remaining * 0.30))) if artifact_parts else 0
    section_budget = max(0, remaining - artifact_budget)
    clipped_sections = _fit_weighted_parts(section_parts, section_weights, section_budget, counter)
    clipped_artifacts = _fit_weighted_parts(artifact_parts, [1.0] * len(artifact_parts), artifact_budget, counter)
    return "\n\n---\n\n".join([header] + clipped_sections + clipped_artifacts)


def _fit_weighted_parts(parts: list[str], weights: list[float], budget: int, counter) -> list[str]:
    if not parts or budget <= 0:
        return []
    total_weight = sum(weights) or float(len(parts))
    allocations = [max(160, int(budget * weight / total_weight)) for weight in weights]
    # If minimum allocations overshoot, equal-share every item so the complete
    # outline still has a corresponding source excerpt.
    if sum(allocations) > budget:
        allocations = [max(32, budget // len(parts))] * len(parts)
    fitted = [counter.clip(part, allocation) for part, allocation in zip(parts, allocations)]
    while counter.count("\n\n---\n\n".join(fitted)) > budget and any(fitted):
        largest = max(range(len(fitted)), key=lambda index: counter.count(fitted[index]))
        current = counter.count(fitted[largest])
        fitted[largest] = counter.clip(fitted[largest], max(0, current - 128))
    return [part for part in fitted if part]


def _section_weight(heading: str) -> float:
    heading = (heading or "").lower()
    if any(key in heading for key in (
        "method", "approach", "system", "architecture", "implement", "experiment",
        "evaluation", "result", "analysis", "ablation", "discussion", "limitation",
        "conclusion", "方法", "实验", "结果", "消融", "限制",
    )):
        return 3.0
    if any(key in heading for key in ("abstract", "introduction", "background", "related", "摘要", "引言")):
        return 2.0
    return 1.0


def _strip_source_frontmatter(markdown: str) -> str:
    text = str(markdown or "")
    if text.startswith("---"):
        match = re.match(r"^---\s*\n.*?\n---\s*\n", text, flags=re.DOTALL)
        if match:
            return text[match.end():]
    return text


def _rank_key_tables(tables: list[Any]) -> list[Any]:
    keywords = (
        "main result", "comparison", "performance", "accuracy", "throughput", "latency",
        "quality", "benchmark", "ablation", "perplexity", "memory", "speedup",
        "主要结果", "对比", "性能", "准确率", "吞吐", "延迟", "消融",
    )

    def score(table: Any) -> tuple[int, int]:
        text = f"{table.caption} {' '.join(table.section_path)} {table.markdown[:1200]}".lower()
        keyword_score = sum(5 for keyword in keywords if keyword in text)
        size_score = min(8, len(table.rows)) + min(5, len(table.headers[-1]) if table.headers else 0)
        return keyword_score + size_score, len(table.markdown)

    limit = int(os.getenv("PAPERWIKI_DEFAULT_CARD_TABLES", "3"))
    return sorted(tables, key=score, reverse=True)[: max(1, limit)]


def _resolved_figure_note(source: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
    caption = sanitize_wiki_text(str(source.get("caption") or ""))
    source_text = sanitize_wiki_text(str(source.get("source_text") or ""))
    description = sanitize_wiki_text(str(proposed.get("description") or ""))
    if not description:
        description = caption or source_text or "MinerU 提取到该图，但论文文本未提供足够描述。"
    return {
        "figure_id": str(source.get("figure_id") or ""),
        "caption": caption,
        "section": " / ".join(source.get("section_path") or []),
        "page": int(source.get("page") or 0),
        "asset_path": str(source.get("asset_path") or ""),
        "description": description,
        "trend": sanitize_wiki_text(str(proposed.get("trend") or "")),
        "conditions": sanitize_wiki_text(str(proposed.get("conditions") or "")),
        "key_values": [
            sanitize_wiki_text(str(value)) for value in proposed.get("key_values") or [] if str(value).strip()
        ],
        "source_text": source_text,
    }


def paper_coverage_issues(candidate: DistilledCandidate, packet: SourcePacket | None = None) -> list[str]:
    """Deterministic completeness gate for the richer PaperPage schema."""
    content = candidate.content_json or {}
    if candidate.candidate_type != "paper_page" or content.get("schema_version") != "paper-wiki-v2":
        return []
    issues = []
    paper_type = str(content.get("paper_type") or "").lower()
    required_text = {
        "research_problem": 80,
        "motivation": 60,
        "method_overview": 120,
    }
    if packet and any(
        any(term in (section.heading or "").lower() for term in ("related", "background", "prior work", "相关工作", "背景"))
        for section in packet.sections
    ):
        required_text["comparison_to_prior_work"] = 60
    for key, minimum in required_text.items():
        if len(sanitize_wiki_text(str(content.get(key) or ""))) < minimum:
            issues.append(f"{key} is missing or too shallow (minimum {minimum} characters)")
    for key, minimum in (("contributions", 2), ("key_takeaways", 3)):
        value = content.get(key)
        if not isinstance(value, list) or len([item for item in value if item not in (None, "", {})]) < minimum:
            issues.append(f"{key} needs at least {minimum} substantive items")
    empirical = bool(packet and packet.tables) or paper_type in {"empirical", "system"}
    if empirical:
        for key, minimum in (("method_components", 2), ("execution_flow", 3)):
            value = content.get(key)
            if not isinstance(value, list) or len([item for item in value if item not in (None, "", {})]) < minimum:
                issues.append(f"empirical/system paper {key} needs at least {minimum} substantive items")
        if not isinstance(content.get("experiment_setup"), list) or not content.get("experiment_setup"):
            issues.append("empirical/system paper needs experiment_setup")
        if not isinstance(content.get("key_results"), list) or len(content.get("key_results") or []) < 2:
            issues.append("empirical/system paper needs at least two key_results")
    if packet and packet.tables and not content.get("key_tables"):
        issues.append("source contains tables but no exact key_tables were attached")
    if packet and packet.figures and not content.get("figure_notes"):
        issues.append("source contains figures but no figure_notes were attached")
    return issues


def _candidate_prompt_json(candidate: DistilledCandidate, *, include_artifacts: bool = True) -> str:
    payload = candidate.model_dump() if hasattr(candidate, "model_dump") else candidate.dict()
    if not include_artifacts:
        content = dict(payload.get("content_json") or {})
        content.pop("key_tables", None)
        content.pop("figure_notes", None)
        payload["content_json"] = content
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _best_evidence(packet: SourcePacket) -> dict[str, Any]:
    for section in packet.sections:
        text = sanitize_wiki_text(section.text)
        if len(text) >= 80:
            return {"text": text[:900], "section_id": section.section_id, "page_start": section.page_start, "evidence_ids": section.evidence_ids[:4]}
    return {"text": sanitize_wiki_text(packet.abstract or packet.title)[:900], "section_id": "abstract", "page_start": 1, "evidence_ids": []}


def _fallback_multiline(text: str, target_lines: int) -> str:
    text = sanitize_wiki_text(text)
    if not text:
        return ""
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[。！？.!?])\s+", text)
        if len(part.strip()) >= 12
    ]
    if not sentences:
        sentences = [text[i:i + 120].strip() for i in range(0, min(len(text), target_lines * 120), 120)]
    return "\n".join(sentences[:target_lines])


def _candidate_alias_keys(candidate: DistilledCandidate) -> set[tuple[str, str]]:
    aliases = [candidate.title] + candidate.aliases
    return {
        (candidate.page_type, normalize_alias(alias))
        for alias in aliases
        if normalize_alias(alias)
    }


def _candidate_aliases_overlap(a: DistilledCandidate, b: DistilledCandidate) -> bool:
    a_aliases = [normalize_alias(alias) for alias in [a.title] + a.aliases]
    b_aliases = [normalize_alias(alias) for alias in [b.title] + b.aliases]
    for left in a_aliases:
        for right in b_aliases:
            if not left or not right:
                continue
            shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
            if len(shorter) >= 16 and shorter in longer:
                return True
    return False


def _merge_duplicate_candidate(a: DistilledCandidate, b: DistilledCandidate) -> DistilledCandidate:
    winner, loser = (a, b) if _candidate_quality_score(a) >= _candidate_quality_score(b) else (b, a)
    winner.aliases = _unique_strings(winner.aliases + [loser.title] + loser.aliases)
    winner.related_topics = _unique_strings(winner.related_topics + loser.related_topics)
    winner.claims = (winner.claims + loser.claims)[:8]
    if len(sanitize_wiki_text(loser.summary)) > len(sanitize_wiki_text(winner.summary)):
        winner.summary = loser.summary
    if not winner.content_json and loser.content_json:
        winner.content_json = loser.content_json
    return winner


def _candidate_quality_score(candidate: DistilledCandidate) -> int:
    content = candidate.content_json or {}
    status = str(content.get("compile_status") or "")
    text_size = len(sanitize_wiki_text(candidate.summary))
    text_size += sum(len(sanitize_wiki_text(str(value))) for value in content.values() if not isinstance(value, (list, dict)))
    score = min(text_size // 40, 80) + len(candidate.claims) * 5
    if status == "llm_refined":
        score += 100
    if status == "seeded_candidate":
        score -= 30
    return score


def _unique_strings(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        text = sanitize_wiki_text(str(item or "")).strip()
        key = normalize_alias(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def parse_json_object(text: Any) -> dict[str, Any] | None:
    text = str(text or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text[start:end + 1])
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            payload = json.loads(re.sub(r",\s*([}\]])", r"\1", candidate))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None
