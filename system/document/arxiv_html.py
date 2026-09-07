"""Fast, structure-preserving parser for arXiv's LaTeXML HTML."""

from __future__ import annotations

import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from system.core.config import ARXIV_HTML_BASE_URL, ARXIV_HTML_TIMEOUT_SECONDS, ARXIV_USER_AGENT
from system.document.evidence import matrix_to_markdown, stable_evidence_id
from system.document.models import ParsedDocument


class ArxivHtmlError(RuntimeError):
    pass


def arxiv_id_from_url(value: str) -> str:
    match = re.search(r"arxiv\.org/(?:abs|pdf|html)/([^?#]+)", str(value or ""), re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).removesuffix(".pdf").strip("/")


class ArxivHtmlParser:
    def __init__(self, session=None):
        self.session = session or requests.Session()

    def parse(self, source_url: str, *, source_key: str) -> ParsedDocument:
        arxiv_id = arxiv_id_from_url(source_url)
        if not arxiv_id:
            raise ArxivHtmlError("source is not an arXiv paper URL")
        html_url = f"{ARXIV_HTML_BASE_URL.rstrip('/')}/{arxiv_id}"
        started = time.perf_counter()
        try:
            response = self.session.get(
                html_url,
                headers={"User-Agent": ARXIV_USER_AGENT, "Accept": "text/html"},
                timeout=(10, ARXIV_HTML_TIMEOUT_SECONDS),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ArxivHtmlError(f"arXiv HTML request failed: {exc}") from exc
        return self.parse_html(
            response.text,
            source_url=str(response.url or html_url),
            source_key=source_key,
            elapsed_seconds=time.perf_counter() - started,
            arxiv_id=arxiv_id,
        )

    def parse_html(
        self,
        html: str,
        *,
        source_url: str,
        source_key: str,
        elapsed_seconds: float = 0.0,
        arxiv_id: str = "",
    ) -> ParsedDocument:
        soup = BeautifulSoup(html or "", "html.parser")
        article = soup.select_one("article.ltx_document") or soup.select_one(".ltx_document")
        if article is None:
            raise ArxivHtmlError("arXiv did not return a structured paper article")

        title_node = article.select_one("h1.ltx_title") or article.select_one("h1")
        title = _clean(title_node.get_text(" ", strip=True) if title_node else "")
        title = re.sub(r"^Title:\s*", "", title, flags=re.IGNORECASE)
        paragraph_count = len(article.select("p.ltx_p")) or len(article.find_all("p"))
        headings = [_clean(node.get_text(" ", strip=True)) for node in article.find_all(re.compile(r"^h[1-6]$"))]
        body_text = _clean(article.get_text(" ", strip=True))
        warnings = article.select(".ltx_ERROR, .ltx_missing")
        math_nodes = article.find_all("math")
        math_with_tex = sum(bool(_latex(node)) for node in math_nodes)
        lowered_headings = " ".join(headings).lower()
        quality = {
            "title_present": bool(title),
            "body_chars": len(body_text),
            "paragraphs": paragraph_count,
            "headings": len(headings),
            "has_abstract": "abstract" in lowered_headings or bool(article.select_one(".ltx_abstract")),
            "has_references": any(word in lowered_headings for word in ("references", "bibliography")),
            "conversion_warnings": len(warnings),
            "math_nodes": len(math_nodes),
            "math_with_tex": math_with_tex,
        }
        failures = []
        if not quality["title_present"]:
            failures.append("missing title")
        if quality["body_chars"] < 5000 or paragraph_count < 10:
            failures.append("paper body is too short")
        if quality["headings"] < 3 or not quality["has_abstract"] or not quality["has_references"]:
            failures.append("section structure is incomplete")
        if len(math_nodes) >= 5 and math_with_tex / len(math_nodes) < 0.9:
            failures.append("formula source coverage is incomplete")
        if failures:
            raise ArxivHtmlError("; ".join(failures))

        blocks, elements, tables, markdown = _extract_article(
            article,
            source_url=source_url,
            source_key=source_key,
        )
        abstract_node = article.select_one(".ltx_abstract")
        abstract = _clean(abstract_node.get_text(" ", strip=True) if abstract_node else "")
        abstract = re.sub(r"^Abstract\s*", "", abstract, flags=re.IGNORECASE)
        return ParsedDocument(
            title=title,
            text=abstract or body_text,
            markdown=markdown,
            metadata={
                "parser": "arxiv-html",
                "arxiv_id": arxiv_id,
                "source_url": source_url,
                "source_locator_kind": "html_anchor",
                "processing_time_s": round(elapsed_seconds, 3),
                "quality_gate": quality,
                "bbox_available": False,
            },
            blocks=blocks,
            elements=elements,
            tables=tables,
            parser="arxiv-html",
        )


def _extract_article(article: Tag, *, source_url: str, source_key: str):
    blocks: list[dict] = []
    elements: list[dict] = []
    tables: list[dict] = []
    markdown_parts: list[str] = []
    heading_path: list[str] = []
    candidates = article.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table"])
    for node in candidates:
        if node.name == "table" and node.find_parent("table") is not None:
            continue
        if node.name != "table" and node.find_parent("table") is not None:
            continue
        if node.name == "li" and node.find_parent("li") is not None:
            continue
        if node.name == "p" and node.find_parent("p") is not None:
            continue
        ref = str(node.get("id") or (node.parent.get("id") if node.parent else "") or f"node-{len(elements)}")
        if re.fullmatch(r"h[1-6]", node.name):
            level = int(node.name[1])
            text = _inline_text(node, source_url)
            if not text:
                continue
            heading_path = heading_path[: max(level - 1, 0)] + [text]
            markdown_parts.extend([f"{'#' * level} {text}", ""])
            block_type = "heading"
        elif node.name == "table":
            table = _table_record(node, source_key, ref, len(tables), heading_path)
            if not table["markdown"]:
                continue
            tables.append(table)
            text = table["markdown"]
            markdown_parts.extend(([f"**{table['caption']}**", ""] if table["caption"] else []) + [text, ""])
            block_type = "table"
        else:
            text = _inline_text(node, source_url)
            if not text:
                continue
            prefix = "- " if node.name == "li" else ""
            markdown_parts.extend([prefix + text, ""])
            block_type = "list_item" if node.name == "li" else "text"
        element_id = stable_evidence_id(source_key, block_type, ref, len(elements))
        metadata = {"source_ref": ref, "source_url": f"{source_url}#{ref}", "heading_path": list(heading_path)}
        block = {
            "page": 0,
            "section": heading_path[-1] if heading_path else "Body",
            "block_type": block_type,
            "text": text,
            "metadata": {**metadata, "evidence_id": element_id},
        }
        blocks.append(block)
        elements.append({
            "element_id": element_id,
            "element_type": block_type,
            "text": text,
            "caption": tables[-1]["caption"] if block_type == "table" else "",
            "page": 0,
            "bbox": {},
            "heading_path": list(heading_path),
            "parent_id": "",
            "reading_order": len(elements),
            "docling_ref": ref,
            "metadata": metadata,
        })
        if block_type == "table":
            tables[-1]["element_id"] = element_id
    # The block projection powers evidence lookup; recursive conversion keeps
    # displayed equations, captions and other content that is not wrapped in a
    # paragraph node.
    markdown = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", _markdown_node(article, source_url)).strip() + "\n"
    return blocks, elements, tables, markdown


def _table_record(node: Tag, source_key: str, ref: str, index: int, heading_path: list[str]) -> dict:
    grid: list[list[str]] = []
    header_flags: list[bool] = []
    spans: dict[tuple[int, int], str] = {}
    for row_index, row_node in enumerate(node.find_all("tr")):
        if row_node.find_parent("table") is not node:
            continue
        row: list[str] = []
        column = 0
        while (row_index, column) in spans:
            row.append(spans[(row_index, column)])
            column += 1
        cells = row_node.find_all(["th", "td"], recursive=False)
        header_flags.append(any(cell.name == "th" for cell in cells))
        for cell in cells:
            while (row_index, column) in spans:
                row.append(spans[(row_index, column)])
                column += 1
            value = _inline_text(cell, "")
            rowspan = max(1, int(cell.get("rowspan", 1) or 1))
            colspan = max(1, int(cell.get("colspan", 1) or 1))
            for dx in range(colspan):
                row.append(value if dx == 0 else "")
                for dy in range(1, rowspan):
                    spans[(row_index + dy, column + dx)] = value if dx == 0 else ""
            column += colspan
        grid.append(row)
    width = max((len(row) for row in grid), default=0)
    grid = [row + [""] * (width - len(row)) for row in grid]
    first_header = 0 if grid and header_flags and header_flags[0] else -1
    headers = [grid[first_header]] if first_header == 0 else []
    rows = grid[1:] if first_header == 0 else grid
    figure = node.find_parent("figure")
    caption_node = figure.find("figcaption") if figure else None
    caption = _clean(caption_node.get_text(" ", strip=True) if caption_node else "")
    table_id = f"tbl-{stable_evidence_id(source_key, 'table', ref, index)[3:]}"
    cells = []
    for row_index, row in enumerate(grid):
        for column_index, value in enumerate(row):
            cells.append({
                "cell_id": stable_evidence_id(source_key, "table_cell", f"{table_id}:{row_index}:{column_index}"),
                "table_id": table_id,
                "row_index": row_index,
                "column_index": column_index,
                "text": value,
                "column_header": row_index == first_header,
                "row_header": False,
                "page": 0,
                "bbox": {},
            })
    return {
        "table_id": table_id,
        "element_id": "",
        "caption": caption,
        "section_path": list(heading_path),
        "page": 0,
        "bbox": {},
        "headers": headers,
        "rows": rows,
        "markdown": matrix_to_markdown(headers, rows),
        "docling_ref": ref,
        "cells": cells,
        "metadata": {"source_locator_kind": "html_anchor", "source_ref": ref, "num_rows": len(grid), "num_cols": width},
    }


def _inline_text(node: Tag, base_url: str) -> str:
    clone = BeautifulSoup(str(node), "html.parser")
    for math in clone.find_all("math"):
        latex = _latex(math)
        math.replace_with(NavigableString(f"${latex}$" if latex else math.get_text(" ", strip=True)))
    for image in clone.find_all("img"):
        alt = image.get("alt", "")
        src = urljoin(base_url, image.get("src", "")) if base_url else image.get("src", "")
        image.replace_with(NavigableString(f"![{alt}]({src})" if src else alt))
    return _clean(clone.get_text(" ", strip=True))


def _markdown_node(node, base_url: str) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag) or node.name in {"script", "style", "nav"}:
        return ""
    if node.name == "math":
        latex = _latex(node)
        marker = "$$" if node.get("display") == "block" else "$"
        return f"{marker}{latex}{marker}" if latex else node.get_text(" ", strip=True)
    if node.name == "table" and node.find_parent("table") is None:
        return "\n\n" + _table_record(node, "markdown", str(node.get("id") or "table"), 0, [])["markdown"] + "\n\n"
    if node.name == "img":
        src = urljoin(base_url, node.get("src", ""))
        return f"![{node.get('alt', '')}]({src})" if src else str(node.get("alt") or "")
    children = "".join(_markdown_node(child, base_url) for child in node.children)
    if re.fullmatch(r"h[1-6]", node.name):
        return f"\n\n{'#' * int(node.name[1])} {children.strip()}\n\n"
    if node.name == "a":
        href = node.get("href")
        return f"[{children}]({urljoin(base_url, href)})" if href else children
    if node.name in {"b", "strong"}:
        return f"**{children}**"
    if node.name in {"i", "em"}:
        return f"*{children}*"
    if node.name == "br":
        return "\n"
    if node.name == "li":
        return f"\n- {children.strip()}\n"
    if node.name == "pre":
        return f"\n\n```\n{node.get_text()}\n```\n\n"
    if node.name in {"p", "section", "figure", "figcaption", "blockquote", "ul", "ol"}:
        return f"\n\n{children.strip()}\n\n"
    return children


def _latex(node: Tag) -> str:
    annotation = node.find("annotation", attrs={"encoding": "application/x-tex"})
    return str(node.get("alttext") or (annotation.get_text() if annotation else "")).strip()


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
