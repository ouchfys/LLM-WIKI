"""
Docling Parser — unified document parsing via remote service, local library, or off.

Three modes controlled by DOCLING_MODE env var:
  remote  → HTTP API at DOCLING_BASE_URL (Docker docling-serve)
  local   → import docling locally (requires pip install docling)
  off     → always unavailable, caller must use fallback

All modes return ParsedDocument. available() reflects the current mode's reachability.
"""

from __future__ import annotations

import html as _html
import json
import re
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests import RequestException, Timeout

from system.core.config import DOCLING_MODE, DOCLING_BASE_URL, DOCLING_TIMEOUT_SECONDS


@dataclass
class ParsedDocument:
    title: str = ""
    text: str = ""
    markdown: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    pages_or_items: List[Dict[str, Any]] = field(default_factory=list)
    parser: str = ""


class DoclingParser:
    """Parse PDFs, images and other documents through the Docling pipeline.

    Usage::

        parser = DoclingParser()
        if parser.available:
            doc = parser.parse_file("path/to/file.pdf")
            # doc.markdown, doc.text, doc.pages_or_items, doc.metadata
        else:
            # fallback to PaperIndexParser
    """

    def __init__(self):
        self.mode = DOCLING_MODE
        self.base_url = DOCLING_BASE_URL.rstrip("/")
        self.timeout = DOCLING_TIMEOUT_SECONDS
        self._available: Optional[bool] = None  # lazy check

    @property
    def available(self) -> bool:
        if self._available is not None:
            return self._available

        if self.mode == "off":
            self._available = False
        elif self.mode == "remote":
            self._available = self._check_remote_health()
        elif self.mode == "local":
            self._available = self._check_local_import()
        else:
            print(f"[DoclingParser] Unknown mode '{self.mode}', treating as off")
            self._available = False

        print(f"[DoclingParser] mode={self.mode} available={self._available}")
        return self._available

    def parse_file(self, path: str) -> ParsedDocument:
        """Parse a local file. Raises RuntimeError if not available."""
        if not self.available:
            raise RuntimeError(f"Docling is not available (mode={self.mode})")

        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))

        if self.mode == "remote":
            return self._parse_remote(file_path)
        if self.mode == "local":
            return self._parse_local(file_path)

        raise RuntimeError(f"Unknown Docling mode: {self.mode}")

    # ------------------------------------------------------------------
    #  Remote mode
    # ------------------------------------------------------------------

    def _parse_remote(self, file_path: Path) -> ParsedDocument:
        ext = file_path.suffix.lower()
        fmt_map = {".pdf": "pdf", ".jpg": "image", ".jpeg": "image",
                    ".png": "image", ".webp": "image", ".gif": "image",
                    ".docx": "docx", ".pptx": "pptx", ".html": "html"}
        from_format = fmt_map.get(ext, "image" if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else "pdf")

        payload = {
            "from_formats": from_format,
            "to_formats": ["md", "text"],
            "do_ocr": "true",
        }

        try:
            with open(file_path, "rb") as f:
                t0 = _time.time()
                resp = requests.post(
                    f"{self.base_url}/v1/convert/file",
                    files={"files": (file_path.name, f, "application/octet-stream")},
                    data=payload,
                    timeout=self.timeout,
                )
        except Timeout as exc:
            print(
                f"[DoclingParser] Remote parse timeout after {self.timeout}s "
                f"for {file_path.name} via {self.base_url}/v1/convert/file"
            )
            raise RuntimeError(
                f"Docling remote timeout after {self.timeout}s for {file_path.name}"
            ) from exc
        except RequestException as exc:
            print(
                f"[DoclingParser] Remote parse request failed for {file_path.name} "
                f"via {self.base_url}/v1/convert/file: {exc}"
            )
            raise RuntimeError(
                f"Docling remote request failed for {file_path.name}: {exc}"
            ) from exc

        if resp.status_code != 200:
            print(
                f"[DoclingParser] Remote parse non-200 for {file_path.name}: "
                f"status={resp.status_code}, body={resp.text[:500]}"
            )
            raise RuntimeError(
                f"Docling remote returned {resp.status_code}: {resp.text[:500]}"
            )

        try:
            result = resp.json()
        except json.JSONDecodeError as exc:
            print(
                f"[DoclingParser] Remote parse returned invalid JSON for {file_path.name}: "
                f"body={resp.text[:500]}"
            )
            raise RuntimeError(
                f"Docling remote returned invalid JSON for {file_path.name}"
            ) from exc
        doc = result.get("document", {})
        errors = result.get("errors", [])
        elapsed = _time.time() - t0

        md_content = (doc.get("md_content") or "").strip()
        text_content = (doc.get("text_content") or "").strip()
        # text_content from Docling serve is HTML; strip tags for plain text
        plain_text = _html_to_text(text_content) if text_content else ""

        title = self._derive_title_from_md(md_content) or file_path.stem

        return ParsedDocument(
            title=title,
            text=plain_text or md_content,
            markdown=md_content,
            metadata={
                "parser": "docling-remote",
                "source_path": str(file_path),
                "docling_mode": "remote",
                "base_url": self.base_url,
                "processing_time_s": round(elapsed, 2),
                "conversion_errors": errors,
            },
            pages_or_items=self._chunk_markdown_to_items(md_content, str(file_path)),
            parser="docling-remote",
        )

    # ------------------------------------------------------------------
    #  Local mode
    # ------------------------------------------------------------------

    def _parse_local(self, file_path: Path) -> ParsedDocument:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError:
            raise RuntimeError("docling is not installed locally. pip install docling")

        converter = DocumentConverter()
        result = converter.convert(str(file_path))
        doc = result.document

        title = self._derive_title_from_md(
            doc.export_to_markdown() if hasattr(doc, "export_to_markdown") else ""
        ) or file_path.stem

        markdown = ""
        if hasattr(doc, "export_to_markdown"):
            try:
                markdown = doc.export_to_markdown()
            except Exception:
                pass

        text = ""
        if hasattr(doc, "sections"):
            try:
                parts = [s.text for s in doc.sections if hasattr(s, "text") and s.text]
                text = "\n\n".join(parts)
            except Exception:
                pass
        if not text:
            text = _html_to_text(markdown)

        pages = []
        if hasattr(doc, "pages"):
            try:
                for pi, page in enumerate(doc.pages, start=1):
                    page_text = getattr(page, "text", "") or ""
                    if page_text.strip():
                        pages.append({
                            "page": pi, "section": "", "block_type": "text",
                            "text": re.sub(r"\s+", " ", page_text).strip(),
                            "metadata": {"source": str(file_path)},
                        })
            except Exception:
                pass
        if not pages:
            pages = self._chunk_markdown_to_items(markdown, str(file_path))

        return ParsedDocument(
            title=title,
            text=text or markdown,
            markdown=markdown,
            metadata={"parser": "docling-local", "source_path": str(file_path), "docling_mode": "local"},
            pages_or_items=pages,
            parser="docling-local",
        )

    # ------------------------------------------------------------------
    #  Health / availability checks
    # ------------------------------------------------------------------

    def _check_remote_health(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=10)
            if resp.status_code != 200:
                print(
                    f"[DoclingParser] Remote health check non-200: "
                    f"status={resp.status_code}, body={resp.text[:300]}"
                )
                return False
            return True
        except Timeout as exc:
            print(
                f"[DoclingParser] Remote health check timeout after 10s "
                f"for {self.base_url}/health: {exc}"
            )
            return False
        except RequestException as exc:
            print(f"[DoclingParser] Remote health check failed for {self.base_url}/health: {exc}")
            return False

    @staticmethod
    def _check_local_import() -> bool:
        try:
            from docling.document_converter import DocumentConverter  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_title_from_md(markdown: str) -> str:
        if not markdown:
            return ""
        match = re.search(r"^#{1,3}\s+(.+)", markdown, re.MULTILINE)
        if match:
            candidate = match.group(1).strip()
            if 3 <= len(candidate) <= 200:
                return candidate
        return ""

    @staticmethod
    def _chunk_markdown_to_items(markdown: str, source_path: str) -> List[Dict[str, Any]]:
        items = []
        paragraphs = re.split(r"\n\s*\n", markdown)
        current_section = ""
        for idx, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                continue
            heading = re.match(r"^#{1,3}\s*(.+)", para)
            if heading:
                current_section = heading.group(1).strip()
            items.append({
                "page": idx + 1,
                "section": current_section,
                "block_type": "text",
                "text": para[:3000],
                "metadata": {"source": source_path},
            })
        return items


def _html_to_text(html_text: str) -> str:
    """Strip HTML tags and unescape entities for plain text extraction."""
    if not html_text:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", "", html_text or "", flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
