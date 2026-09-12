"""MinerU precision API adapter used when the arXiv HTML fast path is unavailable."""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from system.core.config import (
    MINERU_API_BASE_URL,
    MINERU_API_TOKEN,
    MINERU_MODEL_VERSION,
    MINERU_POLL_INTERVAL_SECONDS,
    MINERU_TIMEOUT_SECONDS,
)
from system.document.evidence import elements_from_blocks, matrix_to_markdown, stable_evidence_id
from system.document.models import ParsedDocument


class MinerUError(RuntimeError):
    pass


class MinerUParser:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.base_url = MINERU_API_BASE_URL.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {MINERU_API_TOKEN}",
            "Content-Type": "application/json",
        }

    @property
    def available(self) -> bool:
        return bool(MINERU_API_TOKEN)

    def parse_file(self, path: Path, *, source_key: str, remote_pdf_url: str = "") -> ParsedDocument:
        if not self.available:
            raise MinerUError("MINERU_API_TOKEN is not configured")
        started = time.perf_counter()
        if remote_pdf_url and remote_pdf_url.startswith(("http://", "https://")):
            task_id = self._submit_url(remote_pdf_url, source_key)
            result = self._wait_single(task_id)
        else:
            batch_id = self._submit_file(path, source_key)
            result = self._wait_batch(batch_id, path.name)
        archive = self._download_archive(str(result.get("full_zip_url") or ""))
        markdown, provider_metadata = _read_archive(archive)
        provider_figures = provider_metadata.pop("_figures", [])
        provider_tables = provider_metadata.pop("_tables", [])
        if len(markdown.strip()) < 1000:
            raise MinerUError("MinerU result did not contain a usable full.md")
        parsed = _from_markdown(markdown, source_key=source_key, source_path=str(path))
        parsed.figures = _merge_provider_figures(parsed.figures, provider_figures, source_key)
        _enrich_tables(parsed.tables, provider_tables)
        parsed.metadata.update({
            "parser": "mineru-vlm",
            "processing_time_s": round(time.perf_counter() - started, 3),
            "model_version": MINERU_MODEL_VERSION,
            "source_locator_kind": "mineru_block",
            "bbox_available": False,
            "provider_task_id": task_id if remote_pdf_url and remote_pdf_url.startswith(("http://", "https://")) else "",
            "provider_batch_id": batch_id if not (remote_pdf_url and remote_pdf_url.startswith(("http://", "https://"))) else "",
            **provider_metadata,
        })
        parsed.parser = "mineru-vlm"
        return parsed

    def _submit_url(self, url: str, source_key: str) -> str:
        payload = self._checked(self.session.post(
            f"{self.base_url}/extract/task",
            headers=self.headers,
            json={
                "url": url,
                "model_version": MINERU_MODEL_VERSION,
                "enable_formula": True,
                "enable_table": True,
                "is_ocr": False,
                "language": "en",
                "data_id": source_key[:96],
            },
            timeout=(20, 60),
        ))
        task_id = str((payload.get("data") or {}).get("task_id") or "")
        if not task_id:
            raise MinerUError("MinerU did not return a task_id")
        return task_id

    def _submit_file(self, path: Path, source_key: str) -> str:
        payload = self._checked(self.session.post(
            f"{self.base_url}/file-urls/batch",
            headers=self.headers,
            json={
                "files": [{"name": path.name, "data_id": source_key[:96], "is_ocr": False}],
                "model_version": MINERU_MODEL_VERSION,
                "enable_formula": True,
                "enable_table": True,
                "language": "en",
            },
            timeout=(20, 60),
        ))
        data = payload.get("data") or {}
        batch_id = str(data.get("batch_id") or "")
        upload_urls = data.get("file_urls") or data.get("files") or []
        if not batch_id or not upload_urls:
            raise MinerUError("MinerU did not return a batch_id and upload URL")
        try:
            with path.open("rb") as handle:
                response = self.session.put(str(upload_urls[0]), data=handle, timeout=(20, 180))
            response.raise_for_status()
        except requests.RequestException as exc:
            raise MinerUError(f"MinerU signed upload failed: {exc}") from exc
        return batch_id

    def _wait_single(self, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + MINERU_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            payload = self._checked(self.session.get(
                f"{self.base_url}/extract/task/{task_id}",
                headers=self.headers,
                timeout=(20, 60),
            ))
            result = payload.get("data") or {}
            state = str(result.get("state") or "unknown")
            if state == "done":
                return result
            if state == "failed":
                raise MinerUError(str(result.get("err_msg") or "MinerU extraction failed"))
            if state not in {"pending", "running", "converting", "uploading", "waiting-file"}:
                raise MinerUError(f"unexpected MinerU state: {state}")
            time.sleep(MINERU_POLL_INTERVAL_SECONDS)
        raise MinerUError(f"MinerU extraction exceeded {MINERU_TIMEOUT_SECONDS}s")

    def _wait_batch(self, batch_id: str, filename: str) -> dict[str, Any]:
        deadline = time.monotonic() + MINERU_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            payload = self._checked(self.session.get(
                f"{self.base_url}/extract-results/batch/{batch_id}",
                headers=self.headers,
                timeout=(20, 60),
            ))
            results = (payload.get("data") or {}).get("extract_result") or []
            result = next((item for item in results if item.get("file_name") == filename), results[0] if results else {})
            state = str(result.get("state") or "pending")
            if state == "done":
                return result
            if state == "failed":
                raise MinerUError(str(result.get("err_msg") or "MinerU extraction failed"))
            time.sleep(MINERU_POLL_INTERVAL_SECONDS)
        raise MinerUError(f"MinerU extraction exceeded {MINERU_TIMEOUT_SECONDS}s")

    def _download_archive(self, url: str) -> bytes:
        if not url:
            raise MinerUError("MinerU completed without a result archive URL")
        try:
            response = self.session.get(url, timeout=(20, 240))
            response.raise_for_status()
        except requests.RequestException as exc:
            raise MinerUError(f"MinerU result download failed: {exc}") from exc
        content = response.content
        if len(content) > 1024 * 1024 * 512:
            raise MinerUError("MinerU result archive exceeds 512 MiB")
        return content

    @staticmethod
    def _checked(response) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise MinerUError(f"MinerU API request failed: {exc}") from exc
        if payload.get("code") != 0:
            raise MinerUError(f"MinerU API error {payload.get('code')}: {payload.get('msg', 'unknown error')}")
        return payload


def _read_archive(content: bytes) -> tuple[str, dict[str, Any]]:
    try:
        bundle = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise MinerUError("MinerU returned an invalid zip archive") from exc
    members = bundle.infolist()
    if len(members) > 5000 or sum(item.file_size for item in members) > 1024 * 1024 * 1024:
        raise MinerUError("MinerU result archive exceeds safe extraction limits")
    markdown_files = [item for item in members if item.filename.lower().endswith(".md")]
    if not markdown_files:
        return "", {"archive_files": len(members)}
    preferred = next((item for item in markdown_files if Path(item.filename).name == "full.md"), None)
    selected = preferred or max(markdown_files, key=lambda item: item.file_size)
    markdown = bundle.read(selected).decode("utf-8", errors="replace")
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
    image_files = [
        {"path": item.filename, "size": item.file_size}
        for item in members
        if Path(item.filename).suffix.lower() in image_suffixes
    ]
    provider_figures, provider_tables = _content_list_artifacts(bundle, members)
    return markdown, {
        "archive_files": len(members),
        "markdown_file": selected.filename,
        # Keep an asset manifest for replay/audit without extracting the ZIP
        # into the repository. Figure descriptions are compiled from the
        # caption and nearby paper text below.
        "image_files": image_files[:2000],
        # Internal handoff to parse_file; popped before provider metadata is
        # persisted in the source packet.
        "_figures": provider_figures,
        "_tables": provider_tables,
    }


def _content_list_artifacts(bundle: zipfile.ZipFile, members: list[zipfile.ZipInfo]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    content_file = next(
        (
            item for item in members
            if Path(item.filename).name.lower() == "content_list.json"
            or (
                Path(item.filename).name.lower().endswith("_content_list.json")
                and not Path(item.filename).name.lower().endswith("_content_list_v2.json")
            )
        ),
        None,
    )
    if not content_file or content_file.file_size > 128 * 1024 * 1024:
        return [], []
    try:
        payload = json.loads(bundle.read(content_file).decode("utf-8", errors="replace"))
    except (ValueError, KeyError):
        return [], []
    items = payload if isinstance(payload, list) else payload.get("content_list", []) if isinstance(payload, dict) else []
    figures, tables = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("block_type") or "").lower()
        page = int(item.get("page_idx") or item.get("page") or 0)
        if "page_idx" in item:
            page += 1
        if kind in {"image", "figure"}:
            figures.append({
                "asset_path": str(item.get("img_path") or item.get("image_path") or ""),
                "caption": _join_provider_text(item.get("image_caption") or item.get("caption")),
                "source_text": _join_provider_text(
                    item.get("image_footnote") or item.get("footnote") or item.get("text")
                ),
                "page": page,
            })
        elif kind == "table":
            tables.append({
                "caption": _clean_table_caption(item.get("table_caption") or item.get("caption")),
                "page": page,
                "body": str(item.get("table_body") or item.get("body") or ""),
            })
    return figures, tables


def _join_provider_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(" ".join(str(item).split()) for item in value if str(item).strip())
    return " ".join(str(value or "").split())


def _clean_table_caption(value: Any) -> str:
    """Keep one numbered caption when MinerU groups adjacent captions."""
    text = _join_provider_text(value)
    matches = list(re.finditer(r"\bTable\s+\d+[A-Za-z]?\b", text, flags=re.IGNORECASE))
    if len(matches) >= 2:
        text = text[:matches[1].start()]
    return text.strip()


def _merge_provider_figures(
    markdown_figures: list[dict[str, Any]],
    provider_figures: list[dict[str, Any]],
    source_key: str,
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in markdown_figures]
    by_asset = {str(item.get("asset_path") or "").replace("\\", "/"): item for item in merged}
    for index, provider in enumerate(provider_figures):
        asset = str(provider.get("asset_path") or "").replace("\\", "/")
        target = by_asset.get(asset) if asset else (merged[index] if index < len(merged) else None)
        if target is None:
            ref = f"mineru/figure/provider/{index}"
            element_id = stable_evidence_id(source_key, "figure", ref, index)
            target = {
                "figure_id": f"fig-{element_id[3:]}",
                "element_id": element_id,
                "asset_path": asset,
                "caption": "",
                "section_path": [],
                "page": 0,
                "source_text": "",
                "metadata": {"source_locator_kind": "mineru_content_list", "source_ref": ref},
            }
            merged.append(target)
            if asset:
                by_asset[asset] = target
        if provider.get("caption"):
            target["caption"] = provider["caption"]
        if provider.get("source_text"):
            target["source_text"] = " ".join(
                value for value in (str(target.get("source_text") or ""), str(provider["source_text"])) if value
            )[:2400]
        if provider.get("page"):
            target["page"] = int(provider["page"])
    return merged


def _enrich_tables(tables: list[dict[str, Any]], provider_tables: list[dict[str, Any]]) -> None:
    raw_captions = [str(table.get("caption") or "") for table in tables]
    provider_numbers = {
        number
        for provider in provider_tables
        if (number := _caption_number(str(provider.get("caption") or "")))
    }
    orphan_captions = [
        caption for caption in raw_captions
        if (number := _caption_number(caption)) and number not in provider_numbers
    ]
    unused = set(range(len(provider_tables)))
    equal_counts = len(tables) == len(provider_tables)
    missing_caption_targets = []
    for index, table in enumerate(tables):
        table_tokens = _table_tokens(str(table.get("markdown") or ""))
        scored = []
        for provider_index in unused:
            body = str(provider_tables[provider_index].get("body") or "")
            provider_tokens = _table_tokens(body)
            if not table_tokens or not provider_tokens:
                continue
            overlap = len(table_tokens & provider_tokens) / max(1, min(len(table_tokens), len(provider_tokens)))
            scored.append((overlap, provider_index))
        best_score, best_index = max(scored, default=(0.0, -1))
        if best_score < 0.35:
            # Older MinerU bundles may omit table_body. Positional fallback is
            # safe only when both sequences have identical lengths.
            best_index = index if equal_counts and index in unused else -1
        if best_index < 0:
            continue
        unused.discard(best_index)
        provider = provider_tables[best_index]
        if provider.get("caption"):
            table["caption"] = provider["caption"]
        else:
            missing_caption_targets.append(table)
        if provider.get("page"):
            table["page"] = int(provider["page"])
            for cell in table.get("cells") or []:
                cell["page"] = int(provider["page"])
    # MinerU occasionally emits a table body before another table's caption.
    # content_list still identifies the bodies correctly but can omit one
    # caption. An otherwise-unused numbered caption can then be paired with the
    # only content-matched table that lacks one.
    if len(orphan_captions) == len(missing_caption_targets):
        for table, caption in zip(missing_caption_targets, orphan_captions):
            table["caption"] = caption


def _table_tokens(value: str) -> set[str]:
    text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True).lower()
    return set(re.findall(r"[a-z][a-z0-9_.-]+|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}", text))


