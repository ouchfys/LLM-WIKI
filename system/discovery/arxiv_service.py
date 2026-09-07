"""Reusable arXiv discovery, PDF caching, and Wiki-ingestion gateway.

The MCP server and the built-in paper discovery adapter share this module so
arXiv parsing, request pacing, and identifier normalization have one source of
truth. It deliberately does not parse documents itself: imported PDFs are sent to
the existing FastAPI ingestion endpoint and therefore use the same dedupe,
Agent Runtime, verification, approval, and Wiki commit path as UI uploads.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal
import xml.etree.ElementTree as ET

import requests

from system.core.config import (
    ARXIV_API_URL,
    ARXIV_MCP_CACHE_DIR,
    ARXIV_MAX_PDF_MB,
    ARXIV_PDF_BASE_URL,
    ARXIV_REQUEST_INTERVAL_SECONDS,
    ARXIV_TIMEOUT_SECONDS,
    ARXIV_USER_AGENT,
    LLM_WIKI_API_URL,
)
from system.storage import get_storage_layout


SortBy = Literal["relevance", "lastUpdatedDate", "submittedDate"]
SortOrder = Literal["ascending", "descending"]
ApprovalMode = Literal["risk", "manual", "auto"]

_MODERN_ID = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$", re.IGNORECASE)
_LEGACY_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]+/\d{7}(?:v\d+)?$", re.IGNORECASE)
_SEARCH_SORTS = {"relevance", "lastUpdatedDate", "submittedDate"}
_SEARCH_ORDERS = {"ascending", "descending"}


class ArxivServiceError(RuntimeError):
    """A user-facing failure while talking to arXiv or the Wiki API."""


def normalize_arxiv_id(value: str) -> str:
    """Return a safe modern or legacy arXiv identifier, preserving version."""

    candidate = str(value or "").strip()
    candidate = re.sub(r"^arxiv:\s*", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    match = re.search(r"(?:arxiv\.org|export\.arxiv\.org)/(?:abs|pdf)/(.+)$", candidate, re.IGNORECASE)
    if match:
        candidate = match.group(1)
    candidate = candidate.removesuffix(".pdf").strip("/")
    if not (_MODERN_ID.fullmatch(candidate) or _LEGACY_ID.fullmatch(candidate)):
        raise ValueError(f"Invalid arXiv identifier: {value!r}")
    return candidate


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _element_text(parent: ET.Element, path: str, namespaces: dict[str, str]) -> str:
    element = parent.find(path, namespaces)
    return _clean_text(element.text if element is not None else "")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArxivPaper:
    arxiv_id: str
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    published: str = ""
    updated: str = ""
    categories: list[str] = field(default_factory=list)
    primary_category: str = ""
    doi: str = ""
    journal_ref: str = ""
    comment: str = ""
    abs_url: str = ""
    pdf_url: str = ""

    @property
    def year(self) -> int | None:
        try:
            return int(self.published[:4]) if self.published else None
        except ValueError:
            return None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["year"] = self.year
        return result


@dataclass(frozen=True)
class ArxivSearchPage:
    query: str
    total_results: int
    start_index: int
    items_per_page: int
    papers: list[ArxivPaper]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "total_results": self.total_results,
            "start_index": self.start_index,
            "items_per_page": self.items_per_page,
            "papers": [paper.to_dict() for paper in self.papers],
        }


class RequestPacer:
    """Process-local courtesy interval shared by arXiv API and PDF requests."""

    def __init__(self, interval_seconds: float):
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.interval_seconds - elapsed
            if self._last_request_at and remaining > 0:
                time.sleep(remaining)
            self._last_request_at = time.monotonic()


_DEFAULT_PACER = RequestPacer(ARXIV_REQUEST_INTERVAL_SECONDS)


class ArxivClient:
    def __init__(
        self,
        *,
        api_url: str = ARXIV_API_URL,
        pdf_base_url: str = ARXIV_PDF_BASE_URL,
        user_agent: str = ARXIV_USER_AGENT,
        timeout_seconds: float = ARXIV_TIMEOUT_SECONDS,
        max_pdf_mb: int = ARXIV_MAX_PDF_MB,
        session: requests.Session | Any | None = None,
        pacer: RequestPacer | None = None,
    ):
        self.api_url = api_url.rstrip("?")
        self.pdf_base_url = pdf_base_url.rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_pdf_bytes = max(1, int(max_pdf_mb)) * 1024 * 1024
        self.session = session or requests.Session()
        self.headers = {"User-Agent": user_agent, "Accept": "application/atom+xml"}
        self.pacer = pacer or _DEFAULT_PACER

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        start: int = 0,
        sort_by: SortBy = "relevance",
        sort_order: SortOrder = "descending",
        author: str = "",
        categories: Iterable[str] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> ArxivSearchPage:
        max_results = max(1, min(int(max_results), 50))
        start = max(0, min(int(start), 1999))
        if sort_by not in _SEARCH_SORTS:
            raise ValueError(f"Unsupported arXiv sort: {sort_by}")
        if sort_order not in _SEARCH_ORDERS:
            raise ValueError(f"Unsupported arXiv sort order: {sort_order}")
        search_query = self.build_search_query(
            query,
            author=author,
            categories=categories,
            year_from=year_from,
            year_to=year_to,
        )
        xml_content = self._get_atom({
            "search_query": search_query,
            "start": start,
            "max_results": max_results,
            "sortBy": sort_by,
            "sortOrder": sort_order,
        })
        return self.parse_feed(xml_content, fallback_query=search_query)

    def get_paper(self, arxiv_id: str) -> ArxivPaper | None:
        normalized = normalize_arxiv_id(arxiv_id)
        xml_content = self._get_atom({"id_list": normalized, "max_results": 1})
        result = self.parse_feed(xml_content, fallback_query=f"id:{normalized}")
        return result.papers[0] if result.papers else None

    def download_pdf(
        self,
        arxiv_id: str,
        *,
        destination_dir: str | Path | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        normalized = normalize_arxiv_id(arxiv_id)
        cache_dir = self._resolve_cache_dir(destination_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{normalized.replace('/', '__')}.pdf"
        destination = (cache_dir / filename).resolve()
        if cache_dir not in destination.parents:
            raise ArxivServiceError("Resolved arXiv cache path escaped its managed directory.")
        metadata_path = destination.with_suffix(".json")

        if destination.exists() and not force and self._is_pdf(destination):
            return self._download_result(normalized, destination, metadata_path, cached=True)

        pdf_url = f"{self.pdf_base_url}/{normalized}.pdf"
        self.pacer.wait()
        try:
            response = self.session.get(
                pdf_url,
                headers={**self.headers, "Accept": "application/pdf"},
                timeout=self.timeout_seconds,
                stream=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ArxivServiceError(f"arXiv PDF download failed: {exc}") from exc

        declared_size = int(response.headers.get("Content-Length") or 0)
        if declared_size > self.max_pdf_bytes:
            response.close()
            raise ArxivServiceError(
                f"arXiv PDF is {declared_size} bytes; limit is {self.max_pdf_bytes} bytes."
            )

        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        written = 0
        try:
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > self.max_pdf_bytes:
                        raise ArxivServiceError(
                            f"arXiv PDF exceeded the {self.max_pdf_bytes}-byte download limit."
                        )
                    output.write(chunk)
            if not self._is_pdf(temporary):
                raise ArxivServiceError("arXiv response was not a valid PDF payload.")
            temporary.replace(destination)
        finally:
            response.close()
            if temporary.exists():
                temporary.unlink()

        downloaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        metadata_path.write_text(
            json.dumps({
                "arxiv_id": normalized,
                "abs_url": f"https://arxiv.org/abs/{normalized}",
                "pdf_url": pdf_url,
                "sha256": _sha256(destination),
                "size_bytes": destination.stat().st_size,
                "downloaded_at": downloaded_at,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self._download_result(normalized, destination, metadata_path, cached=False)

    @staticmethod
    def build_search_query(
        query: str,
        *,
        author: str = "",
        categories: Iterable[str] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> str:
        clauses: list[str] = []
        query = _clean_text(query)
        author = _clean_text(author)
        if query:
            clauses.append(f'all:"{ArxivClient._escape_query_value(query)}"')
        if author:
            clauses.append(f'au:"{ArxivClient._escape_query_value(author)}"')

        safe_categories = [
            category.strip()
            for category in (categories or [])
            if re.fullmatch(r"[A-Za-z0-9.\-]+", str(category or "").strip())
        ]
        if safe_categories:
            category_query = " OR ".join(f"cat:{category}" for category in safe_categories[:8])
            clauses.append(f"({category_query})")

        if year_from is not None or year_to is not None:
            lower = max(1991, int(year_from or 1991))
            upper = min(2100, int(year_to or datetime.now().year))
            if lower > upper:
                raise ValueError("year_from must not be greater than year_to")
            clauses.append(f"submittedDate:[{lower}01010000 TO {upper}12312359]")

        if not clauses:
            raise ValueError("Provide a query, author, category, or year range.")
        return " AND ".join(clauses)

    @staticmethod
    def parse_feed(xml_content: bytes | str, *, fallback_query: str = "") -> ArxivSearchPage:
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as exc:
            raise ArxivServiceError(f"arXiv returned malformed Atom XML: {exc}") from exc

        namespaces = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
            "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
        }
        query_element = root.find("opensearch:Query", namespaces)
        query = (
            _clean_text(query_element.attrib.get("searchTerms"))
            if query_element is not None else ""
        ) or fallback_query
        total = ArxivClient._safe_int(_element_text(root, "opensearch:totalResults", namespaces))
        start = ArxivClient._safe_int(_element_text(root, "opensearch:startIndex", namespaces))
        page_size = ArxivClient._safe_int(_element_text(root, "opensearch:itemsPerPage", namespaces))
        papers: list[ArxivPaper] = []

        for entry in root.findall("atom:entry", namespaces):
            identity = _element_text(entry, "atom:id", namespaces)
            try:
                arxiv_id = normalize_arxiv_id(identity)
            except ValueError:
                continue
            links = entry.findall("atom:link", namespaces)
            abs_url = next(
                (str(link.attrib.get("href") or "") for link in links if link.attrib.get("rel") == "alternate"),
                f"https://arxiv.org/abs/{arxiv_id}",
            )
            pdf_url = next(
                (
                    str(link.attrib.get("href") or "")
                    for link in links
                    if link.attrib.get("type") == "application/pdf"
                    or link.attrib.get("title") == "pdf"
                ),
                f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            )
            authors = [
                _element_text(author, "atom:name", namespaces)
                for author in entry.findall("atom:author", namespaces)
            ]
            categories = [
                str(category.attrib.get("term") or "")
                for category in entry.findall("atom:category", namespaces)
                if category.attrib.get("term")
            ]
            primary = entry.find("arxiv:primary_category", namespaces)
            papers.append(ArxivPaper(
                arxiv_id=arxiv_id,
                title=_element_text(entry, "atom:title", namespaces) or "Untitled",
                abstract=_element_text(entry, "atom:summary", namespaces),
                authors=[author for author in authors if author],
                published=_element_text(entry, "atom:published", namespaces),
                updated=_element_text(entry, "atom:updated", namespaces),
                categories=categories,
                primary_category=str(primary.attrib.get("term") or "") if primary is not None else "",
                doi=_element_text(entry, "arxiv:doi", namespaces),
                journal_ref=_element_text(entry, "arxiv:journal_ref", namespaces),
                comment=_element_text(entry, "arxiv:comment", namespaces),
                abs_url=abs_url,
                pdf_url=pdf_url,
            ))

        return ArxivSearchPage(
            query=query,
            total_results=total or len(papers),
            start_index=start,
            items_per_page=page_size or len(papers),
            papers=papers,
        )

    def _get_atom(self, params: dict[str, Any]) -> bytes:
        self.pacer.wait()
        try:
            response = self.session.get(
                self.api_url,
                params=params,
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            raise ArxivServiceError(f"arXiv API request failed: {exc}") from exc

    @staticmethod
    def _escape_query_value(value: str) -> str:
        return value.replace("\\", " ").replace('"', " ").strip()

    @staticmethod
    def _safe_int(value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_pdf(path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size < 5:
                return False
            with path.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False

    @staticmethod
    def _resolve_cache_dir(destination_dir: str | Path | None) -> Path:
        if destination_dir:
            return Path(destination_dir).expanduser().resolve()
        if ARXIV_MCP_CACHE_DIR:
            configured = Path(ARXIV_MCP_CACHE_DIR).expanduser()
            if not configured.is_absolute():
                configured = get_storage_layout().repo_root / configured
            return configured.resolve()
        return get_storage_layout().source_dir("papers", "arxiv-cache").resolve()

    def _download_result(
        self,
        arxiv_id: str,
        path: Path,
        metadata_path: Path,
        *,
        cached: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "arxiv_id": arxiv_id,
            "cached": cached,
            "path": str(path),
            "metadata_path": str(metadata_path),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
            "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"{self.pdf_base_url}/{arxiv_id}.pdf",
        }


class WikiIngestionClient:
    """Submit an arXiv PDF to the already-running LLM-WIKI API."""

    def __init__(
        self,
        *,
        base_url: str = LLM_WIKI_API_URL,
        timeout_seconds: float = 120,
        session: requests.Session | Any | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.session = session or requests.Session()

    def submit_pdf(
        self,
        pdf_path: str | Path,
        *,
        source_url: str,
        pipeline: str = "wiki_compile",
        approval_mode: ApprovalMode = "risk",
    ) -> dict[str, Any]:
        path = Path(pdf_path).resolve()
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise ValueError(f"PDF file not found: {path}")
        if approval_mode not in {"risk", "manual", "auto"}:
            raise ValueError(f"Unsupported approval mode: {approval_mode}")
        try:
            with path.open("rb") as handle:
                response = self.session.post(
                    f"{self.base_url}/api/papers/ingest",
                    files={"file": (path.name, handle, "application/pdf")},
                    data={
                        "source_url": source_url,
                        "pipeline": pipeline or "wiki_compile",
                        "approval_mode": approval_mode,
                    },
                    timeout=self.timeout_seconds,
                )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise ArxivServiceError(
                "Could not submit the PDF to LLM-WIKI. Start the FastAPI service "
                f"at {self.base_url} and retry. Detail: {exc}"
            ) from exc
        except ValueError as exc:
            raise ArxivServiceError("LLM-WIKI returned a non-JSON ingestion response.") from exc
        if not isinstance(payload, dict):
            raise ArxivServiceError("LLM-WIKI returned an invalid ingestion response.")
        return payload

    def get_job(self, job_id: str) -> dict[str, Any]:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("job_id is required")
        try:
            response = self.session.get(
                f"{self.base_url}/api/papers/ingest/jobs/{job_id}",
                timeout=min(self.timeout_seconds, 30),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise ArxivServiceError(f"Could not read ingestion job {job_id}: {exc}") from exc
        except ValueError as exc:
            raise ArxivServiceError("LLM-WIKI returned a non-JSON job response.") from exc
        if not isinstance(payload, dict):
            raise ArxivServiceError("LLM-WIKI returned an invalid job response.")
        return payload


class ArxivMcpService:
    """Small facade used by MCP tool handlers."""

    def __init__(
        self,
        arxiv: ArxivClient | None = None,
        ingestion: WikiIngestionClient | None = None,
    ):
        self.arxiv = arxiv or ArxivClient()
        self.ingestion = ingestion or WikiIngestionClient()

    def import_paper(
        self,
        arxiv_id: str,
        *,
        approval_mode: ApprovalMode = "risk",
    ) -> dict[str, Any]:
        normalized = normalize_arxiv_id(arxiv_id)
        download = self.arxiv.download_pdf(normalized)
        submitted = self.ingestion.submit_pdf(
            download["path"],
            source_url=f"https://arxiv.org/abs/{normalized}",
            pipeline="wiki_compile",
            approval_mode=approval_mode,
        )
        return {
            "ok": bool(submitted.get("ok", True)),
            "arxiv_id": normalized,
            "download": download,
            "ingestion": submitted,
            "next": (
                "The PDF already exists in the Wiki."
                if submitted.get("already_exists")
                else "Use arxiv_ingestion_status with the returned job_id; parsing and Wiki compilation run asynchronously."
            ),
        }
