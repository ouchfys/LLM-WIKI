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
        if len(markdown.strip()) < 1000:
            raise MinerUError("MinerU result did not contain a usable full.md")
        parsed = _from_markdown(markdown, source_key=source_key, source_path=str(path))
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
    return markdown, {"archive_files": len(members), "markdown_file": selected.filename}


def _from_markdown(markdown: str, *, source_key: str, source_path: str) -> ParsedDocument:
    title_match = re.search(r"^#\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else Path(source_path).stem
    blocks = _markdown_blocks(markdown, source_path)
    elements = elements_from_blocks(blocks, source_key)
    tables = _html_tables(markdown, source_key)
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
        blocks.append({
            "page": 0,
            "section": current_section,
            "block_type": "heading" if heading else "text",
            "text": part[:6000],
            "metadata": {"source": source_path, "source_ref": f"mineru/{index}", "heading_path": [current_section]},
        })
    return blocks


def _html_tables(markdown: str, source_key: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(markdown, "html.parser")
    records = []
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
        records.append({
            "table_id": table_id,
            "element_id": element_id,
            "caption": "",
            "section_path": [],
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
