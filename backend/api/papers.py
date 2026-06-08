import re
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.deps import get_merge_llm, get_paper_index, get_review_llm, get_summary_llm, get_wiki_store, get_chunk_index
from system.paper_index.parser import PaperIndexParser
from system.paper_index.store import PaperIndexStore
from system.discovery.source_adapters import ArxivAdapter
from system.document.docling_parser import DoclingParser, ParsedDocument
from system.storage import get_object_storage, get_storage_layout
from system.wiki.wiki_builder import WikiBuilder, sanitize_wiki_text
from system.wiki.paper_pipeline import run_paper_pipeline
from system.wiki.ingestion_jobs import IngestionJobStore
from system.wiki.maintenance.runner import WikiMaintenanceRunner
from system.wiki.raw_source_vault import RawSourceVault
from system.wiki.wiki_store import WikiStore


router = APIRouter()

REPO_ROOT = Path(__file__).resolve().parents[2]
STORAGE_LAYOUT = get_storage_layout()
SOURCES_DIR = STORAGE_LAYOUT.sources_dir
UPLOAD_DIR = STORAGE_LAYOUT.source_dir("papers", "uploads")


class IndexLocalPayload(BaseModel):
    local_path: str
    source_url: str = ""
    pipeline: str = ""


def _safe_pdf_name(filename: str) -> str:
    name = Path(filename or "paper.pdf").name
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return "".join(ch if ch.isalnum() or ch in " ._-()" else "_" for ch in name)[:120]


def _resolve_data_pdf(path_value: str) -> Path:
    raw = Path(path_value)
    path = raw if raw.is_absolute() else REPO_ROOT / raw
    resolved = path.resolve()
    if not resolved.exists() or resolved.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="PDF file not found.")
    return resolved


def _parse_pdf_with_fallback(pdf_path: Path, use_docling: bool = True) -> dict:
    """Parse a PDF with Docling if available, otherwise fallback to PaperIndexParser.

    Returns a dict with keys: title, summary, metadata, blocks, parser_used.
    """
    docling_parser = DoclingParser() if use_docling else None

    if docling_parser and docling_parser.available:
        try:
            doc: ParsedDocument = docling_parser.parse_file(str(pdf_path))
            blocks = doc.pages_or_items
            if not blocks:
                # Docling succeeded but produced no blocks — chunk markdown
                blocks = _chunk_markdown_to_blocks(doc.markdown, str(pdf_path))
            return {
                "title": doc.title,
                "summary": _make_summary(doc.text or doc.markdown),
                "metadata": doc.metadata,
                "blocks": blocks,
                "markdown": doc.markdown,
                "parser_used": doc.parser or (doc.metadata or {}).get("parser") or "docling-remote",
            }
        except Exception as exc:
            print(f"[papers] Docling parse failed, falling back to PaperIndexParser: {exc}")

    # Fallback to PaperIndexParser
    fallback = PaperIndexParser().parse_pdf(str(pdf_path))
    fallback["parser_used"] = "PaperIndexParser"
    fallback.setdefault("markdown", "")
    return fallback


def _make_summary(text: str, max_len: int = 600) -> str:
    text = " ".join(sanitize_wiki_text(text or "").split())
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def _chunk_markdown_to_blocks(markdown: str, source_path: str) -> list:
    """Chunk markdown text into block-like dicts when Docling gives no pages."""
    import re
    blocks = []
    paragraphs = re.split(r"\n\s*\n", markdown)
    current_section = ""
    for idx, para in enumerate(paragraphs):
        para = para.strip()
        if not para:
            continue
        heading_match = re.match(r"^#{1,3}\s*(.+)", para)
        if heading_match:
            current_section = heading_match.group(1).strip()
        blocks.append({
            "page": idx + 1,
            "section": current_section,
            "block_type": "text",
            "text": para[:3000],
            "metadata": {"source": source_path},
        })
    return blocks


