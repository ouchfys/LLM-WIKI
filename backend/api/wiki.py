import hashlib
import csv
import json
import mimetypes
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from backend.deps import get_chunk_index, get_session_store, get_wiki_chat, get_wiki_store
from backend.api.papers import (
    REPO_ROOT as PAPER_REPO_ROOT,
    UPLOAD_DIR as PAPER_UPLOAD_DIR,
    _resolve_data_pdf as _resolve_paper_data_pdf,
    _run_ingestion_job as _run_paper_ingestion_job,
    _safe_pdf_name as _safe_paper_pdf_name,
)
from system.discovery.xiaohongshu_extractor import XiaohongshuExtractor
from system.document.docling_parser import DoclingParser
from system.document.source_files import download_images
from system.storage import get_object_storage
from system.wiki.raw_source_vault import RawSourceVault
from system.wiki.wiki_builder import WikiBuilder, heuristic_compile_raw_markdown, pending_compiled_content, sanitize_wiki_text
from system.wiki.wiki_chat import WikiChatService
from system.wiki.paper_pipeline.distiller import parse_json_object
from system.wiki.ingestion_jobs import IngestionJobStore
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore, normalize_alias
from system.wiki.wiki_store import WikiStore

REPO_ROOT = _Path(__file__).resolve().parents[2]
RAW_IMAGE_DIR = REPO_ROOT / "data" / "raw_sources" / "images"
EVALUATION_RUNS_DIR = REPO_ROOT / "test" / "evaluation" / "runs"

router = APIRouter()


class CapturePayload(BaseModel):
    title: str
    raw_text: str
    source_url: str = ""
    source_type: str = "inspiration"
    tags: list[str] = Field(default_factory=list)


class WikiChatPayload(BaseModel):
    message: str
    session_id: str = ""
    stream: bool = False


class XhsImportPayload(BaseModel):
    text_or_url: str
    tags: list[str] = Field(default_factory=list)


class SessionCreatePayload(BaseModel):
    title: str = "新会话"


def _page_type(source_type: str) -> str:
    if source_type == "interview":
        return "InterviewQA"
    if source_type == "paper_note":
        return "PaperPage"
    return "SourceNote"


def _summary(text: str, limit: int = 180) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rsplit(" ", 1)[0] + "..."


def _safe_eval_run_path(run_id: str) -> _Path:
    run_id = (run_id or "").strip()
    if not run_id or _Path(run_id).name != run_id:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    run_path = EVALUATION_RUNS_DIR / run_id
    if not run_path.exists() or not run_path.is_dir():
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    return run_path


def _load_eval_summary(run_path: _Path) -> dict[str, Any]:
    summary_path = run_path / "summary.json"
    if not summary_path.exists():
        return {}
    try:
        return json.loads(summary_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read evaluation summary: {exc}") from exc


def _eval_file_time(path: _Path) -> str:
    timestamp = path.stat().st_mtime
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "y"}:
        return True
    if text in {"false", "no", "n", ""}:
        return False
    try:
        return float(text) != 0.0
    except ValueError:
        return False


def _json_field(value: str) -> Any:
    value = (value or "").strip()
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _split_eval_titles(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;|]", value or "") if part.strip()]


def _evaluation_case(row: dict[str, str], include_answer: bool = True) -> dict[str, Any]:
    case = {
        "id": row.get("id", ""),
        "query": row.get("query", ""),
        "query_type": row.get("query_type", ""),
        "difficulty": row.get("difficulty", ""),
        "expected_source": row.get("expected_source", ""),
        "expected_card_titles": _split_eval_titles(row.get("expected_card_titles", "")),
        "allow_web": _as_bool(row.get("allow_web")),
        "latency_seconds": _as_float(row.get("latency_seconds")),
        "retrieval_hit": _as_bool(row.get("retrieval_hit")),
        "top1_hit": _as_bool(row.get("top1_hit")),
        "web_allowed": _as_bool(row.get("web_allowed")),
        "web_used": _as_bool(row.get("web_used")),
        "tool_routing_correct": _as_bool(row.get("tool_routing_correct")),
        "answer_confidence": _as_float(row.get("answer_confidence")),
        "answer_completeness": _as_float(row.get("answer_completeness")),
        "citation_grounding": _as_float(row.get("citation_grounding")),
        "unsupported_claim_risk": _as_float(row.get("unsupported_claim_risk")),
        "final_score": _as_float(row.get("final_score")),
        "reviewer_reason": row.get("reviewer_reason", ""),
        "failure_bucket": _failure_bucket(row),
    }
    if include_answer:
        case.update(
            {
                "answer": row.get("answer", ""),
                "citations": _json_field(row.get("citations", "")),
                "resources": _json_field(row.get("resources", "")),
                "tool_plan": _json_field(row.get("tool_plan", "")),
            }
        )
    return case


def _failure_bucket(row: dict[str, str]) -> str:
    final_score = _as_float(row.get("final_score"))
    if final_score >= 0.85:
        return "ok"
    if not _as_bool(row.get("retrieval_hit")):
        return "retrieval_miss"
    if not _as_bool(row.get("top1_hit")):
        return "weak_top1"
    if _as_float(row.get("citation_grounding")) < 0.8:
        return "weak_grounding"
    if _as_float(row.get("answer_confidence")) < 0.8:
        return "low_confidence"
    return "answer_quality"


def _read_evaluation_cases(run_path: _Path, include_answer: bool = True) -> list[dict[str, Any]]:
    cases_path = run_path / "queries_details.csv"
    if not cases_path.exists():
        return []
    try:
        with cases_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [_evaluation_case(row, include_answer=include_answer) for row in csv.DictReader(handle)]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read evaluation cases: {exc}") from exc


