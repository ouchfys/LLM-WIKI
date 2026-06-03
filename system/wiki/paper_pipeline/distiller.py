from __future__ import annotations

import json
import re
import uuid
from typing import Any

from system.wiki.paper_pipeline.models import CandidateClaim, DistilledCandidate, SourcePacket
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore, normalize_alias
from system.wiki.wiki_builder import sanitize_wiki_text


DISTILL_PROMPT = """\
You are the paper knowledge distillation agent for Jarvis Notes.
Input is one paper_pdf SourcePacket. Produce source-grounded reusable wiki
candidates, not a one-off long summary.

Output language:
- Write all free-text explanations in Simplified Chinese.
- Keep paper titles, model names, method names, metrics, datasets, and equations
  in their original language.

Hard rules:
- Return valid JSON only. No markdown fences and no prose outside JSON.
- Do not invent authors, datasets, metrics, numbers, or conclusions.
- Prefer durable ConceptPage and MethodPage cards that can be reused by later papers.
- Do not create cards for incidental details that are only useful inside this paper.
- Every claim must include evidence copied or tightly paraphrased from the source
  and a section_id.
- If evidence is weak, leave fields empty instead of guessing.
- ConceptPage/MethodPage candidates must have aliases.
- candidate_type must be one of: paper_page, concept_card, method_card.
- page_type must be one of: PaperPage, ConceptPage, MethodPage.
- Field quality targets:
  problem: explain the research problem and why it matters.
  key_idea: explain the central insight with enough detail for a user to understand it later.
  method: describe the actual workflow, signals, models, probes, datasets, or evaluation steps when supported.
  results/findings: include only source-supported conclusions, metrics, or qualitative observations.
  limitations: leave empty when evidence is insufficient; do not invent caveats.
  key_takeaways: 2-6 concrete reusable items.
- Write naturally. Paragraphs or line breaks are both acceptable.
- Do not pad content to hit an arbitrary sentence count.
- Do not return a vague one-sentence key_idea or method when the source contains enough detail.

Return this exact JSON shape:
{{
  "paper_page": {{
    "candidate_type": "paper_page",
    "page_type": "PaperPage",
    "title": "...",
    "aliases": [],
    "summary": "...",
    "content_json": {{
      "schema_version": "paper-wiki-v1",
      "compile_status": "llm_refined",
      "problem": "",
      "key_idea": "",
      "method": "",
      "results": "",
      "limitations": "",
      "key_takeaways": [],
      "interview_notes": [],
      "notes": ""
    }},
    "claims": [{{"claim": "...", "evidence": "...", "section_id": "...", "page_start": 0}}],
    "related_topics": [],
    "source_level": "primary"
  }},
  "knowledge_cards": [
    {{
      "candidate_type": "concept_card",
      "page_type": "ConceptPage",
      "title": "...",
      "aliases": ["..."],
      "summary": "...",
      "content_json": {{
        "schema_version": "paper-wiki-v1",
        "compile_status": "llm_refined",
        "definition": "",
        "mechanism": "",
        "method": "",
        "findings": "",
        "limitations": "",
        "key_takeaways": []
      }},
      "claims": [{{"claim": "...", "evidence": "...", "section_id": "...", "page_start": 0}}],
      "related_topics": [],
      "source_level": "primary"
    }}
  ]
}}

Paper title: {title}
Abstract: {abstract}

Source sections:
{sections}
"""


class PaperDistiller:
    def __init__(self, llm=None, store: PaperWikiPipelineStore | None = None):
        self.llm = llm
        self.store = store

    def distill(self, packet: SourcePacket) -> list[DistilledCandidate]:
        candidates = self._llm_distill(packet) if self.llm else []
        if not candidates:
            candidates = self._fallback_candidates(packet)
        candidates.extend(self._seed_reusable_candidates(packet))
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
        prompt = DISTILL_PROMPT.format(
            title=packet.title,
            abstract=sanitize_wiki_text(packet.abstract)[:1200],
            sections=self._sections_for_prompt(packet),
        )
        try:
            raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=4500)
        except Exception as exc:
            print(f"[paper_pipeline.distiller] LLM distill failed: {exc}")
            return []
        payload = parse_json_object(raw)
        if not payload:
            return []
        candidates = []
        paper = payload.get("paper_page")
        if isinstance(paper, dict):
            candidate = self._candidate_from_payload(paper)
            if candidate:
                candidates.append(candidate)
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
        if page_type not in {"PaperPage", "ConceptPage", "MethodPage"}:
            return None
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
                claims.append(CandidateClaim(
                    claim=claim_text,
                    evidence=evidence[:1000],
                    section_id=str(claim.get("section_id") or ""),
                    page_start=int(claim.get("page_start") or 0),
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
            claims=[CandidateClaim(claim=packet.abstract[:240] or packet.title, evidence=evidence["text"], section_id=evidence["section_id"], page_start=evidence["page_start"])],
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
                page_type=page_type,
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

    @staticmethod
    def _sections_for_prompt(packet: SourcePacket) -> str:
        parts = []
        preferred = []
        remaining = []
        for section in packet.sections:
            heading = (section.heading or "").lower()
            text = (section.text or "").lower()
            if any(key in heading for key in ("abstract", "introduction", "method", "approach", "experiment", "result", "conclusion")):
                preferred.append(section)
            elif any(key in text[:500] for key in ("difficulty", "hidden representation", "probe", "probing", "experiment", "result")):
                preferred.append(section)
            else:
                remaining.append(section)
        for section in (preferred + remaining)[:6]:
            text = sanitize_wiki_text(section.text)
            if not text:
                continue
            parts.append(
                f"[section_id={section.section_id}; heading={section.heading}; page={section.page_start}]\n{text[:1000]}"
            )
        return "\n\n---\n\n".join(parts)[:7500]


def _best_evidence(packet: SourcePacket) -> dict[str, Any]:
    for section in packet.sections:
        text = sanitize_wiki_text(section.text)
        if len(text) >= 80:
            return {"text": text[:900], "section_id": section.section_id, "page_start": section.page_start}
    return {"text": sanitize_wiki_text(packet.abstract or packet.title)[:900], "section_id": "abstract", "page_start": 1}


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
