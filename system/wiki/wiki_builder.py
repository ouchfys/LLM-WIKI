"""
Wiki Builder — LLM-driven conversion of sources and conversations into wiki cards.

Decides page_type, extracts structured content, and returns card-ready dicts.
"""

import json
import re
from typing import Any, Dict, Optional

from system.wiki.wiki_store import CARD_TYPES

BUILD_FROM_CONVERSATION_PROMPT = """\
You are a knowledge extraction assistant. Given a Q&A exchange about a technical topic, extract key information that should be saved as a structured wiki card.

## Card Types
- ConceptPage: A concept or term explanation
- PaperPage: A research paper summary
- MethodPage: A method or technique description
- ComparePage: A comparison between two items
- InterviewQA: An interview question with ideal answer
- MistakeNote: A mistake the user made with correction
- StudyPlan: A learning or review plan
- SourceNote: A note compiled from a technical article, blog, or pasted source

## Conversation
User: {user_query}
Assistant: {assistant_response}

## Output
Return JSON only, no other text:
{{"page_type": "one of the above", "title": "concise title", "summary": "1-2 sentence summary", "content_json": {{...appropriate fields per page_type...}}, "source_level": "", "related_topics": ["topic1", "topic2"]}}

Content fields per page_type:
- ConceptPage: {{"explanation": "...", "examples": "...", "related_concepts": ["..."]}}
- PaperPage: {{"authors": "...", "year": "", "venue": "", "key_contributions": "...", "methods": "...", "results": "...", "notes": "..."}}
- MethodPage: {{"category": "...", "description": "...", "when_to_use": "...", "steps": "...", "comparison_to_alternatives": "..."}}
- ComparePage: {{"item_a": "...", "item_b": "...", "dimensions": [{{"dimension": "...", "a_value": "...", "b_value": "..."}}]}}
- InterviewQA: {{"question": "...", "ideal_answer": "...", "key_points": ["..."], "common_mistakes": ["..."]}}
- MistakeNote: {{"mistake": "...", "correction": "...", "lesson": "...", "context": "..."}}
- StudyPlan: {{"goal": "...", "steps": "...", "timeline": "...", "review_plan": "..."}}
- SourceNote: {{"source_type": "...", "main_points": "...", "useful_for": "...", "notes": "..."}}

If the exchange does NOT contain save-worthy knowledge, return: {{"skip": true, "reason": "..."}}
"""

SUGGEST_PAGE_TYPE_PROMPT = """\
Given the following text, suggest the best wiki page type from: {card_types}

Text: {text}

Return JSON: {{"page_type": "..."}}
"""

BUILD_FROM_RAW_MARKDOWN_PROMPT = """\
You are the compiler for a private technical Wiki named Jarvis Notes.
Your job is to turn noisy raw source Markdown into one durable review page.

Output language:
- All free-text explanation fields must be written in Chinese: summary, problem,
  key_idea, method, results, limitations, key_takeaways, interview_notes,
  open_questions, my_take, notes, ideal_answer, key_points, common_mistakes.
- If the raw source is English, translate and synthesize the meaning in Chinese.
- Keep paper titles, model names, method names, metrics, datasets, and URLs in their original language.
- 必须输出中文笔记，不要因为论文原文是英文就改用英文总结。

Hard rules:
- Return valid JSON only. No markdown fences. No prose before or after JSON.
- Do not invent authors, venues, numbers, datasets, results, or claims.
- Use the title hint as the title unless it is empty or obviously a parser artifact.
- Prefer evidence in the raw source. If a field is not supported, use an empty string or empty list.
- Ignore OCR garbage, base64/image payloads, legal boilerplate, navigation text, repeated captions, and broken HTML/CSS.
- Do not summarize this as a RAG chunk. Write a page the user can review, ask about, and use for interview/project explanation.
- Set source_level to "primary" for paper_pdf sources, "secondary" for Xiaohongshu/blog/social sources, otherwise "".

Page type: {page_type}
Title hint: {title}
Source kind: {source_kind}

Schema selection:
- For PaperPage, produce AutoSci-style paper reading notes:
  problem, key_idea, method, results, limitations, contribution_type, datasets,
  key_takeaways, interview_notes, open_questions, my_take, notes.
- For InterviewQA, produce interview-ready notes:
  question, ideal_answer, key_points, common_mistakes, interview_notes,
  problem, key_idea, notes.
- For other page types, use the shared fields:
  problem, key_idea, method, results, key_takeaways, interview_notes, notes.

JSON shape:
{{
  "page_type": "{page_type}",
  "title": "{title}",
  "summary": "2-3 sentences, concrete and source-grounded",
  "content_json": {{
    "schema_version": "autosci-deepseek-v1",
    "compile_status": "llm_refined",
    "problem": "",
    "key_idea": "",
    "question": "",
    "ideal_answer": "",
    "key_points": [],
    "common_mistakes": [],
    "method": "",
    "results": "",
    "limitations": "",
    "contribution_type": [],
    "datasets": [],
    "key_takeaways": [],
    "interview_notes": [],
    "open_questions": [],
    "my_take": "",
    "notes": ""
  }},
  "source_level": "",
  "related_topics": []
}}

Raw Markdown:
{markdown}
"""


