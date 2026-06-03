"""Reviewer for Agentic Wiki Chat evaluation answers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from system.core.config import SILICONFLOW_REVIEW_MODEL

try:
    from system.core.siliconflow_client import SiliconFlowChat
except Exception:
    SiliconFlowChat = None


@dataclass
class ReviewResult:
    answer_confidence: float
    answer_completeness: float
    citation_grounding: float
    tool_routing_correctness: float
    unsupported_claim_risk: float
    final_score: float
    passed: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer_confidence": self.answer_confidence,
            "answer_completeness": self.answer_completeness,
            "citation_grounding": self.citation_grounding,
            "tool_routing_correctness": self.tool_routing_correctness,
            "unsupported_claim_risk": self.unsupported_claim_risk,
            "final_score": self.final_score,
            "passed": self.passed,
            "reason": self.reason,
        }


class AgenticWikiAnswerReviewer:
    REVIEW_RESPONSE_FORMAT = {"type": "json_object"}

    def __init__(self, llm=None):
        self.llm = llm

    @classmethod
    def create_default(cls) -> "AgenticWikiAnswerReviewer":
        if SiliconFlowChat is None:
            return cls(llm=None)
        try:
            return cls(llm=SiliconFlowChat(model=SILICONFLOW_REVIEW_MODEL, temperature=0.0, max_tokens=1200))
        except Exception:
            return cls(llm=None)

    def review(
        self,
        dataset_row: Dict[str, Any],
        answer_payload: Dict[str, Any],
        retrieval_hit_rate: float,
        top1_hit: bool,
    ) -> ReviewResult:
        if self.llm:
            try:
                return self._review_with_llm(dataset_row, answer_payload, retrieval_hit_rate, top1_hit)
            except Exception as exc:
                print(f"[AgenticWikiAnswerReviewer] LLM review failed, fallback used: {exc}")
                return self._fallback_review(
                    dataset_row,
                    answer_payload,
                    retrieval_hit_rate,
                    top1_hit,
                    fallback_reason=f"llm_review_failed: {exc}",
                )
        return self._fallback_review(dataset_row, answer_payload, retrieval_hit_rate, top1_hit)

    def _review_with_llm(
        self,
        dataset_row: Dict[str, Any],
        answer_payload: Dict[str, Any],
        retrieval_hit_rate: float,
        top1_hit: bool,
    ) -> ReviewResult:
        prompt = self._build_prompt(dataset_row, answer_payload, retrieval_hit_rate, top1_hit)
        last_error: Optional[Exception] = None
        raw = ""
        for attempt in range(2):
            try:
                raw = self.llm.invoke(
                    prompt,
                    temperature=0.0,
                    max_tokens=1200,
                    response_format=self.REVIEW_RESPONSE_FORMAT,
                ).strip()
                parsed = self._parse_json(raw)
                return self._normalize_review(parsed, retrieval_hit_rate)
            except Exception as exc:
                last_error = exc
                if raw and attempt == 0:
                    prompt = self._build_repair_prompt(raw)
        raise last_error or RuntimeError("review parse failed")

    def _fallback_review(
        self,
        dataset_row: Dict[str, Any],
        answer_payload: Dict[str, Any],
        retrieval_hit_rate: float,
        top1_hit: bool,
        fallback_reason: str = "",
    ) -> ReviewResult:
        answer = (answer_payload.get("answer") or "").strip()
        citations = answer_payload.get("citations") or []
        tool_plan = answer_payload.get("tool_plan") or {}
        expected_keywords = self._split_pipe(dataset_row.get("expected_keywords", ""))
        web_used = bool(tool_plan.get("use_web"))
        evidence_text = " ".join([
            answer,
            " ".join(str(item.get("title", "")) for item in citations if isinstance(item, dict)),
            " ".join(str(item.get("summary", "")) for item in citations if isinstance(item, dict)),
        ]).lower()
        keyword_hits = sum(1 for item in expected_keywords if self._keyword_present(item, evidence_text))
        keyword_score = keyword_hits / len(expected_keywords) if expected_keywords else 0.5

        answer_confidence = min(1.0, 0.35 + 0.35 * retrieval_hit_rate + 0.30 * keyword_score)
        answer_completeness = min(1.0, 0.30 + 0.40 * keyword_score + 0.30 * (1.0 if len(answer) >= 120 else 0.3))
        citation_grounding = min(1.0, 0.25 + 0.45 * (1.0 if citations else 0.0) + 0.30 * retrieval_hit_rate)
        tool_routing_correctness = self._routing_score(dataset_row, tool_plan, retrieval_hit_rate)
        unsupported_claim_risk = 0.10
        if not citations and retrieval_hit_rate == 0:
            unsupported_claim_risk = 0.55
        elif answer_confidence < 0.45:
            unsupported_claim_risk = 0.45
        if top1_hit:
            answer_confidence = min(1.0, answer_confidence + 0.08)
            citation_grounding = min(1.0, citation_grounding + 0.05)

        final_score = self._weighted_score(
            answer_confidence=answer_confidence,
            answer_completeness=answer_completeness,
            citation_grounding=citation_grounding,
            retrieval_hit_rate=retrieval_hit_rate,
            tool_routing_correctness=tool_routing_correctness,
            unsupported_claim_risk=unsupported_claim_risk,
        )
        passed = final_score >= 0.75 and answer_confidence >= 0.75 and citation_grounding >= 0.65
        reason = (
            f"fallback review; keyword_score={keyword_score:.2f}, retrieval_hit_rate={retrieval_hit_rate:.2f}, "
            f"web_used={web_used}, citations={len(citations)}"
        )
        if fallback_reason:
            reason = f"{reason}; {fallback_reason}"
        return ReviewResult(
            answer_confidence=round(answer_confidence, 4),
            answer_completeness=round(answer_completeness, 4),
            citation_grounding=round(citation_grounding, 4),
            tool_routing_correctness=round(tool_routing_correctness, 4),
            unsupported_claim_risk=round(unsupported_claim_risk, 4),
            final_score=round(final_score, 4),
            passed=passed,
            reason=reason,
        )

    def _build_prompt(
        self,
        dataset_row: Dict[str, Any],
        answer_payload: Dict[str, Any],
        retrieval_hit_rate: float,
        top1_hit: bool,
    ) -> str:
        return (
            "You are reviewing a private Wiki agent answer. Return only strict JSON.\n"
            "The API call uses SiliconFlow response_format={\"type\":\"json_object\"}; comply with it.\n"
            "Judge whether the final answer is trustworthy and supported, not just whether retrieval returned something.\n"
            "Scores are floats in [0,1]. High unsupported_claim_risk means more hallucination risk.\n"
            "Do not penalize Web Search usage by itself. Treat web_used as a diagnostic only; judge answer quality, correctness, completeness, and grounding.\n"
            "Output rules:\n"
            "- Return exactly one JSON object.\n"
            "- No markdown.\n"
            "- No code fence.\n"
            "- No explanation before or after the JSON.\n"
            "- If uncertain, still fill every field with a best-effort score.\n"
            "JSON schema:\n"
            "{"
            "\"answer_confidence\":0.0,"
            "\"answer_completeness\":0.0,"
            "\"citation_grounding\":0.0,"
            "\"tool_routing_correctness\":0.0,"
            "\"unsupported_claim_risk\":0.0,"
            "\"final_score\":0.0,"
            "\"passed\":false,"
            "\"reason\":\"short explanation\""
            "}\n\n"
            f"Dataset row:\n{json.dumps(dataset_row, ensure_ascii=False)}\n\n"
            f"Answer payload:\n{json.dumps(answer_payload, ensure_ascii=False)}\n\n"
            f"Retrieval diagnostics:\n"
            f"- retrieval_hit_rate: {retrieval_hit_rate:.4f}\n"
            f"- top1_hit: {top1_hit}\n"
        )

    @staticmethod
    def _build_repair_prompt(raw_text: str) -> str:
        return (
            "Convert the following reviewer output into exactly one valid JSON object.\n"
            "Return JSON only. No markdown. No explanation.\n"
            "Required keys:\n"
            "[\"answer_confidence\",\"answer_completeness\",\"citation_grounding\","
            "\"tool_routing_correctness\",\"unsupported_claim_risk\",\"final_score\",\"passed\",\"reason\"]\n\n"
            f"Input:\n{raw_text}"
        )

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        cleaned = (text or "").strip()
        tag_match = re.search(r"<json>\s*(\{.*?\})\s*</json>", cleaned, flags=re.S | re.I)
        if tag_match:
            cleaned = tag_match.group(1).strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise ValueError("no json object found")
        return json.loads(match.group(0))

    def _normalize_review(self, data: Dict[str, Any], retrieval_hit_rate: float) -> ReviewResult:
        answer_confidence = self._float01(data.get("answer_confidence"))
        answer_completeness = self._float01(data.get("answer_completeness"))
        citation_grounding = self._float01(data.get("citation_grounding"))
        tool_routing_correctness = self._float01(data.get("tool_routing_correctness"))
        unsupported_claim_risk = self._float01(data.get("unsupported_claim_risk"))
        final_score = data.get("final_score")
        if final_score is None:
            final_score = self._weighted_score(
                answer_confidence=answer_confidence,
                answer_completeness=answer_completeness,
                citation_grounding=citation_grounding,
                retrieval_hit_rate=retrieval_hit_rate,
                tool_routing_correctness=tool_routing_correctness,
                unsupported_claim_risk=unsupported_claim_risk,
            )
        else:
            final_score = self._float01(final_score)
        passed = bool(data.get("passed"))
        reason = str(data.get("reason") or "").strip() or "reviewed"
        return ReviewResult(
            answer_confidence=round(answer_confidence, 4),
            answer_completeness=round(answer_completeness, 4),
            citation_grounding=round(citation_grounding, 4),
            tool_routing_correctness=round(tool_routing_correctness, 4),
            unsupported_claim_risk=round(unsupported_claim_risk, 4),
            final_score=round(final_score, 4),
            passed=passed,
            reason=reason,
        )

    def _routing_score(self, dataset_row: Dict[str, Any], tool_plan: Dict[str, Any], retrieval_hit_rate: float) -> float:
        use_wiki = bool(tool_plan.get("use_wiki"))
        use_web = bool(tool_plan.get("use_web"))
        use_resources = bool(tool_plan.get("use_resources"))
        query_type = str(dataset_row.get("query_type", "")).lower()
        score = 0.25
        if use_wiki:
            score += 0.35
        if use_web:
            score += 0.05
        if "private_wiki" in query_type and not use_web:
            score += 0.10
        if "resource" not in query_type and use_resources:
            score -= 0.10
        if retrieval_hit_rate > 0 and use_wiki:
            score += 0.10
        return max(0.0, min(1.0, score))

    @staticmethod
    def _split_pipe(value: str) -> List[str]:
        return [item.strip() for item in str(value or "").split("|") if item.strip()]

    @staticmethod
    def _keyword_present(keyword: str, text: str) -> bool:
        value = (keyword or "").strip().lower()
        if not value:
            return False
        if value in text:
            return True
        synonyms = {
            "no token generation": ["without generating", "without generating output tokens", "no output token", "无需生成", "不生成", "不用生成"],
            "efficiency": ["efficient", "inference efficiency", "计算效率", "推理效率", "节省", "降低成本"],
            "hidden state": ["hidden states", "hidden representation", "hidden representations", "隐藏状态", "隐藏表示", "内部表示"],
            "frontend": ["front-end", "front end", "前端"],
            "backend": ["back-end", "back end", "后端"],
            "mini-program": ["mini program", "wechat mini program", "微信小程序", "小程序"],
            "framework": ["框架"],
        }
        return any(alias.lower() in text for alias in synonyms.get(value, []))

    @staticmethod
    def _float01(value: Any) -> float:
        try:
            number = float(value)
        except Exception:
            number = 0.0
        return max(0.0, min(1.0, number))

    @staticmethod
    def _weighted_score(
        *,
        answer_confidence: float,
        answer_completeness: float,
        citation_grounding: float,
        retrieval_hit_rate: float,
        tool_routing_correctness: float,
        unsupported_claim_risk: float,
    ) -> float:
        score = (
            0.45 * answer_confidence
            + 0.25 * answer_completeness
            + 0.15 * citation_grounding
            + 0.10 * retrieval_hit_rate
            + 0.05 * tool_routing_correctness
            - 0.10 * unsupported_claim_risk
        )
        return max(0.0, min(1.0, score))