def _index_pdf(
    pdf_path: Path,
    source_url: str,
    store: PaperIndexStore,
    wiki_store: WikiStore,
    use_docling: bool = True,
    use_llm_compile: bool = True,
) -> dict:
    source_urls = [source_url] if source_url else [f"file://{pdf_path.resolve().as_posix()}"]
    arxiv_item = _fetch_arxiv_metadata(source_url)
    quick_pdf_title = _quick_pdf_title(pdf_path)
    quick_pdf_abstract = _quick_pdf_abstract(pdf_path)
    if arxiv_item and quick_pdf_title and not _titles_compatible(quick_pdf_title, arxiv_item.title):
        raise HTTPException(
            status_code=400,
            detail=(
                "PDF title does not match the arXiv URL. "
                f"PDF appears to be '{quick_pdf_title}', but arXiv metadata is '{arxiv_item.title}'."
            ),
        )

    parsed = _parse_pdf_with_fallback(pdf_path, use_docling=use_docling)
    parser_used = parsed.pop("parser_used", "unknown")
    parsed_pdf_title = parsed.get("title", "")
    if quick_pdf_abstract and len(quick_pdf_abstract) > len(sanitize_wiki_text(parsed.get("summary", ""))):
        parsed["summary"] = quick_pdf_abstract
    if arxiv_item:
        if parsed_pdf_title and not _titles_compatible(parsed_pdf_title, arxiv_item.title):
            raise HTTPException(
                status_code=400,
                detail=(
                    "PDF title does not match the arXiv URL. "
                    f"PDF appears to be '{parsed_pdf_title}', but arXiv metadata is '{arxiv_item.title}'."
                ),
            )
        parsed["title"] = arxiv_item.title
        parsed["summary"] = arxiv_item.summary or parsed["summary"]
        parsed["metadata"].update({
            "authors": arxiv_item.authors,
            "year": arxiv_item.year,
            "venue": arxiv_item.venue,
            "arxiv_id": arxiv_item.raw_metadata.get("arxiv_id", ""),
        })
    pdf_storage_uri = get_object_storage().upload_file(
        pdf_path,
        key=STORAGE_LAYOUT.paper_original_key(pdf_path.name),
        content_type="application/pdf",
    )
    paper_id = store.upsert_paper(
        title=parsed["title"],
        source_url=source_url,
        pdf_path=str(pdf_path),
        summary=parsed["summary"],
        metadata=parsed["metadata"],
    )
    store.replace_blocks(paper_id, parsed["blocks"])

    raw_markdown = parsed.get("markdown", "") if isinstance(parsed.get("markdown", ""), str) else ""
    if not raw_markdown.strip():
        raw_markdown = _paper_markdown_from_blocks(parsed["title"], parsed["summary"], parsed["blocks"])
    raw_markdown = sanitize_wiki_text(raw_markdown)
    if not raw_markdown.strip():
        raw_markdown = sanitize_wiki_text(
            _paper_markdown_from_blocks(parsed["title"], parsed["summary"], parsed["blocks"])
        )
    raw_source_path = RawSourceVault().write_source(
        source_kind="paper_pdf",
        title=parsed["title"],
        body_markdown=raw_markdown,
        metadata={
            "parser": parser_used,
            "pdf_path": str(pdf_path),
            "pdf_storage_uri": pdf_storage_uri,
            "page_count": parsed["metadata"].get("page_count", 0),
            "year": parsed["metadata"].get("year", ""),
            "venue": parsed["metadata"].get("venue", ""),
        },
        source_urls=source_urls,
        local_files=[str(pdf_path)],
        slug_hint=pdf_path.stem,
    )

    compiled = _compile_paper_wiki_page(
        title=parsed["title"],
        raw_markdown=raw_markdown,
        parser_used=parser_used,
    ) if use_llm_compile else None
    card_title = parsed["title"]
    fallback = None
    if _compiled_paper_is_useful(compiled):
        card_content = (compiled or {}).get("content_json") or {}
        card_summary = (
            sanitize_wiki_text((compiled or {}).get("summary") or "")
            or sanitize_wiki_text(str(card_content.get("problem") or card_content.get("key_idea") or parsed["summary"]))
        )[:240]
        related_topics = (compiled or {}).get("related_topics") or []
        source_level = (compiled or {}).get("source_level") or "primary"
    else:
        fallback = _build_paper_schema_page(
            parsed=parsed,
            raw_markdown=raw_markdown,
            parser_used=parser_used,
        )
        card_summary = (
            sanitize_wiki_text(fallback.get("summary", ""))
            or sanitize_wiki_text(parsed.get("summary", ""))
        )[:240]
        card_content = fallback.get("content_json") or {}
        related_topics = fallback.get("related_topics") or []
        source_level = fallback.get("source_level") or "primary"
    card_content = _augment_paper_card_content(
        content=card_content,
        parsed=parsed,
        raw_source_path=raw_source_path,
        pdf_storage_uri=pdf_storage_uri,
    )

    existing_card = wiki_store.find_duplicate(
        title=card_title,
        page_type="PaperPage",
        source_urls=source_urls,
    )
    wiki_card_id = wiki_store.create_card(
        title=card_title,
        page_type="PaperPage",
        content_json=card_content,
        summary=card_summary[:240],
        source_level=source_level,
        source_urls=source_urls,
        related_topics=related_topics,
    )

    # Trigger unified chunk indexing so Wiki Chat can find this paper's full text
    chunk_idx = get_chunk_index()
    chunk_idx.reindex_card(
        card_id=wiki_card_id,
        raw_source_path=raw_source_path,
        source_kind="paper_pdf",
    )

    return {
        "paper_id": paper_id,
        "blocks": len(parsed["blocks"]),
        "wiki_card_id": wiki_card_id,
        "deduped": bool(existing_card),
        "parser": parser_used,
        "raw_source_path": raw_source_path,
        "pdf_storage_uri": pdf_storage_uri,
    }