def _case_metric_splits(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        key = case.get("expected_source") or "unknown"
        groups.setdefault(str(key), []).append(case)

    result = []
    for source, items in groups.items():
        count = len(items)
        result.append(
            {
                "source": source,
                "count": count,
                "final_score": round(sum(item["final_score"] for item in items) / count, 4),
                "answer_confidence": round(sum(item["answer_confidence"] for item in items) / count, 4),
                "citation_grounding": round(sum(item["citation_grounding"] for item in items) / count, 4),
                "retrieval_hit_rate": round(sum(1 for item in items if item["retrieval_hit"]) / count, 4),
                "top1_hit_rate": round(sum(1 for item in items if item["top1_hit"]) / count, 4),
            }
        )
    return sorted(result, key=lambda item: (item["final_score"], item["source"]))


def _low_score_cases(cases: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    preview_fields = [
        "id",
        "query",
        "expected_source",
        "final_score",
        "answer_confidence",
        "citation_grounding",
        "retrieval_hit",
        "top1_hit",
        "web_used",
        "latency_seconds",
        "failure_bucket",
        "reviewer_reason",
    ]
    items = sorted(cases, key=lambda item: (item["final_score"], item["id"]))[:limit]
    return [{key: item.get(key) for key in preview_fields} for item in items]


def _evaluation_run_item(run_path: _Path) -> dict[str, Any]:
    summary = _load_eval_summary(run_path)
    cases_path = run_path / "queries_details.csv"
    case_count = 0
    if cases_path.exists():
        try:
            with cases_path.open("r", encoding="utf-8-sig", newline="") as handle:
                case_count = sum(1 for _ in csv.DictReader(handle))
        except Exception:
            case_count = 0
    return {
        "id": run_path.name,
        "updated_at": _eval_file_time(run_path),
        "has_summary": bool(summary),
        "case_count": int(summary.get("count") or case_count or 0),
        "overall_final_score": _as_float(summary.get("overall_final_score")),
        "overall_answer_confidence": _as_float(summary.get("overall_answer_confidence")),
        "overall_citation_grounding": _as_float(summary.get("overall_citation_grounding")),
        "retrieval_hit_rate": _as_float(summary.get("retrieval_hit_rate")),
        "top1_hit_rate": _as_float(summary.get("top1_hit_rate")),
        "pass_rate": _as_float(summary.get("pass_rate")),
    }



def _split_tags(raw: str) -> list[str]:
    return [part.strip() for part in re.split("[,\uFF0C]", raw or "") if part.strip()]


def _safe_filename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-() " else "_" for ch in (name or "file"))
    return cleaned.strip()[:120] or "file"


def _source_id(title: str, source_url: str, payload_hint: str = "") -> str:
    basis = payload_hint or source_url or title or "source"
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:16]


def _looks_like_base64_blob(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 256:
        return False
    allowed = sum(1 for ch in compact if ch.isalnum() or ch in "+/=_-")
    return allowed / max(len(compact), 1) > 0.9


def _sanitize_ocr_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return ""

    text = re.sub(r"\u0000", "", text)
    text = re.sub(r"!\[[^\]]*\]\(data:image/[^)]+\)", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\b(?:小红书|xhslink|xhscdn)\b.*$", "", text, flags=re.IGNORECASE | re.MULTILINE)

    cleaned_lines: list[str] = []
    prev_line = ""
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            prev_line = ""
            continue
        if line.lower().startswith("data:image/"):
            continue
        if _looks_like_base64_blob(line):
            continue
        if re.fullmatch(r"[A-Za-z0-9+/=]{256,}", re.sub(r"\s+", "", line)):
            continue
        compact = re.sub(r"\s+", "", line)
        if len(compact) < 3 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", line):
            continue
        if line == prev_line:
            continue
        cleaned_lines.append(line)
        prev_line = line

    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _visible_ocr_excerpt(text: str, limit: int = 480) -> str:
    return _summary(_sanitize_ocr_text(text), limit=limit)


def _read_raw_markdown(raw_source_path: str) -> str:
    if not raw_source_path:
        return ""
    if raw_source_path.startswith(("oss://", "local://")):
        try:
            return get_object_storage().read_text(raw_source_path)
        except Exception as exc:
            print(f"[wiki] raw source storage read failed for {raw_source_path}: {exc}")
            return ""
    path = _Path(raw_source_path)
    if not path.is_absolute():
        path = REPO_ROOT / raw_source_path
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _read_storage_bytes(reference: str) -> bytes:
    reference = (reference or "").strip()
    if not reference:
        return b""

    storage = get_object_storage()
    if reference.startswith("oss://"):
        key = storage.key_from_uri(reference)
        if storage.enabled:
            try:
                return storage._get_bucket().get_object(key).read()
            except Exception:
                cached = storage.local_cache_path_for_key(key)
                if cached and cached.exists() and cached.is_file():
                    return cached.read_bytes()
                raise
        cached = storage.local_cache_path_for_key(key)
        return cached.read_bytes() if cached and cached.exists() and cached.is_file() else b""

    if reference.startswith("local://"):
        cached = storage.local_cache_path_for_key(reference)
        return cached.read_bytes() if cached and cached.exists() and cached.is_file() else b""

    path = _Path(reference)
    if not path.is_absolute():
        path = REPO_ROOT / reference
    if path.exists() and path.is_file():
        return path.read_bytes()
    return b""


def _compile_raw_wiki_page(
    title: str,
    raw_source_path: str,
    page_type: str,
    source_kind: str,
) -> Optional[dict]:
    try:
        from backend.deps import get_summary_llm
        llm = get_summary_llm()
    except Exception as exc:
        print(f"[wiki] Wiki compiler LLM unavailable: {exc}")
        return None
    raw_markdown = sanitize_wiki_text(_read_raw_markdown(raw_source_path))
    if not raw_markdown:
        return None
    if not llm:
        return heuristic_compile_raw_markdown(
            title=title,
            raw_markdown=raw_markdown,
            page_type=page_type,
            source_kind=source_kind,
        )


    try:
        return WikiBuilder(llm).build_from_raw_markdown(
            title=title,
            raw_markdown=raw_markdown,
            page_type=page_type,
            source_kind=source_kind,
        )
    except Exception as exc:
        print(f"[wiki] Wiki compile failed: {exc}")
        return heuristic_compile_raw_markdown(
            title=title,
            raw_markdown=raw_markdown,
            page_type=page_type,
            source_kind=source_kind,
        )


def _merge_operational_fields(content: dict, fields: dict) -> dict:
    merged = dict(content or {})
    for key, value in fields.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _compiled_or_pending_content(
    compiled: Optional[dict],
    raw_source_path: str,
    source_kind: str,
    operational_fields: dict,
    fallback_summary: str = "",
    fallback_content: Optional[dict] = None,
    fallback_related: Optional[list[str]] = None,
) -> tuple[str, dict, list[str], str]:
    if _compiled_wiki_page_is_useful(compiled):
        content = _merge_operational_fields(compiled.get("content_json") or {}, operational_fields)
        summary = (
            sanitize_wiki_text(compiled.get("summary", ""))
            or sanitize_wiki_text(str(content.get("problem") or content.get("key_idea") or content.get("notes") or ""))
        )[:240]
        related = compiled.get("related_topics") or operational_fields.get("tags") or []
        source_level = compiled.get("source_level", "")
        return summary, content, related, source_level

    if fallback_content:
        content = _merge_operational_fields(fallback_content, operational_fields)
        summary = (
            sanitize_wiki_text(fallback_summary)
            or sanitize_wiki_text(str(content.get("tldr") or content.get("problem") or content.get("ideal_answer") or content.get("notes") or ""))
        )[:240]
        return summary, content, fallback_related or operational_fields.get("tags") or [], "secondary"

    content = _merge_operational_fields(
        pending_compiled_content(
            raw_source_path=raw_source_path,
            source_kind=source_kind,
            reason="wiki compile failed or LLM unavailable",
        ),
        operational_fields,
    )
    return "原始资料已入库，等待 Wiki 编译生成可读页面。", content, operational_fields.get("tags") or [], ""


def _compiled_wiki_page_is_useful(compiled: Optional[dict]) -> bool:
    if not isinstance(compiled, dict):
        return False
    summary = sanitize_wiki_text(compiled.get("summary") or "")
    if len(summary) >= 30:
        return True
    content = compiled.get("content_json") or {}
    if not isinstance(content, dict):
        return False
    useful_fields = 0
    for key, value in content.items():
        if str(key).startswith("_") or key in {
            "raw_source_path",
            "source_type",
            "source_kind",
            "attachments",
            "image_urls",
            "downloaded_images",
            "compile_status",
            "compile_error",
        }:
            continue
        if len(sanitize_wiki_text(_stringify_wiki_value(value))) >= 30:
            useful_fields += 1
    return useful_fields >= 1


def _stringify_wiki_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify_wiki_value(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_stringify_wiki_value(item) for item in value.values())
    return str(value)


def _takeaways_from_text(text: str, limit: int = 6) -> list[str]:
    cleaned = sanitize_wiki_text(text or "")
    candidates: list[str] = []
    for line in re.split(r"[\n。！？!?；;]+", cleaned):
        line = re.sub(r"^[-*•\d.\s]+", "", line).strip()
        if 8 <= len(line) <= 180 and line not in candidates:
            candidates.append(line)
        if len(candidates) >= limit:
            break
    if not candidates and cleaned:
        candidates.append(_summary(cleaned, limit=160))
    return candidates


def _build_xhs_schema_page(
    title: str,
    source_url: str,
    note_text: str,
    cleaned_ocr: str,
    structured_ocr: str,
    tags: list[str],
    raw_source_path: str,
) -> tuple[str, dict, list[str]]:
    body = "\n\n".join(part for part in [note_text, structured_ocr or cleaned_ocr] if part).strip()
    takeaways = _takeaways_from_text(body)
    tldr = _summary(body or title, limit=220)
    content = {
        "schema_version": "xhs-lite-v1",
        "compile_status": "direct_source",
        "source_type": "xiaohongshu",
        "source_url": source_url,
        "title": title,
        "content": note_text[:1200],
        "ocr_excerpt": _visible_ocr_excerpt(cleaned_ocr),
        "image_notes": structured_ocr,
        "key_points": takeaways,
        "notes": body[:1200],
        "tags": tags,
        "raw_source_path": raw_source_path,
    }
    related = list(dict.fromkeys((tags or []) + ["小红书", "面经"]))
    return tldr, content, related


XHS_DISTILL_PROMPT = """\
You are the Xiaohongshu interview-note distillation agent for Jarvis Notes.

Goal:
- Convert a short social/interview note into a useful InterviewQA knowledge page.
- Preserve factual interview questions and technical topics.
- Do not invent company details, results, or answers that are not supported.
- If the note is mostly a job interview list, organize it for interview preparation.
- Keep Chinese output when the source is Chinese.

Return ONLY valid JSON with this exact shape:
{{
  "summary": "120-220 Chinese characters, useful overview",
  "content_json": {{
    "schema_version": "xhs-interview-v1",
    "compile_status": "llm_distilled",
    "source_type": "xiaohongshu",
    "question_context": "What this note is about and why it matters.",
    "core_points": ["3-6 concrete technical themes or takeaways"],
    "interview_questions": ["4-10 extracted interview questions or likely variants"],
    "answer_frame": ["3-6 concise preparation directions, based only on the source"],
    "learning_value": "How this source connects to paper/concept/method review.",
    "tags": ["short topical tags"]
  }},
  "related_topics": ["LLM", "Transformer", "Agent", "..."],
  "review_hints": ["possible uncertainty or source limitation"]
}}

Source title:
{title}

Source URL:
{source_url}

User tags:
{tags}

Public note text:
{note_text}

OCR text from images:
{ocr_text}
"""


XHS_REVIEW_PROMPT = """\
You are the reviewer agent for a Xiaohongshu-to-Wiki card.

Review whether the distilled card is useful, grounded, and safe to merge into Jarvis Notes.
Reject if it invents unsupported facts, is too generic, lacks interview questions, or contains raw JSON/noisy clipboard text.

Return ONLY valid JSON:
{{
  "status": "approved" | "rejected",
  "errors": ["short reasons"],
  "confidence": 0.0
}}

Source excerpt:
{source_excerpt}

Distilled card:
{card}
"""


def _compile_xhs_distilled_page(
    title: str,
    source_url: str,
    note_text: str,
    cleaned_ocr: str,
    structured_ocr: str,
    tags: list[str],
    raw_source_path: str,
) -> Optional[tuple[str, dict, list[str], dict]]:
    try:
        from backend.deps import get_summary_llm
        llm = get_summary_llm()
    except Exception as exc:
        print(f"[wiki] XHS distiller LLM unavailable: {exc}")
        return None
    if not llm:
        return None

    ocr_text = structured_ocr or cleaned_ocr
    prompt = XHS_DISTILL_PROMPT.format(
        title=sanitize_wiki_text(title)[:200],
        source_url=source_url,
        tags=", ".join(tags or []),
        note_text=sanitize_wiki_text(note_text)[:4500],
        ocr_text=sanitize_wiki_text(ocr_text)[:1600],
    )
    try:
        raw = llm.invoke(prompt, temperature=0.0, max_tokens=2600)
    except Exception as exc:
        print(f"[wiki] XHS distill failed: {exc}")
        return None

    payload = parse_json_object(raw)
    if not isinstance(payload, dict):
        return None

    content = payload.get("content_json")
    if not isinstance(content, dict):
        return None

    content = _normalize_xhs_distilled_content(
        content=content,
        title=title,
        source_url=source_url,
        raw_source_path=raw_source_path,
        tags=tags,
        compiler_model=getattr(llm, "model", ""),
    )
    summary = sanitize_wiki_text(str(payload.get("summary") or ""))[:260]
    related = [
        sanitize_wiki_text(str(topic))
        for topic in payload.get("related_topics") or []
        if sanitize_wiki_text(str(topic))
    ]
    review_hints = [
        sanitize_wiki_text(str(item))
        for item in payload.get("review_hints") or []
        if sanitize_wiki_text(str(item))
    ]
    review = _review_xhs_distilled_page(
        source_text="\n\n".join(part for part in [note_text, ocr_text] if part),
        summary=summary,
        content=content,
    )
    if review.get("status") != "approved":
        print(f"[wiki] XHS reviewer rejected distilled card: {review}")
        return None

    content["review_status"] = review.get("status", "approved")
    content["review_confidence"] = review.get("confidence", 0.0)
    if review_hints:
        content["review_hints"] = review_hints
    return summary, content, related or _xhs_topic_terms("\n".join([title, note_text, ocr_text])), review


def _normalize_xhs_distilled_content(
    content: dict,
    title: str,
    source_url: str,
    raw_source_path: str,
    tags: list[str],
    compiler_model: str,
) -> dict:
    normalized = dict(content or {})
    normalized["schema_version"] = "xhs-interview-v1"
    normalized["compile_status"] = "llm_distilled"
    normalized["source_type"] = "xiaohongshu"
    normalized["source_url"] = source_url
    normalized["title"] = title
    normalized["raw_source_path"] = raw_source_path
    normalized["compiler_model"] = compiler_model
    normalized["pipeline"] = "xhs_four_agent"
    normalized["extractor_agent"] = "xiaohongshu_public_meta+ocr"
    normalized["distiller_agent"] = compiler_model or "summary_llm"
    normalized["reviewer_agent"] = "xhs_reviewer"
    normalized["merge_agent"] = "known_card_linker"
    normalized["tags"] = list(dict.fromkeys([*(tags or []), *arrayish(normalized.get("tags"))]))

    for key in ("core_points", "interview_questions", "answer_frame"):
        items = [sanitize_wiki_text(str(item)) for item in arrayish(normalized.get(key)) if sanitize_wiki_text(str(item))]
        normalized[key] = items[:10]
    for key in ("question_context", "learning_value"):
        normalized[key] = sanitize_wiki_text(str(normalized.get(key) or ""))
    return normalized


def _review_xhs_distilled_page(source_text: str, summary: str, content: dict) -> dict:
    deterministic = _deterministic_xhs_review(summary=summary, content=content)
    if deterministic.get("status") != "approved":
        return deterministic

    try:
        from backend.deps import get_review_llm
        review_llm = get_review_llm()
    except Exception as exc:
        print(f"[wiki] XHS review LLM unavailable: {exc}")
        return deterministic
    if not review_llm:
        return deterministic

    prompt = XHS_REVIEW_PROMPT.format(
        source_excerpt=sanitize_wiki_text(source_text)[:1800],
        card=json.dumps({"summary": summary, "content_json": content}, ensure_ascii=False, indent=2)[:3000],
    )
    try:
        raw = review_llm.invoke(prompt, temperature=0.0, max_tokens=900)
    except Exception as exc:
        print(f"[wiki] XHS review failed: {exc}")
        return deterministic
    payload = parse_json_object(raw)
    if not isinstance(payload, dict):
        return deterministic
    status = str(payload.get("status") or "").lower()
    if status not in {"approved", "rejected"}:
        return deterministic
    return {
        "status": status,
        "errors": [sanitize_wiki_text(str(item)) for item in payload.get("errors") or []],
        "confidence": float(payload.get("confidence") or deterministic.get("confidence") or 0.6),
    }


def _deterministic_xhs_review(summary: str, content: dict) -> dict:
    errors: list[str] = []
    if len(sanitize_wiki_text(summary)) < 30:
        errors.append("summary too short")
    if len(sanitize_wiki_text(str(content.get("question_context") or ""))) < 20:
        errors.append("question_context too short")
    if len(arrayish(content.get("core_points"))) < 2:
        errors.append("not enough core_points")
    if len(arrayish(content.get("interview_questions"))) < 2:
        errors.append("not enough interview_questions")
    noisy = " ".join([
        summary,
        sanitize_wiki_text(str(content.get("question_context") or "")),
        " ".join(str(item) for item in arrayish(content.get("core_points"))),
    ])
    if "xsec_token" in noisy or "source=webshare" in noisy or noisy.strip().startswith("{"):
        errors.append("contains raw clipboard/url noise")
    return {
        "status": "rejected" if errors else "approved",
        "errors": errors,
        "confidence": 0.78 if not errors else 0.35,
    }


def arrayish(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _link_xhs_card_to_known_cards(
    card_id: str,
    title: str,
    text: str,
    raw_source_path: str,
    source_url: str,
    store: WikiStore,
    limit: int = 8,
) -> list[dict[str, str]]:
    pipeline_store = PaperWikiPipelineStore(db_path=store.db_path)
    normalized_text = normalize_alias(text)
    if not normalized_text:
        return []

    linked: list[dict[str, str]] = []
    seen_cards: set[str] = set()
    aliases = pipeline_store.list_aliases()
    for alias in aliases:
        target_id = str(alias.get("card_id") or "")
        page_type = str(alias.get("page_type") or "")
        alias_text = str(alias.get("alias") or "")
        normalized_alias = normalize_alias(alias_text)
        compact_alias = normalized_alias.replace(" ", "")
        if (
            not target_id
            or target_id == card_id
            or target_id in seen_cards
            or page_type not in {"ConceptPage", "MethodPage"}
            or len(compact_alias) < 4
        ):
            continue
        if normalized_alias not in normalized_text:
            continue

        target = store.get_card(target_id)
        if not target:
            continue
        evidence = _evidence_window(text, alias_text)
        _add_xhs_card_relation(
            pipeline_store=pipeline_store,
            from_card_id=card_id,
            target_id=target_id,
            raw_source_path=raw_source_path,
            source_url=source_url,
            evidence_text=evidence or alias_text,
            claim_text=f"小红书面经提到 {target.get('title', alias_text)}",
            relation_type="mentions",
            confidence=0.55,
        )
        seen_cards.add(target_id)
        linked.append({
            "id": target_id,
            "title": target.get("title", alias_text),
            "page_type": page_type,
            "alias": alias_text,
            "relation_type": "mentions",
        })
        if len(linked) >= limit:
            break
    if len(linked) >= limit:
        return linked

    alias_terms_by_card: dict[str, list[str]] = {}
    for alias in aliases:
        target_id = str(alias.get("card_id") or "")
        if not target_id:
            continue
        alias_terms_by_card.setdefault(target_id, []).append(str(alias.get("alias") or ""))

    topic_terms = _xhs_topic_terms(text)
    if not topic_terms:
        return linked

    for target in _iter_knowledge_cards(store, limit=500):
        target_id = str(target.get("id") or "")
        page_type = str(target.get("page_type") or "")
        if (
            not target_id
            or target_id == card_id
            or target_id in seen_cards
            or page_type not in {"ConceptPage", "MethodPage"}
        ):
            continue

        matched_term = _match_xhs_topic_to_card(
            target=target,
            aliases=alias_terms_by_card.get(target_id, []),
            topic_terms=topic_terms,
            normalized_text=normalized_text,
        )
        if not matched_term:
            continue

        evidence = _evidence_window(text, matched_term)
        _add_xhs_card_relation(
            pipeline_store=pipeline_store,
            from_card_id=card_id,
            target_id=target_id,
            raw_source_path=raw_source_path,
            source_url=source_url,
            evidence_text=evidence or _summary(text, limit=240),
            claim_text=f"小红书面经与 {target.get('title', matched_term)} 主题相关",
            relation_type="topic_related",
            confidence=0.42,
        )
        seen_cards.add(target_id)
        linked.append({
            "id": target_id,
            "title": target.get("title", matched_term),
            "page_type": page_type,
            "alias": matched_term,
            "relation_type": "topic_related",
        })
        if len(linked) >= limit:
            break
    return linked


def _add_xhs_card_relation(
    pipeline_store: PaperWikiPipelineStore,
    from_card_id: str,
    target_id: str,
    raw_source_path: str,
    source_url: str,
    evidence_text: str,
    claim_text: str,
    relation_type: str,
    confidence: float,
) -> None:
    pipeline_store.add_card_link(
        from_card_id=from_card_id,
        to_card_id=target_id,
        relation_type=relation_type,
        source_packet_id=from_card_id,
        evidence_text=evidence_text,
    )
    pipeline_store.add_card_source(
        card_id=target_id,
        source_packet_id=from_card_id,
        source_card_id=from_card_id,
        raw_source_path=raw_source_path,
        source_url=source_url,
        section_id="xiaohongshu",
        evidence_text=evidence_text,
        claim_text=claim_text,
        confidence=confidence,
    )


def _iter_knowledge_cards(store: WikiStore, limit: int = 500) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for page_type in ("ConceptPage", "MethodPage"):
        cards.extend(store.list_cards(page_type=page_type, limit=limit))
    return cards


def _xhs_topic_terms(text: str) -> list[str]:
    normalized = normalize_alias(text)
    compact = normalized.replace(" ", "")
    rules = [
        ("LLM", ["llm", "large language model", "large language models", "大模型", "大语言模型"]),
        ("Transformer", ["transformer", "encoder", "decoder"]),
        ("Attention", ["attention", "注意力", "self attention", "scaled dot product"]),
        ("RMSNorm", ["rmsnorm", "root mean square layer normalization"]),
        ("LayerNorm", ["layernorm", "layer norm", "layer normalization", "层归一化"]),
        ("BatchNorm", ["batchnorm", "batch norm", "batch normalization", "批归一化"]),
        ("Normalization", ["normalization", "归一化", "标准化"]),
        ("Pre-LN", ["pre ln", "preln", "pre layernorm"]),
        ("Post-LN", ["post ln", "postln", "post layernorm"]),
        ("LoRA", ["lora", "low rank adaptation"]),
        ("RAG", ["rag", "retrieval augmented generation", "检索增强"]),
        ("Agent", ["agent", "multi agent", "多agent", "智能体"]),
        ("Qwen", ["qwen", "通义千问"]),
        ("Megatron", ["megatron"]),
        ("FFN", ["ffn", "feed forward", "前馈网络"]),
    ]
    terms: list[str] = []
    for canonical, synonyms in rules:
        for synonym in synonyms:
            norm_synonym = normalize_alias(synonym)
            if norm_synonym and (norm_synonym in normalized or norm_synonym.replace(" ", "") in compact):
                terms.append(canonical)
                break
    return list(dict.fromkeys(terms))


GENERIC_XHS_TOPIC_TERMS = {"LLM"}


def _match_xhs_topic_to_card(
    target: dict[str, Any],
    aliases: list[str],
    topic_terms: list[str],
    normalized_text: str,
) -> str:
    strict_fields = [str(target.get("title") or "")]
    strict_fields.extend(str(alias) for alias in aliases)
    strict_terms = " ".join(normalize_alias(field) for field in strict_fields if field)
    compact_strict = strict_terms.replace(" ", "")

    broad_fields = [*strict_fields, *(str(topic) for topic in (target.get("related_topics") or []))]
    broad_terms = " ".join(normalize_alias(field) for field in broad_fields if field)
    compact_broad = broad_terms.replace(" ", "")

    for term in topic_terms:
        if term in GENERIC_XHS_TOPIC_TERMS:
            continue
        normalized_term = normalize_alias(term)
        compact_term = normalized_term.replace(" ", "")
        if normalized_term in strict_terms or compact_term in compact_strict:
            return term

    non_generic_matches: list[str] = []
    for term in topic_terms:
        if term in GENERIC_XHS_TOPIC_TERMS:
            continue
        normalized_term = normalize_alias(term)
        compact_term = normalized_term.replace(" ", "")
        if normalized_term and (normalized_term in broad_terms or compact_term in compact_broad):
            non_generic_matches.append(term)
    if len(set(non_generic_matches)) >= 2:
        return non_generic_matches[0]

    title_term = normalize_alias(str(target.get("title") or ""))
    compact_title = title_term.replace(" ", "")
    if len(compact_title) >= 4 and (title_term in normalized_text or compact_title in normalized_text.replace(" ", "")):
        return str(target.get("title") or "")
    return ""


def _evidence_window(text: str, alias: str, radius: int = 120) -> str:
    text = sanitize_wiki_text(text or "")
    alias = (alias or "").strip()
    if not text or not alias:
        return ""
    index = text.lower().find(alias.lower())
    if index < 0:
        return _summary(text, limit=240)
    start = max(0, index - radius)
    end = min(len(text), index + len(alias) + radius)
    return text[start:end].strip()


def _structure_ocr_text(text: str, max_lines: int = 12) -> str:
    text = _sanitize_ocr_text(text)
    if not text:
        return ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    bullets = []
    for line in lines[:max_lines]:
        line = re.sub(r"^[-*•\d.\s]+", "", line).strip()
        if len(line) < 2:
            continue
        if line not in bullets:
            bullets.append(line)

    if not bullets:
        return _summary(text, limit=800)

    if len(bullets) >= 3:
        return "\n".join(f"- {item}" for item in bullets[:max_lines])

    return _summary(text, limit=800)


def _build_attachment_section(local_files: list[str]) -> list[str]:
    if not local_files:
        return []
    lines = ["## Attachments", ""]
    lines.extend(f"- {path}" for path in local_files)
    lines.append("")
    return lines


def _ocr_images(local_paths: list[str]) -> tuple[str, str, int, int]:
    if not local_paths:
        return "", "not_needed", 0, 0

    parser = DoclingParser()
    if not parser.available:
        return "", "pending", 0, len(local_paths)

    texts: list[str] = []
    failed = 0
    for local_rel in local_paths:
        local_path = REPO_ROOT / local_rel
        try:
            parsed = parser.parse_file(str(local_path))
            extracted = _sanitize_ocr_text(parsed.text or parsed.markdown or "")
            if extracted:
                texts.append(extracted)
            else:
                failed += 1
        except Exception as exc:
            print(f"[wiki] OCR failed for {local_rel}: {exc}")
            failed += 1

    if not texts:
        return "", "failed", 0, failed
    if failed:
        return "\n\n---\n\n".join(texts), "partial", len(texts), failed
    return "\n\n---\n\n".join(texts), "done", len(texts), 0


def _write_text_raw_markdown(
    title: str,
    raw_text: str,
    source_type: str,
    source_url: str,
    tags: list[str],
) -> str:
    lines = [f"# {title}", "", raw_text.strip(), ""]
    if tags:
        lines.extend(["## Tags", ""])
        lines.extend(f"- {tag}" for tag in tags)
        lines.append("")
    return RawSourceVault().write_source(
        source_kind="text_note",
        title=title,
        body_markdown="\n".join(lines),
        metadata={"source_type": source_type},
        source_urls=[source_url] if source_url else [],
        slug_hint=_source_id(title, source_url, raw_text[:120]),
    )


def _write_xhs_raw_markdown(
    title: str,
    source_url: str,
    note_text: str,
    image_urls: list[str],
    downloaded_images: list[str],
    ocr_text: str,
    ocr_status: str,
    tags: list[str],
    slug_hint: str,
) -> str:
    lines = [f"# {title}", "", "## Source", ""]
    if source_url:
        lines.append(f"- Public URL: {source_url}")
    if image_urls:
        lines.append(f"- Remote image count: {len(image_urls)}")
    lines.append("")
    lines.extend(["## Share Text", "", note_text.strip() or "(empty)", ""])
    if ocr_text.strip():
        lines.extend(["## OCR Text", "", ocr_text.strip(), ""])
    if tags:
        lines.extend(["## Tags", ""])
        lines.extend(f"- {tag}" for tag in tags)
        lines.append("")
    lines.extend(_build_attachment_section(downloaded_images))
    return RawSourceVault().write_source(
        source_kind="xiaohongshu_note",
        title=title,
        body_markdown="\n".join(lines),
        metadata={
            "ocr_status": ocr_status,
            "image_count": len(image_urls or []),
        },
        source_urls=[source_url] if source_url else [],
        local_files=downloaded_images,
        slug_hint=slug_hint,
    )


def _write_image_raw_markdown(
    title: str,
    source_url: str,
    source_type: str,
    tags: list[str],
    local_files: list[str],
    ocr_text: str,
    ocr_status: str,
) -> str:
    lines = [f"# {title}", "", "## Source", ""]
    if source_url:
        lines.append(f"- Source URL: {source_url}")
    lines.append("")
    if ocr_text.strip():
        lines.extend(["## OCR Text", "", ocr_text.strip(), ""])
    else:
        lines.extend(["## OCR Text", "", "(pending or empty)", ""])
    if tags:
        lines.extend(["## Tags", ""])
        lines.extend(f"- {tag}" for tag in tags)
        lines.append("")
    lines.extend(_build_attachment_section(local_files))
    return RawSourceVault().write_source(
        source_kind="image_note",
        title=title,
        body_markdown="\n".join(lines),
        metadata={
            "source_type": source_type,
            "ocr_status": ocr_status,
            "image_count": len(local_files),
        },
        source_urls=[source_url] if source_url else [],
        local_files=local_files,
        slug_hint=_source_id(title, source_url, "".join(local_files)),
    )


def _store_image_uploads(files: list[UploadFile], source_id: str) -> list[str]:
    if not files:
        raise HTTPException(status_code=400, detail="No image files provided.")

    dest_dir = RAW_IMAGE_DIR / source_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[str] = []
    for index, file in enumerate(files):
        if not file.filename:
            continue
        suffix = _Path(file.filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            raise HTTPException(status_code=400, detail=f"Unsupported image type: {file.filename}")
        dest = dest_dir / f"{index:02d}-{_safe_filename(file.filename)}"
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        get_object_storage().upload_file(dest, content_type=file.content_type or "")
        saved_paths.append(str(dest.relative_to(REPO_ROOT)))

    if not saved_paths:
        raise HTTPException(status_code=400, detail="No valid image files were uploaded.")
    return saved_paths


@router.get("")
def list_wiki(
    query: str = "",
    page_type: Optional[str] = None,
    store: WikiStore = Depends(get_wiki_store),
):
    if query:
        cards = store.search_cards(query, limit=100)
    elif page_type and page_type != "all":
        cards = store.get_cards_by_type(page_type, limit=100)
    else:
        cards = store.get_recent_cards(limit=100)
    for card in cards:
        card["obsidian_uri"] = store.vault.card_uri(card.get("markdown_path", ""))
    return {"items": cards, "counts": store.count_by_type()}


@router.get("/vault/info")
def get_vault_info(store: WikiStore = Depends(get_wiki_store)):
    return store.vault.vault_info()


@router.get("/graph")
def get_wiki_graph(store: WikiStore = Depends(get_wiki_store)):
    cards = store.get_recent_cards(limit=500)
    nodes = []
    links = []
    topic_to_cards: dict[str, list[str]] = {}

    for card in cards:
        card_id = card["id"]
        nodes.append({
            "id": card_id,
            "label": card["title"],
            "type": card["page_type"],
            "size": 18 + min(len(card.get("related_topics") or []) * 2, 10),
        })
        for topic in card.get("related_topics") or []:
            topic_key = topic.strip()
            if topic_key:
                topic_to_cards.setdefault(topic_key, []).append(card_id)

    for topic, card_ids in topic_to_cards.items():
        topic_id = f"topic:{topic}"
        nodes.append({
            "id": topic_id,
            "label": topic,
            "type": "Topic",
            "size": 14 + min(len(card_ids) * 2, 12),
        })
        for card_id in card_ids:
            links.append({"source": card_id, "target": topic_id, "label": "tag"})

    return {"nodes": nodes, "links": links}


@router.get("/aliases")
def list_wiki_aliases(store: WikiStore = Depends(get_wiki_store)):
    items = PaperWikiPipelineStore(db_path=store.db_path).list_aliases()
    return {"items": items}


@router.get("/merge-audit")
def list_merge_audit(
    source_packet_id: str = "",
    card_id: str = "",
    limit: int = 100,
    store: WikiStore = Depends(get_wiki_store),
):
    bounded_limit = max(1, min(limit, 500))
    items = PaperWikiPipelineStore(db_path=store.db_path).list_merge_audit(
        source_packet_id=source_packet_id.strip(),
        card_id=card_id.strip(),
        limit=bounded_limit,
    )
    return {
        "items": items,
        "count": len(items),
        "filters": {
            "source_packet_id": source_packet_id.strip(),
            "card_id": card_id.strip(),
            "limit": bounded_limit,
        },
    }


@router.get("/evaluations")
def list_evaluation_runs(limit: int = 50):
    bounded_limit = max(1, min(limit, 200))
    if not EVALUATION_RUNS_DIR.exists():
        return {"items": [], "latest_run_id": ""}
    runs = [
        _evaluation_run_item(path)
        for path in EVALUATION_RUNS_DIR.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    runs = [run for run in runs if run["has_summary"] or run["case_count"] > 0]
    runs.sort(key=lambda item: item["updated_at"], reverse=True)
    runs = runs[:bounded_limit]
    latest = next((item for item in runs if item["has_summary"] or item["case_count"] > 0), None)
    return {
        "items": runs,
        "latest_run_id": latest["id"] if latest else "",
    }


@router.get("/evaluations/{run_id}")
def get_evaluation_run(run_id: str):
    run_path = _safe_eval_run_path(run_id)
    summary = _load_eval_summary(run_path)
    cases = _read_evaluation_cases(run_path, include_answer=True)
    return {
        "id": run_path.name,
        "updated_at": _eval_file_time(run_path),
        "summary": summary,
        "split_metrics": _case_metric_splits(cases),
        "lowest_cases": _low_score_cases(cases, limit=8),
        "case_count": len(cases),
        "artifacts": {
            "summary_json": str(run_path / "summary.json") if (run_path / "summary.json").exists() else "",
            "summary_md": str(run_path / "summary.md") if (run_path / "summary.md").exists() else "",
            "queries_details_csv": str(run_path / "queries_details.csv") if (run_path / "queries_details.csv").exists() else "",
            "failed_cases_md": str(run_path / "failed_cases.md") if (run_path / "failed_cases.md").exists() else "",
        },
    }


@router.get("/evaluations/{run_id}/cases")
def list_evaluation_cases(
    run_id: str,
    limit: int = 100,
    low_only: bool = False,
):
    run_path = _safe_eval_run_path(run_id)
    bounded_limit = max(1, min(limit, 500))
    cases = _read_evaluation_cases(run_path, include_answer=True)
    if low_only:
        cases = sorted(cases, key=lambda item: (item["final_score"], item["id"]))
    return {
        "run_id": run_path.name,
        "items": cases[:bounded_limit],
        "count": len(cases[:bounded_limit]),
        "total": len(cases),
    }


@router.post("/ingest")
def create_wiki_ingestion_job(
    file: Optional[UploadFile] = File(None),
    local_path: str = Form(""),
    source_url: str = Form(""),
    pipeline: str = Form("four_agent"),
    store: WikiStore = Depends(get_wiki_store),
):
    if file and file.filename:
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF uploads are supported.")
        PAPER_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = PAPER_UPLOAD_DIR / _safe_paper_pdf_name(file.filename)
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        source_uri = str(dest.relative_to(PAPER_REPO_ROOT))
        filename = file.filename
    elif local_path.strip():
        dest = _resolve_paper_data_pdf(local_path)
        source_uri = str(dest.relative_to(PAPER_REPO_ROOT))
        filename = dest.name
    else:
        raise HTTPException(status_code=400, detail="Provide a PDF file or local_path.")

    jobs = IngestionJobStore(db_path=store.db_path)
    job = jobs.create_job(
        source_type="paper_pdf",
        source_uri=source_uri,
        stage="queued",
        metadata={
            "source_url": source_url,
            "pipeline": pipeline or "four_agent",
            "filename": filename,
        },
    )
    threading.Thread(
        target=_run_paper_ingestion_job,
        kwargs={
            "db_path": jobs.db_path,
            "job_id": job["id"],
            "pdf_path": str(dest.resolve()),
            "source_url": source_url,
            "pipeline": pipeline or "four_agent",
        },
        daemon=True,
    ).start()
    return {"ok": True, "job_id": job["id"], "job": job}


@router.get("/ingest/jobs")
def list_wiki_ingestion_jobs(
    limit: int = 100,
    store: WikiStore = Depends(get_wiki_store),
):
    return {"items": IngestionJobStore(db_path=store.db_path).list_jobs(limit=limit)}


@router.get("/ingest/jobs/{job_id}")
def get_wiki_ingestion_job(
    job_id: str,
    store: WikiStore = Depends(get_wiki_store),
):
    job = IngestionJobStore(db_path=store.db_path).get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Ingestion job not found")
    return job


@router.get("/sessions")
def list_chat_sessions(store=Depends(get_session_store)):
    sessions = store.list_sessions()
    return {"items": sessions}


@router.post("/sessions")
def create_chat_session(payload: SessionCreatePayload, store=Depends(get_session_store)):
    session_id = store.create_session(title=(payload.title or "新会话").strip() or "新会话", settings={"mode": "wiki_chat"})
    return {"id": session_id, "title": payload.title or "新会话"}


@router.get("/sessions/{session_id}/messages")
def get_chat_session_messages(session_id: str, store=Depends(get_session_store)):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    history = store.get_display_history(session_id, last_n=50)
    items = []
    seq = 1
    for user_text, assistant_text, metadata in history:
        items.append({
            "id": seq,
            "role": "user",
            "content": user_text,
            "citations": [],
            "profile_updates": [],
        })
        seq += 1
        if assistant_text:
            items.append({
                "id": seq,
                "role": "assistant",
                "content": assistant_text,
                "citations": metadata.get("citations", []),
                "resources": metadata.get("resources", []),
                "profile_updates": metadata.get("profile_updates", []),
                "tool_plan": metadata.get("tool_plan", {}),
                "trace": metadata.get("trace", {}),
            })
            seq += 1
    return {"session": session, "items": items}


@router.delete("/sessions/{session_id}")
def delete_chat_session(session_id: str, store=Depends(get_session_store)):
    deleted = store.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True, "deleted": 1, "memory_retained": True}


@router.delete("/sessions")
def delete_all_chat_sessions(store=Depends(get_session_store)):
    deleted = store.delete_all_sessions()
    return {"ok": True, "deleted": deleted, "memory_retained": True}


@router.post("/chat")
def chat_with_wiki(
    payload: WikiChatPayload,
    chat_service: WikiChatService = Depends(get_wiki_chat),
):
    if payload.stream:
        def _stream():
            for chunk in chat_service.chat_stream(
                payload.message,
                session_id=payload.session_id,
            ):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    result = chat_service.chat(payload.message, session_id=payload.session_id)
    return {
        "answer": result.answer,
        "citations": [citation.__dict__ for citation in result.citations],
        "resources": result.resources,
        "profile_updates": result.profile_updates,
        "tool_plan": result.tool_plan,
        "trace": result.trace,
    }


@router.post("/capture")
def capture_note(payload: CapturePayload, store: WikiStore = Depends(get_wiki_store)):
    title = payload.title.strip() or _summary(payload.raw_text, limit=40) or "Untitled Note"
    raw_text = payload.raw_text.strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="raw_text cannot be empty")

    page_type = _page_type(payload.source_type)
    raw_source_path = _write_text_raw_markdown(
        title=title,
        raw_text=raw_text,
        source_type=payload.source_type,
        source_url=payload.source_url,
        tags=payload.tags,
    )
    existing = store.find_duplicate(
        title=title,
        page_type=page_type,
        source_urls=[payload.source_url] if payload.source_url else [],
    )
    compiled = _compile_raw_wiki_page(
        title=title,
        raw_source_path=raw_source_path,
        page_type=page_type,
        source_kind="text_note",
    )
    summary, content_json, related_topics, source_level = _compiled_or_pending_content(
        compiled=compiled,
        raw_source_path=raw_source_path,
        source_kind="text_note",
        operational_fields={
            "source_type": payload.source_type,
            "tags": payload.tags,
            "raw_source_path": raw_source_path,
        },
    )
    if not compiled:
        content_json["notes"] = "原始文本已保存，等待 Wiki 编译。"

    legacy_content = {
        "source_type": payload.source_type,
        "main_points": _summary(raw_text, limit=360),
        "notes": raw_text,
        "useful_for": "面试复盘 / 论文阅读 / 灵感沉淀",
        "tags": payload.tags,
        "raw_source_path": raw_source_path,
    }
    card_id = store.create_card(
        title=title,
        page_type=page_type,
        content_json=content_json or legacy_content,
        summary=summary or _summary(raw_text),
        source_level=source_level or ("secondary" if payload.source_type == "interview" else ""),
        source_urls=[payload.source_url] if payload.source_url else [],
        related_topics=related_topics,
    )
    _reindex_card(card_id, content_json)
    return {"ok": True, "card_id": card_id, "deduped": bool(existing), "raw_source_path": raw_source_path}


@router.post("/import-xhs")
def import_xiaohongshu(payload: XhsImportPayload, store: WikiStore = Depends(get_wiki_store)):
    note = XiaohongshuExtractor().extract_from_text_or_url(payload.text_or_url)
    if not note:
        raise HTTPException(status_code=400, detail="无法从小红书链接或分享文案中提取公开信息。")

    source_url = note.final_url or note.source_url
    if "/404" in source_url:
        source_url = note.source_url
    note_id = _xhs_note_id(source_url)

    downloaded_images: list[str] = []
    if note.image_urls:
        try:
            downloaded_images = download_images(note.image_urls, note_id=note_id)
        except Exception as exc:
            print(f"[wiki] XHS image download failed (non-fatal): {exc}")

    ocr_text, ocr_status, _, failed = _ocr_images(downloaded_images)
    if downloaded_images and ocr_status == "failed" and failed == 0:
        ocr_status = "pending"

    cleaned_ocr = _sanitize_ocr_text(ocr_text)
    structured_ocr = _structure_ocr_text(cleaned_ocr)
    operational_content: dict = {
        "source_type": "xiaohongshu",
        "ocr_excerpt": _visible_ocr_excerpt(cleaned_ocr),
        "attachments": downloaded_images,
        "image_urls": note.image_urls,
        "downloaded_images": downloaded_images,
        "_ocr_text": cleaned_ocr,
        "_ocr_notes": structured_ocr,
        "_ocr_status": ocr_status,
        "tags": payload.tags,
    }
    raw_source_path = _write_xhs_raw_markdown(
        title=note.title,
        source_url=source_url,
        note_text=note.content,
        image_urls=note.image_urls,
        downloaded_images=downloaded_images,
        ocr_text=structured_ocr or cleaned_ocr,
        ocr_status=ocr_status,
        tags=payload.tags,
        slug_hint=note_id,
    )
    operational_content["raw_source_path"] = raw_source_path
    distilled = _compile_xhs_distilled_page(
        title=note.title,
        source_url=source_url,
        note_text=note.content,
        cleaned_ocr=cleaned_ocr,
        structured_ocr=structured_ocr,
        tags=payload.tags,
        raw_source_path=raw_source_path,
    )
    fallback_summary, fallback_content, fallback_related = _build_xhs_schema_page(
        title=note.title,
        source_url=source_url,
        note_text=note.content,
        cleaned_ocr=cleaned_ocr,
        structured_ocr=structured_ocr,
        tags=payload.tags,
        raw_source_path=raw_source_path,
    )
    if distilled:
        distilled_summary, distilled_content, distilled_related, review = distilled
        content_json = _merge_operational_fields(distilled_content, operational_content)
        content_json["distill_review"] = review
        summary = distilled_summary or fallback_summary or _summary(note.content, limit=220) or note.title
        related_topics = list(dict.fromkeys((distilled_related or []) + fallback_related))
    else:
        content_json = _merge_operational_fields(fallback_content, operational_content)
        content_json["pipeline"] = "xhs_four_agent"
        content_json["compile_status"] = "direct_source_fallback"
        content_json["review_status"] = "fallback"
        summary = fallback_summary or _summary(note.content, limit=220) or note.title
        related_topics = fallback_related
    source_level = "secondary"

    existing = store.find_duplicate(
        title=note.title,
        page_type="InterviewQA",
        source_urls=[source_url],
    )
    card_id = store.create_card(
        title=note.title,
        page_type="InterviewQA",
        content_json=content_json,
        summary=summary or _summary(note.content, limit=220),
        source_level=source_level or "secondary",
        source_urls=[source_url],
        related_topics=related_topics or payload.tags or ["小红书", "面经"],
    )
    linked_cards = _link_xhs_card_to_known_cards(
        card_id=card_id,
        title=note.title,
        text="\n\n".join(
            part for part in [
                note.title,
                note.content,
                structured_ocr,
                cleaned_ocr,
                " ".join(payload.tags),
            ]
            if part
        ),
        raw_source_path=raw_source_path,
        source_url=source_url,
        store=store,
    )
    if linked_cards:
        content_json["linked_knowledge"] = linked_cards
        store.update_card(card_id, content_json=content_json)
    _reindex_card(card_id, content_json)
    return {
        "ok": True,
        "card_id": card_id,
        "deduped": bool(existing),
        "title": note.title,
        "final_url": source_url,
        "summary": summary,
        "images_downloaded": len(downloaded_images),
        "ocr_status": ocr_status,
        "ocr_chars": len(cleaned_ocr) if cleaned_ocr else 0,
        "linked_cards": linked_cards,
        "raw_source_path": raw_source_path,
    }


@router.post("/import-images")
def import_images(
    files: list[UploadFile] = File(...),
    title: str = Form(""),
    source_url: str = Form(""),
    source_type: str = Form("interview"),
    tags: str = Form(""),
    store: WikiStore = Depends(get_wiki_store),
):
    tag_list = _split_tags(tags)
    resolved_title = title.strip() or "图片资料"
    source_id = _source_id(resolved_title, source_url, resolved_title + tags)
    local_files = _store_image_uploads(files, source_id)
    ocr_text, ocr_status, processed, failed = _ocr_images(local_files)
    cleaned_ocr = _sanitize_ocr_text(ocr_text)
    structured_ocr = _structure_ocr_text(cleaned_ocr)

    page_type = _page_type(source_type)
    raw_source_path = _write_image_raw_markdown(
        title=resolved_title,
        source_url=source_url,
        source_type=source_type,
        tags=tag_list,
        local_files=local_files,
        ocr_text=cleaned_ocr,
        ocr_status=ocr_status,
    )
    existing = store.find_duplicate(
        title=resolved_title,
        page_type=page_type,
        source_urls=[source_url] if source_url else [],
    )
    operational_content = {
        "source_type": source_type,
        "ocr_excerpt": _visible_ocr_excerpt(cleaned_ocr),
        "attachments": local_files,
        "downloaded_images": local_files,
        "_ocr_text": cleaned_ocr,
        "_ocr_notes": structured_ocr,
        "_ocr_status": ocr_status,
        "tags": tag_list,
        "raw_source_path": raw_source_path,
    }
    compiled = _compile_raw_wiki_page(
        title=resolved_title,
        raw_source_path=raw_source_path,
        page_type=page_type,
        source_kind="image_note",
    )
    summary, content_json, related_topics, source_level = _compiled_or_pending_content(
        compiled=compiled,
        raw_source_path=raw_source_path,
        source_kind="image_note",
        operational_fields=operational_content,
    )
    if not compiled:
        content_json["notes"] = structured_ocr or cleaned_ocr.strip() if cleaned_ocr.strip() else "图片已保存，等待 OCR 或人工补充。"
    card_id = store.create_card(
        title=resolved_title,
        page_type=page_type,
        content_json=content_json,
        summary=summary or (_summary(structured_ocr or cleaned_ocr, limit=180) if cleaned_ocr.strip() else "图片资料已入库。"),
        source_level=source_level or ("secondary" if source_type == "interview" else ""),
        source_urls=[source_url] if source_url else [],
        related_topics=related_topics or tag_list,
    )
    _reindex_card(card_id, content_json)
    return {
        "ok": True,
        "card_id": card_id,
        "deduped": bool(existing),
        "title": resolved_title,
        "images_saved": len(local_files),
        "ocr_status": ocr_status,
        "ocr_chars": len(cleaned_ocr),
        "processed": processed,
        "failed": failed,
        "raw_source_path": raw_source_path,
    }


@router.post("/{card_id}/ocr")
def ocr_card_images(card_id: str, store: WikiStore = Depends(get_wiki_store)):
    card = store.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Wiki card not found")

    content = card.get("content_json", {})
    if not isinstance(content, dict):
        content = {}

    image_urls = content.get("image_urls", [])
    downloaded_images = content.get("downloaded_images", [])
    if not image_urls and not downloaded_images:
        return {
            "ok": True,
            "card_id": card_id,
            "ocr_status": "not_needed",
            "images": 0,
            "text_chars": 0,
            "message": "No image URLs or local images on this card.",
        }

    note_id = _xhs_note_id(card.get("source_urls", [""])[0] if card.get("source_urls") else "")
    if image_urls:
        try:
            new_images = download_images(image_urls, note_id=note_id)
            if new_images:
                downloaded_images = list(dict.fromkeys(downloaded_images + new_images))
        except Exception as exc:
            print(f"[wiki] OCR image download failed: {exc}")

    ocr_text, ocr_status, processed, failed = _ocr_images(downloaded_images)
    cleaned_ocr = _sanitize_ocr_text(ocr_text)
    structured_ocr = _structure_ocr_text(cleaned_ocr)
    content["downloaded_images"] = downloaded_images
    content["attachments"] = downloaded_images
    if cleaned_ocr:
        content["_ocr_text"] = cleaned_ocr
        content["_ocr_notes"] = structured_ocr
        content["ocr_excerpt"] = _visible_ocr_excerpt(cleaned_ocr)
        content["ocr_notes"] = structured_ocr or _visible_ocr_excerpt(cleaned_ocr)
        if not (content.get("notes", "") or "").strip():
            content["notes"] = structured_ocr or _visible_ocr_excerpt(cleaned_ocr)
    content["_ocr_status"] = ocr_status
    if ocr_status in {"failed", "pending"} and not cleaned_ocr:
        content["ocr_error"] = "No text extracted yet"

    content["raw_source_path"] = _write_xhs_raw_markdown(
        title=card.get("title", ""),
        source_url=card.get("source_urls", [""])[0] if card.get("source_urls") else "",
        note_text=content.get("notes", ""),
        image_urls=image_urls,
        downloaded_images=downloaded_images,
        ocr_text=content.get("_ocr_text", ""),
        ocr_status=content["_ocr_status"],
        tags=content.get("tags", []),
        slug_hint=note_id or card_id,
    )
    store.update_card(card_id, content_json=content)
    _reindex_card(card_id, content, card.get("markdown_path", ""))

    return {
        "ok": True,
        "card_id": card_id,
        "ocr_status": content["ocr_status"],
        "images": len(downloaded_images),
        "processed": processed,
        "failed": failed,
        "text_chars": len(content.get("_ocr_text", "")),
        "raw_source_path": content["raw_source_path"],
    }


def _reindex_card(card_id: str, content_json: dict, markdown_path: str = ""):
    """Trigger chunk reindex for a card after create/update."""
    from backend.deps import get_chunk_index
    chunk_idx = get_chunk_index()
    raw_source = (content_json or {}).get("raw_source_path", "")
    source_kind = (content_json or {}).get("source_type", "")
    chunk_idx.reindex_card(
        card_id=card_id,
        raw_source_path=raw_source,
        markdown_path=markdown_path,
        source_kind=source_kind,
    )


def _xhs_note_id(source_url: str) -> str:
    for pattern in [
        r"/discovery/item/([a-fA-F0-9]+)",
        r"/explore/([a-fA-F0-9]+)",
        r"/a/([a-fA-F0-9]+)",
    ]:
        match = re.search(pattern, source_url or "")
        if match:
            return match.group(1)
    return hashlib.md5((source_url or "unknown").encode()).hexdigest()[:16]


@router.get("/object")
def get_storage_object(ref: str):
    data = _read_storage_bytes(ref)
    if not data:
        raise HTTPException(status_code=404, detail="Storage object not found")
    content_type = mimetypes.guess_type(ref)[0] or "application/octet-stream"
    return Response(content=data, media_type=content_type)


@router.get("/{card_id}")
def get_card(card_id: str, store: WikiStore = Depends(get_wiki_store)):
    card = store.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Wiki card not found")
    card["obsidian_uri"] = store.vault.card_uri(card.get("markdown_path", ""))
    return card


@router.get("/{card_id}/links")
def get_card_links(card_id: str, store: WikiStore = Depends(get_wiki_store)):
    card = store.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Wiki card not found")
    links = PaperWikiPipelineStore(db_path=store.db_path).list_card_links(card_id)
    return {"card_id": card_id, **links}


@router.get("/{card_id}/raw-source")
def get_card_raw_source(card_id: str, store: WikiStore = Depends(get_wiki_store)):
    """Return the raw Markdown source for a Wiki card.

    Reads the raw_source_path from content_json.raw_source_path, then the
    vault markdown file, and returns it as plain text.
    Falls back to the main markdown_path if raw_source_path is unavailable.
    """
    card = store.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Wiki card not found")

    content = card.get("content_json", {})
    if not isinstance(content, dict):
        content = {}

    raw_path = content.get("raw_source_path", "") or card.get("markdown_path", "")
    if not raw_path:
        raise HTTPException(status_code=404, detail="No markdown source file available for this card.")

    try:
        markdown_text = get_object_storage().read_text(raw_path)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Markdown source not readable: {raw_path}") from exc

    if not markdown_text:
        raise HTTPException(status_code=404, detail=f"Markdown source file not found: {raw_path}")

    return {
        "card_id": card_id,
        "title": card.get("title", ""),
        "path": raw_path,
        "markdown": markdown_text,
        "length": len(markdown_text),
    }


@router.delete("/{card_id}")
def delete_card(card_id: str, store: WikiStore = Depends(get_wiki_store)):
    card = store.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Wiki card not found")
    store.delete_card(card_id)
    return {"ok": True, "deleted_id": card_id}