def _caption_number(value: str) -> str:
    match = re.search(r"\bTable\s+(\d+[A-Za-z]?)\b", value or "", flags=re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _from_markdown(markdown: str, *, source_key: str, source_path: str) -> ParsedDocument:
    title_match = re.search(r"^#\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else Path(source_path).stem
    blocks = _markdown_blocks(markdown, source_path)
    elements = elements_from_blocks(blocks, source_key)
    tables = _html_tables(markdown, source_key)
    figures = _markdown_figures(markdown, source_key)
    abstract = ""
    abstract_match = re.search(r"(?:^|\n)#{1,4}\s*Abstract\s*\n(.+?)(?=\n#{1,4}\s|\Z)", markdown, re.I | re.S)
    if abstract_match:
        abstract = " ".join(abstract_match.group(1).split())[:3000]
    return ParsedDocument(
        title=title,
        text=abstract or " ".join(markdown.split())[:3000],
        markdown=markdown,
        metadata={},
        blocks=blocks,
        elements=elements,
        figures=figures,
        tables=tables,
    )


def _markdown_blocks(markdown: str, source_path: str) -> list[dict[str, Any]]:
    blocks = []
    current_section = "Body"
    for index, part in enumerate(re.split(r"\n\s*\n", markdown or "")):
        part = part.strip()
        if not part:
            continue
        heading = re.match(r"^#{1,6}\s+(.+)", part)
        if heading:
            current_section = heading.group(1).strip()
        image = re.search(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)", part)
        blocks.append({
            "page": 0,
            "section": current_section,
            "block_type": "heading" if heading else ("figure" if image else "text"),
            "text": part,
            "caption": _figure_caption(part, image.group(1) if image else "") if image else "",
            "metadata": {"source": source_path, "source_ref": f"mineru/{index}", "heading_path": [current_section]},
        })
    return blocks


def _markdown_figures(markdown: str, source_key: str) -> list[dict[str, Any]]:
    """Turn MinerU image references into text-grounded figure records.

    The chat model used by the compiler is text-only. We therefore preserve the
    asset locator and give it the caption plus nearby author-written discussion;
    the compiler prompt explicitly forbids guessing unseen visual values.
    """
    pattern = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
    figures: list[dict[str, Any]] = []
    for index, match in enumerate(pattern.finditer(markdown or "")):
        before = markdown[:match.start()]
        after = markdown[match.end():]
        headings = list(re.finditer(r"^#{1,6}\s+(.+?)\s*$", before, re.MULTILINE))
        section = headings[-1].group(1).strip() if headings else "Body"
        before_nearby = before[-900:]
        after_nearby = after[:1200]
        nearby = (before_nearby + "\n" + after_nearby).strip()
        nearby = pattern.sub("", nearby)
        caption = (
            _figure_caption(after_nearby)
            or _figure_caption("", match.group(1))
            or _figure_caption(before_nearby)
        )
        source_text = _figure_context(nearby, caption)
        ref = f"mineru/figure/{index}"
        element_id = stable_evidence_id(source_key, "figure", ref, index)
        figures.append({
            "figure_id": f"fig-{element_id[3:]}",
            "element_id": element_id,
            "asset_path": match.group(2).strip(),
            "caption": caption,
            "section_path": [section],
            "page": 0,
            "source_text": source_text,
            "metadata": {"source_locator_kind": "mineru_block", "source_ref": ref},
        })
    return figures


def _figure_caption(text: str, alt: str = "") -> str:
    caption = re.search(
        r"(?:^|\n)\s*((?:Figure|Fig\.?|图)\s*[A-Za-z0-9.:-]+[^\n]{0,700})",
        text or "",
        flags=re.IGNORECASE,
    )
    if caption:
        return " ".join(caption.group(1).split())
    alt = " ".join(str(alt or "").split()).strip()
    return "" if alt.lower() in {"image", "figure", "img"} else alt


def _figure_context(text: str, caption: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text or "")
    clean = re.sub(r"^#{1,6}\s+", "", clean, flags=re.MULTILINE)
    clean = " ".join(clean.split())
    if caption and clean.startswith(caption):
        clean = clean[len(caption):].lstrip(" .:：")
    return clean[:1800]


def _html_tables(markdown: str, source_key: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(markdown, "html.parser")
    records = []
    raw_tables = list(re.finditer(r"<table\b.*?</table>", markdown or "", flags=re.IGNORECASE | re.DOTALL))
    for index, table in enumerate(soup.find_all("table")):
        grid = []
        for row in table.find_all("tr"):
            grid.append([" ".join(cell.get_text(" ", strip=True).split()) for cell in row.find_all(["th", "td"], recursive=False)])
        width = max((len(row) for row in grid), default=0)
        if not width:
            continue
        grid = [row + [""] * (width - len(row)) for row in grid]
        headers, rows = [grid[0]], grid[1:]
        ref = f"mineru/table/{index}"
        element_id = stable_evidence_id(source_key, "table", ref, index)
        table_id = f"tbl-{element_id[3:]}"
        cells = []
        for row_index, row in enumerate(grid):
            for column_index, value in enumerate(row):
                cells.append({
                    "cell_id": stable_evidence_id(source_key, "table_cell", f"{table_id}:{row_index}:{column_index}"),
                    "table_id": table_id,
                    "row_index": row_index,
                    "column_index": column_index,
                    "text": value,
                    "column_header": row_index == 0,
                    "page": 0,
                    "bbox": {},
                })
        raw_match = raw_tables[index] if index < len(raw_tables) else None
        raw_start = raw_match.start() if raw_match else -1
        raw_end = raw_match.end() if raw_match else -1
        prefix = markdown[:raw_start] if raw_start >= 0 else ""
        suffix = markdown[raw_end:] if raw_end >= 0 else ""
        headings = list(re.finditer(r"^#{1,6}\s+(.+?)\s*$", prefix, re.MULTILINE))
        section = headings[-1].group(1).strip() if headings else ""
        caption = _table_caption(suffix[:700]) or _table_caption(prefix[-700:])
        records.append({
            "table_id": table_id,
            "element_id": element_id,
            "caption": caption,
            "section_path": [section] if section else [],
            "page": 0,
            "bbox": {},
            "headers": headers,
            "rows": rows,
            "markdown": matrix_to_markdown(headers, rows),
            "docling_ref": ref,
            "cells": cells,
            "metadata": {"source_locator_kind": "mineru_block", "num_rows": len(grid), "num_cols": width},
        })
    return records


def _table_caption(text: str) -> str:
    matches = re.findall(
        r"(?:^|\n)\s*((?:Table|Tab\.?|表)\s*[A-Za-z0-9.:-]+[^\n]{0,700})",
        text or "",
        flags=re.IGNORECASE,
    )
    return " ".join(matches[-1].split()) if matches else ""