def _compiled_paper_is_useful(compiled: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(compiled, dict):
        return False

    summary = sanitize_wiki_text(compiled.get("summary") or "")
    if len(summary) >= 40:
        return True

    content = compiled.get("content_json") or {}
    if not isinstance(content, dict):
        return False

    technical_fields = {
        "raw_source_path",
        "pdf_storage_uri",
        "source_kind",
        "compile_status",
        "compile_error",
    }
    useful_fields = 0
    for key, value in content.items():
        if key in technical_fields:
            continue
        text = _stringify_content_value(value)
        if len(sanitize_wiki_text(text)) >= 40:
            useful_fields += 1
    return useful_fields >= 2


def _stringify_content_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify_content_value(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_stringify_content_value(item) for item in value.values())
    return str(value)


def _augment_paper_card_content(
    content: Dict[str, Any],
    parsed: Dict[str, Any],
    raw_source_path: str,
    pdf_storage_uri: str,
) -> Dict[str, Any]:
    next_content = dict(content or {})
    metadata = parsed.get("metadata") or {}

    for key in ("authors", "year", "venue", "arxiv_id"):
        value = metadata.get(key)
        if value and not next_content.get(key):
            next_content[key] = value

    parsed_summary = sanitize_wiki_text(parsed.get("summary", ""))
    if parsed_summary and not next_content.get("problem"):
        next_content["problem"] = parsed_summary[:800]

    next_content["raw_source_path"] = raw_source_path
    next_content["pdf_storage_uri"] = pdf_storage_uri
    return next_content


def _quick_pdf_title(pdf_path: Path) -> str:
    try:
        import fitz
    except Exception:
        return ""
    try:
        doc = fitz.open(str(pdf_path))
        if not doc.page_count:
            doc.close()
            return ""
        text = doc[0].get_text() or ""
        doc.close()
    except Exception:
        return ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title_lines: list[str] = []
    for line in lines[:8]:
        lowered = line.lower()
        if lowered == "abstract" or "@" in line:
            break
        if len(line) <= 2:
            continue
        title_lines.append(line)
        if len(title_lines) >= 3:
            break
    return " ".join(title_lines).strip()[:300]


def _quick_pdf_abstract(pdf_path: Path) -> str:
    try:
        import fitz
    except Exception:
        return ""
    try:
        doc = fitz.open(str(pdf_path))
        text = "\n".join(doc[index].get_text() or "" for index in range(min(doc.page_count, 2)))
        doc.close()
    except Exception:
        return ""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = re.search(
        r"\bAbstract\b\s*(.*?)(?:\n\s*(?:1\.?\s*)?Introduction\b|\n\s*Keywords?\b)",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        abstract = match.group(1)
    else:
        intro = re.search(r"\n\s*(?:1\.?\s*)?Introduction\b", normalized, flags=re.IGNORECASE)
        if not intro:
            return ""
        prefix_lines = [line.strip() for line in normalized[:intro.start()].splitlines() if line.strip()]
        start = 0
        marker = re.compile(
            r"(corresponding author|equal contribution|project lead|university|institute|college|school|laboratory|^\\d+\\s+)",
            re.IGNORECASE,
        )
        for index, line in enumerate(prefix_lines[:40]):
            if marker.search(line):
                start = index + 1
        abstract = " ".join(prefix_lines[start:])
    abstract = re.sub(r"\s+", " ", abstract).strip()
    return _make_summary(abstract, max_len=900)


def _build_paper_schema_page(parsed: Dict[str, Any], raw_markdown: str, parser_used: str) -> Dict[str, Any]:
    summary = sanitize_wiki_text(parsed.get("summary", ""))
    if not summary:
        summary = _extract_markdown_section(raw_markdown, ["abstract"], limit=700)
    if not summary:
        summary = _make_summary(raw_markdown, max_len=700)

    method = _extract_markdown_section(raw_markdown, ["method", "approach", "training"], limit=900)
    results = _extract_markdown_section(raw_markdown, ["experiment", "results", "evaluation"], limit=900)
    limitations = _extract_markdown_section(raw_markdown, ["limitation", "discussion"], limit=700)
    takeaways = [item for item in [
        _make_summary(summary, max_len=220),
        _make_summary(method, max_len=220),
        _make_summary(results, max_len=220),
    ] if item]

    return {
        "page_type": "PaperPage",
        "title": parsed.get("title") or "Untitled",
        "summary": summary[:360],
        "content_json": {
            "schema_version": "autosci-lite-v1",
            "compile_status": "schema_fallback",
            "source_kind": f"paper_pdf/{parser_used}",
            "tldr": _make_summary(summary, max_len=220),
            "contribution_type": [],
            "datasets": [],
            "problem": summary[:900],
            "key_idea": summary[:900],
            "method": method,
            "results": results,
            "limitations": limitations,
            "open_questions": [],
            "my_take": "",
            "key_takeaways": takeaways[:5],
            "notes": "Generated by deterministic paper schema fallback; raw source is preserved for later LLM refinement.",
        },
        "source_level": "primary",
        "related_topics": [],
    }


def _extract_markdown_section(raw_markdown: str, names: list[str], limit: int = 800) -> str:
    text = sanitize_wiki_text(raw_markdown)
    if not text:
        return ""
    heading_pattern = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
    matches = list(heading_pattern.finditer(text))
    lowered_names = [name.lower() for name in names]
    for index, match in enumerate(matches):
        heading = match.group(1).strip().lower()
        if not any(name in heading for name in lowered_names):
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[start:end].strip()
        return _make_summary(section, max_len=limit)
    return ""


def _title_tokens(title: str) -> set[str]:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", " ", (title or "").lower())
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "via",
        "with",
    }
    return {token for token in normalized.split() if len(token) >= 3 and token not in stop_words}


def _titles_compatible(pdf_title: str, metadata_title: str) -> bool:
    pdf_tokens = _title_tokens(pdf_title)
    metadata_tokens = _title_tokens(metadata_title)
    if not pdf_tokens or not metadata_tokens:
        return True
    overlap = len(pdf_tokens & metadata_tokens)
    return overlap / max(min(len(pdf_tokens), len(metadata_tokens)), 1) >= 0.45


def _compile_paper_wiki_page(title: str, raw_markdown: str, parser_used: str) -> Optional[dict]:
    """Best-effort Karpathy-style compile step for a paper.

    Failure must not block ingestion. The raw Markdown remains the durable
    source, while the compiled page is the user-facing Wiki note.
    """
    try:
        from backend.deps import get_summary_llm
        llm = get_summary_llm()
    except Exception as exc:
        print(f"[papers] Wiki compiler LLM unavailable: {exc}")
        return None
    if not llm:
        return None
    try:
        compiled = WikiBuilder(llm).build_from_raw_markdown(
            title=title,
            raw_markdown=sanitize_wiki_text(raw_markdown),
            page_type="PaperPage",
            source_kind=f"paper_pdf/{parser_used}",
        )
        if compiled:
            return compiled
    except Exception as exc:
        print(f"[papers] Wiki compile failed: {exc}")
    return None


def _fetch_arxiv_metadata(source_url: str):
    source_url = (source_url or "").strip()
    if "arxiv.org" not in source_url:
        return None
    import re
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#/]+)", source_url)
    if not match:
        return None
    arxiv_id = match.group(1).removesuffix(".pdf")
    try:
        return ArxivAdapter().fetch_detail(arxiv_id)
    except Exception as exc:
        print(f"[papers] arXiv metadata fetch failed: {exc}")
        return None


def _paper_markdown_from_blocks(title: str, summary: str, blocks: list) -> str:
    lines = [f"# {title}", ""]
    if summary:
        lines.extend(["## Summary", "", summary, ""])
    current_section = None
    for block in blocks[:200]:
        section = (block.get("section") or "").strip()
        text = (block.get("text") or "").strip()
        if not text:
            continue
        if section and section != current_section:
            current_section = section
            lines.extend([f"## {section}", ""])
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


@router.get("")
def list_papers(store: PaperIndexStore = Depends(get_paper_index)):
    return {"items": store.list_papers(limit=200)}


@router.get("/files")
def list_local_pdfs():
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(SOURCES_DIR.rglob("*.pdf")):
        files.append({
            "name": path.name,
            "path": str(path.relative_to(REPO_ROOT)),
            "size": path.stat().st_size,
        })
    return {"items": files}


@router.get("/{paper_id}")
def get_paper(paper_id: str, store: PaperIndexStore = Depends(get_paper_index)):
    paper = store.get_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return {
        **paper,
        "sections": store.list_sections(paper_id),
    }


@router.get("/{paper_id}/blocks")
def list_blocks(
    paper_id: str,
    section: str = "",
    store: PaperIndexStore = Depends(get_paper_index),
):
    if not store.get_paper(paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    return {"items": store.list_blocks(paper_id, section=section, limit=200)}


@router.get("/{paper_id}/search")
def search_blocks(
    paper_id: str,
    query: str,
    store: PaperIndexStore = Depends(get_paper_index),
):
    if not store.get_paper(paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    return {"items": store.search_blocks(paper_id, query=query, limit=30)}


@router.delete("/{paper_id}")
def delete_paper(paper_id: str, store: PaperIndexStore = Depends(get_paper_index)):
    if not store.get_paper(paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    store.delete_paper(paper_id)
    return {"ok": True, "deleted_id": paper_id}


@router.post("/index-local")
def index_local_pdf(
    payload: IndexLocalPayload,
    store: PaperIndexStore = Depends(get_paper_index),
    wiki_store: WikiStore = Depends(get_wiki_store),
):
    pdf_path = _resolve_data_pdf(payload.local_path)
    result = _run_pdf_pipeline(pdf_path, payload.source_url, payload.pipeline, store, wiki_store)
    return {"ok": True, **result}


def _model_to_dict(model):
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _run_pdf_pipeline(
    pdf_path: Path,
    source_url: str,
    pipeline: str,
    store: PaperIndexStore,
    wiki_store: WikiStore,
) -> dict:
    normalized_pipeline = (pipeline or "").strip().lower()
    if normalized_pipeline == "four_agent":
        return _model_to_dict(run_paper_pipeline(
            pdf_path=pdf_path,
            source_url=source_url,
            paper_store=store,
            wiki_store=wiki_store,
            chunk_index=get_chunk_index(),
            llm=get_summary_llm(),
            review_llm=get_review_llm(),
            merge_llm=get_merge_llm(),
        ))
    if normalized_pipeline in {"paperindex", "fallback", "basic_fallback"}:
        return _index_pdf(pdf_path, source_url, store, wiki_store, use_docling=False, use_llm_compile=False)
    return _index_pdf(pdf_path, source_url, store, wiki_store)


def _run_ingestion_job(
    *,
    db_path: str,
    job_id: str,
    pdf_path: str,
    source_url: str,
    pipeline: str,
) -> None:
    jobs = IngestionJobStore(db_path=db_path)
    normalized_pipeline = (pipeline or "").strip().lower()
    initial_stage = "indexing" if normalized_pipeline in {"paperindex", "fallback", "basic_fallback"} else "docling_extracting"
    jobs.merge_metadata(job_id, {"runner_version": "async-v2", "runner_pipeline": normalized_pipeline})
    jobs.update_job(job_id, status="running", stage=initial_stage, progress=0.12)
    try:
        result = _run_pdf_pipeline(
            pdf_path=Path(pdf_path),
            source_url=source_url,
            pipeline=pipeline,
            store=PaperIndexStore(db_path=db_path),
            wiki_store=WikiStore(db_path=db_path),
        )
        jobs.update_job(job_id, status="running", stage="indexing", progress=0.92)
        maintenance_result = _run_post_ingestion_maintenance(db_path)
        jobs.update_job(
            job_id,
            status="done",
            stage="done",
            progress=1.0,
            source_packet_id=str(result.get("source_packet_id") or ""),
            paper_card_id=str(result.get("paper_card_id") or result.get("wiki_card_id") or ""),
            result={"ok": True, **result, "maintenance": maintenance_result},
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        jobs.update_job(job_id, status="failed", stage="failed", progress=1.0, error=str(detail))


def _run_post_ingestion_maintenance(db_path: str) -> dict:
    try:
        result = WikiMaintenanceRunner(db_path=db_path).run_once(
            check_storage=False,
            create_repair_tasks=True,
            process_deterministic_repairs=True,
            generate_indices=True,
            upload_indices=True,
        )
        return {
            "ok": result.get("ok", False),
            "run_id": result.get("run_id", ""),
            "repair_task_count": result.get("repair_task_count", 0),
            "index_artifact_count": (result.get("indices") or {}).get("artifact_count", 0),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/ingest")
def create_ingestion_job(
    file: Optional[UploadFile] = File(None),
    local_path: str = Form(""),
    source_url: str = Form(""),
    pipeline: str = Form("four_agent"),
    store: PaperIndexStore = Depends(get_paper_index),
):
    if file and file.filename:
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF uploads are supported.")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOAD_DIR / _safe_pdf_name(file.filename)
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        source_uri = str(dest.relative_to(REPO_ROOT))
    elif local_path.strip():
        dest = _resolve_data_pdf(local_path)
        source_uri = str(dest.relative_to(REPO_ROOT))
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
            "filename": file.filename if file and file.filename else Path(source_uri).name,
        },
    )
    threading.Thread(
        target=_run_ingestion_job,
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
def list_ingestion_jobs(
    limit: int = 100,
    store: PaperIndexStore = Depends(get_paper_index),
):
    return {"items": IngestionJobStore(db_path=store.db_path).list_jobs(limit=limit)}


@router.get("/ingest/jobs/{job_id}")
def get_ingestion_job(
    job_id: str,
    store: PaperIndexStore = Depends(get_paper_index),
):
    job = IngestionJobStore(db_path=store.db_path).get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Ingestion job not found")
    return job


@router.post("/upload")
def upload_and_index(
    file: UploadFile = File(...),
    source_url: str = Form(""),
    pipeline: str = Form("four_agent"),
    store: PaperIndexStore = Depends(get_paper_index),
    wiki_store: WikiStore = Depends(get_wiki_store),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / _safe_pdf_name(file.filename)
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    result = _run_pdf_pipeline(dest.resolve(), source_url, pipeline, store, wiki_store)
    return {
        "ok": True,
        **result,
        "file": {
            "name": dest.name,
            "path": str(dest.relative_to(REPO_ROOT)),
            "size": dest.stat().st_size,
        },
    }
