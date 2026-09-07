"""Choose the fastest trustworthy parser and expose explicit degradation."""

from __future__ import annotations

from pathlib import Path

from system.core.config import PAPER_PARSER_MODE
from system.document.arxiv_html import ArxivHtmlError, ArxivHtmlParser, arxiv_id_from_url
from system.document.evidence import elements_from_blocks
from system.document.mineru import MinerUError, MinerUParser
from system.document.models import ParsedDocument
from system.paper_index.parser import PaperIndexParser


class PaperParserRouter:
    def __init__(self, html_parser=None, mineru_parser=None, fallback_parser=None):
        self.html = html_parser or ArxivHtmlParser()
        self.mineru = mineru_parser or MinerUParser()
        self.fallback = fallback_parser or PaperIndexParser()

    def parse(self, pdf_path: Path, *, source_url: str = "", source_key: str = "") -> ParsedDocument:
        attempts: list[dict[str, str]] = []
        mode = PAPER_PARSER_MODE
        arxiv_id = arxiv_id_from_url(source_url)
        if arxiv_id and mode in {"auto", "arxiv_html"}:
            try:
                parsed = self.html.parse(source_url, source_key=source_key)
                parsed.metadata["parser_attempts"] = attempts + [{"parser": "arxiv-html", "status": "selected"}]
                return parsed
            except ArxivHtmlError as exc:
                attempts.append({"parser": "arxiv-html", "status": "rejected", "reason": str(exc)[:400]})
                if mode == "arxiv_html":
                    return self._fallback(pdf_path, source_key, attempts)

        if mode in {"auto", "mineru"} and self.mineru.available:
            try:
                remote_pdf_url = f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else ""
                parsed = self.mineru.parse_file(pdf_path, source_key=source_key, remote_pdf_url=remote_pdf_url)
                parsed.metadata["parser_attempts"] = attempts + [{"parser": "mineru-vlm", "status": "selected"}]
                return parsed
            except MinerUError as exc:
                attempts.append({"parser": "mineru-vlm", "status": "failed", "reason": str(exc)[:400]})

        return self._fallback(pdf_path, source_key, attempts)

    def parse_local_fallback(self, pdf_path: Path, *, source_key: str = "") -> ParsedDocument:
        return self._fallback(pdf_path, source_key, [])

    def _fallback(self, pdf_path: Path, source_key: str, attempts: list[dict[str, str]]) -> ParsedDocument:
        raw = self.fallback.parse_pdf(str(pdf_path))
        blocks = raw.get("blocks") or []
        return ParsedDocument(
            title=str(raw.get("title") or pdf_path.stem),
            text=str(raw.get("summary") or ""),
            markdown="",
            metadata={
                **dict(raw.get("metadata") or {}),
                "parser": "pymupdf-fallback",
                "degraded": True,
                "degradation_reason": attempts[-1].get("reason") if attempts else "precision parser disabled or unavailable",
                "parser_attempts": attempts + [{"parser": "pymupdf-fallback", "status": "selected"}],
                "bbox_available": False,
            },
            blocks=blocks,
            elements=elements_from_blocks(blocks, source_key),
            parser="pymupdf-fallback",
        )