class WikiBuilder:
    """Convert sources and conversations into structured wiki cards via LLM."""

    def __init__(self, llm):
        self.llm = llm

    def build_from_conversation(
        self,
        user_query: str,
        assistant_response: str,
    ) -> Optional[Dict[str, Any]]:
        """Extract a wiki card from a Q&A exchange. Returns card dict or None."""
        prompt = BUILD_FROM_CONVERSATION_PROMPT.format(
            user_query=self._truncate(user_query, 300),
            assistant_response=self._truncate(assistant_response, 800),
        )

        try:
            raw = self.llm.invoke(prompt)
        except Exception as exc:
            print(f"[WikiBuilder] LLM call failed: {exc}")
            return None

        parsed = self._parse_json(raw)
        if not parsed:
            return None

        if parsed.get("skip"):
            print(f"[WikiBuilder] Skipped: {parsed.get('reason', 'unknown')}")
            return None

        page_type = parsed.get("page_type", "")
        if page_type not in CARD_TYPES:
            print(f"[WikiBuilder] Invalid page_type: {page_type}")
            return None

        return {
            "title": parsed.get("title", "Untitled"),
            "page_type": page_type,
            "summary": parsed.get("summary", ""),
            "content_json": parsed.get("content_json", {}),
            "source_level": parsed.get("source_level", ""),
            "related_topics": parsed.get("related_topics", []),
        }

    def build_from_source(
        self,
        source_item,
        page_type: str = None,
    ) -> Optional[Dict[str, Any]]:
        """Convert a SourceItem into a wiki card. LLM decides page_type if not given."""
        text = f"Title: {source_item.title}\nSummary: {source_item.summary}"
        if source_item.authors:
            text += f"\nAuthors: {', '.join(source_item.authors)}"
        if source_item.year:
            text += f"\nYear: {source_item.year}"
        if source_item.venue:
            text += f"\nVenue: {source_item.venue}"

        if page_type is None:
            page_type = self.suggest_page_type(text)
            if page_type not in CARD_TYPES:
                page_type = "PaperPage"

        prompt = f"""\
You are a knowledge extraction assistant. Convert the following source into a structured wiki card of type "{page_type}".

Source:
{self._truncate(text, 1500)}

Return JSON with: page_type, title, summary, content_json (fields appropriate for {page_type}), source_level, related_topics.
Only return JSON, no other text.
"""
        try:
            raw = self.llm.invoke(prompt)
        except Exception as exc:
            print(f"[WikiBuilder] LLM call failed: {exc}")
            return None

        parsed = self._parse_json(raw)
        if not parsed:
            return None

        return {
            "title": parsed.get("title", source_item.title or "Untitled"),
            "page_type": parsed.get("page_type", page_type),
            "summary": parsed.get("summary", ""),
            "content_json": parsed.get("content_json", {}),
            "source_level": parsed.get("source_level", source_item.source_level or ""),
            "source_urls": [source_item.url] if source_item.url else [],
            "related_topics": parsed.get("related_topics", []),
        }

    def suggest_page_type(self, text: str) -> str:
        """LLM decides the best wiki page_type for given text."""
        prompt = SUGGEST_PAGE_TYPE_PROMPT.format(
            card_types=", ".join(CARD_TYPES),
            text=self._truncate(text, 1000),
        )
        try:
            raw = self.llm.invoke(prompt)
        except Exception:
            return "ConceptPage"

        parsed = self._parse_json(raw)
        if parsed and parsed.get("page_type") in CARD_TYPES:
            return parsed["page_type"]
        return "ConceptPage"

    def build_from_raw_markdown(
        self,
        title: str,
        raw_markdown: str,
        page_type: str = "SourceNote",
        source_kind: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Compile raw Markdown into a durable Wiki page.

        This is the minimal Karpathy-style path: raw source text is converted
        into a readable page before it becomes part of the user's Wiki.
        """
        if page_type not in CARD_TYPES:
            page_type = "SourceNote"

        prompt = BUILD_FROM_RAW_MARKDOWN_PROMPT.format(
            title=title or "Untitled",
            page_type=page_type,
            source_kind=source_kind or "",
            markdown=self._truncate(raw_markdown, 12000),
        )
        try:
            raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=3000)
        except Exception as exc:
            print(f"[WikiBuilder] raw markdown compile failed: {exc}")
            return None

        parsed = self._parse_json(raw)
        if not parsed:
            return None

        next_type = parsed.get("page_type", page_type)
        if next_type not in CARD_TYPES:
            next_type = page_type
        content_json = parsed.get("content_json", {})
        if isinstance(content_json, dict):
            content_json.setdefault("compiler_model", getattr(self.llm, "model", ""))

        return {
            "title": parsed.get("title", title or "Untitled"),
            "page_type": next_type,
            "summary": parsed.get("summary", ""),
            "content_json": content_json if isinstance(content_json, dict) else {},
            "source_level": parsed.get("source_level", ""),
            "related_topics": parsed.get("related_topics", []),
        }

    @staticmethod
    def _truncate(text: str, limit: int = 500) -> str:
        text = sanitize_wiki_text(str(text or "")).strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

    @staticmethod
    def _parse_json(text: Any) -> Optional[Dict[str, Any]]:
        text = str(text or "").strip()
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()
        start = text.find("{")
        if start == -1:
            return None
        candidate = text[start:]
        end = candidate.rfind("}")
        if end != -1:
            candidate = candidate[:end + 1]
        candidate = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", candidate)
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                payload, _ = decoder.raw_decode(repaired)
            except json.JSONDecodeError:
                return None
        return payload if isinstance(payload, dict) else None


def sanitize_wiki_text(text: str) -> str:
    """Remove parser noise that should never become a user-facing Wiki note."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return ""

    text = re.sub(r"!\[[^\]]*\]\(data:image/[^)]+\)", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)

    cleaned_lines = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        compact = re.sub(r"\s+", "", line)
        if len(compact) >= 256:
            allowed = sum(1 for ch in compact if ch.isalnum() or ch in "+/=_-")
            whitespace = sum(1 for ch in raw_line if ch.isspace())
            whitespace_ratio = whitespace / max(len(raw_line), 1)
            if whitespace_ratio < 0.05 and allowed / max(len(compact), 1) > 0.9:
                continue
        cleaned_lines.append(raw_line.rstrip())

    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def pending_compiled_content(
    raw_source_path: str,
    source_kind: str,
    reason: str = "Wiki compiler unavailable",
) -> Dict[str, Any]:
    return {
        "compile_status": "pending",
        "source_kind": source_kind,
        "notes": "原始资料已入库，等待 Wiki 编译生成可读页面。",
        "raw_source_path": raw_source_path,
        "compile_error": reason,
    }


def heuristic_compile_raw_markdown(
    title: str,
    raw_markdown: str,
    page_type: str,
    source_kind: str,
) -> Dict[str, Any]:
    """Local readable fallback when the LLM compiler is unavailable."""
    text = sanitize_wiki_text(raw_markdown)
    text = re.sub(r"^\s*---\n.*?\n---\s*", "", text, count=1, flags=re.DOTALL)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    useful_lines = []
    skip_headings = {
        "source",
        "tags",
        "attachments",
        "来源",
        "标签",
        "附件",
    }
    for line in lines:
        lowered = line.strip("#- * ").lower()
        if lowered in skip_headings:
            continue
        if re.match(r"^#{1,6}\s+", line):
            continue
        if re.match(r"^(title|type|source_urls|local_files|metadata|created|updated|source_kind|storage|parser|pdf_path|pdf_storage_uri|page_count|ocr_status):", lowered):
            continue
        if line.startswith("---"):
            continue
        if re.match(r"^[-*]\s*(public url|source url|remote image count):", line, re.IGNORECASE):
            continue
        useful_lines.append(re.sub(r"^[-*]\s+", "", line))

    compact = " ".join(useful_lines)
    summary = compact[:360].rsplit(" ", 1)[0] if len(compact) > 360 else compact
    if not summary:
        summary = "原始资料已入库，已生成本地 Wiki 草稿。"

    takeaways = []
    for line in useful_lines[:16]:
        if 12 <= len(line) <= 180 and line not in takeaways:
            takeaways.append(line)
        if len(takeaways) >= 5:
            break

    return {
        "page_type": page_type,
        "title": title or "Untitled",
        "summary": summary,
        "content_json": {
            "compile_status": "heuristic",
            "source_kind": source_kind,
            "problem": summary,
            "key_idea": "这页由本地规则从原始资料中清洗生成，没有经过 LLM 精编。等 LLM 编译服务可用后，可重建为更精炼的 Wiki 页面。",
            "key_takeaways": takeaways,
            "notes": f"原始资料已保留。当前模式：本地规则清洗 fallback（{source_kind}）。",
        },
        "source_level": "",
        "related_topics": [],
    }
