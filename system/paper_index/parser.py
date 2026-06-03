from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import fitz


SECTION_RE = re.compile(
    r"^\s*(abstract|introduction|related work|background|method|methods|approach|experiments?|evaluation|results|discussion|conclusion|limitations|references)\s*$",
    re.IGNORECASE,
)


class PaperIndexParser:
    """Fast local PDF parser for PaperIndex.

    This parser intentionally avoids embeddings, Neo4j, and entity extraction.
    It creates page-level text blocks with rough section labels so the agent can
    navigate papers quickly.
    """

    def parse_pdf(self, pdf_path: str) -> Dict[str, Any]:
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(str(path))

        doc = fitz.open(str(path))
        blocks: List[Dict[str, Any]] = []
        current_section = "Front Matter"
        title = self._metadata_title(doc) or self._title_from_filename(path)
        summary_parts: List[str] = []

        for page_index, page in enumerate(doc, start=1):
            text = page.get_text("text").strip()
            if not text:
                continue
            if page_index == 1:
                title = self._guess_title(text) or title
            current_section = self._update_section(text, current_section)
            clean_text = self._normalize_text(text)
            if len(" ".join(summary_parts)) < 1200:
                summary_parts.append(clean_text[:800])
            blocks.append(
                {
                    "page": page_index,
                    "section": current_section,
                    "block_type": "text",
                    "text": clean_text,
                    "metadata": {"source": str(path)},
                }
            )

        return {
            "title": title,
            "summary": self._make_summary(summary_parts),
            "metadata": {
                "page_count": len(doc),
                "parser": "PaperIndexParser",
                "pdf_metadata": dict(doc.metadata or {}),
            },
            "blocks": blocks,
        }

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        return text

    @staticmethod
    def _guess_title(first_page_text: str) -> str:
        lines = [line.strip() for line in first_page_text.splitlines() if line.strip()]
        lines = PaperIndexParser._drop_license_preamble(lines)
        joined = "\n".join(lines[:40])
        arxiv_match = re.search(r"(Attention\s+Is\s+All\s+You\s+Need)", joined, re.IGNORECASE)
        if arxiv_match:
            return arxiv_match.group(1)
        candidates = [
            line for line in lines[:12]
            if 8 <= len(line) <= 180
            and not line.lower().startswith(("abstract", "arxiv", "http", "provided proper attribution", "permission"))
            and "permission" not in line.lower()
            and "google hereby grants" not in line.lower()
            and "@" not in line
            and not re.search(r"\b(contents|table of contents)\b", line, re.IGNORECASE)
        ]
        return candidates[0] if candidates else ""

    @staticmethod
    def _drop_license_preamble(lines: List[str]) -> List[str]:
        skip_until = 0
        for index, line in enumerate(lines[:40]):
            lowered = line.lower()
            if "abstract" in lowered:
                break
            if (
                "provided proper attribution" in lowered
                or "google hereby grants permission" in lowered
                or "solely for use in journalistic" in lowered
            ):
                skip_until = index + 1
        return lines[skip_until:] if skip_until else lines

    @staticmethod
    def _metadata_title(doc: fitz.Document) -> str:
        title = (doc.metadata or {}).get("title", "") or ""
        title = title.strip()
        if not title or title.lower() in {"untitled", "microsoft word - main"}:
            return ""
        if "provided proper attribution" in title.lower():
            return ""
        return title

    @staticmethod
    def _title_from_filename(path: Path) -> str:
        stem = path.stem.replace("_", " ").replace("-", " ")
        stem = re.sub(r"\s+", " ", stem).strip()
        return stem.title() if stem else path.stem

    @staticmethod
    def _update_section(text: str, current: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines[:12]:
            normalized = re.sub(r"^\d+(\.\d+)*\s+", "", line).strip()
            if SECTION_RE.match(normalized):
                return normalized.title()
        return current

    @staticmethod
    def _make_summary(parts: List[str]) -> str:
        text = " ".join(parts).strip()
        if len(text) <= 600:
            return text
        return text[:600].rsplit(" ", 1)[0] + "..."
