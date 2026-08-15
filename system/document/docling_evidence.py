"""Normalize DoclingDocument JSON into stable, addressable evidence records.

The adapter deliberately accepts several Docling schema variants.  Raw JSON is
kept verbatim by the storage layer; these records are a queryable projection,
not a replacement for the original conversion artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


HEADING_LABELS = {"title", "section_header", "heading"}
TEXT_COLLECTIONS = ("texts", "groups", "key_value_items", "form_items")


def stable_evidence_id(source_key: str, kind: str, ref: str, index: int = 0) -> str:
    seed = f"{source_key}|{kind}|{ref}|{index}".encode("utf-8")
    return f"ev-{hashlib.sha256(seed).hexdigest()[:24]}"


def normalize_docling_document(
    document: dict[str, Any] | None,
    *,
    source_key: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(elements, tables)`` from a DoclingDocument JSON object."""
    if not isinstance(document, dict) or not document:
        return [], []

    records: list[tuple[int, str, dict[str, Any]]] = []
    ordinal = 0
    for collection in TEXT_COLLECTIONS:
        for item in _as_list(document.get(collection)):
            if isinstance(item, dict):
                records.append((ordinal, collection, item))
                ordinal += 1
    for collection in ("pictures", "tables"):
        for item in _as_list(document.get(collection)):
            if isinstance(item, dict):
                records.append((ordinal, collection, item))
                ordinal += 1

    # Prefer Docling's body tree, which is the authoritative reading order and
    # correctly interleaves paragraphs, figures and tables.  A provenance sort
    # is only a compatibility fallback for older/minimal JSON exports.
    body_refs = _body_refs(document.get("body"))
    if body_refs:
        by_ref = {
            str(item.get("self_ref") or item.get("id") or ""): entry
            for entry in records
            for item in [entry[2]]
        }
        ordered = [by_ref[ref] for ref in body_refs if ref in by_ref]
        used = {id(entry[2]) for entry in ordered}
        ordered.extend(entry for entry in records if id(entry[2]) not in used)
        records = ordered
    heading_path: list[str] = []
    elements: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []

    for reading_order, (_, collection, item) in enumerate(records):
        label = str(item.get("label") or collection.rstrip("s") or "text")
        ref = str(item.get("self_ref") or item.get("id") or f"#/{collection}/{reading_order}")
        text = _item_text(item)
        if label in HEADING_LABELS and text:
            level = _heading_level(item)
            heading_path = heading_path[: max(level - 1, 0)] + [text]
        page, bbox = _provenance(item)
        element_type = "table" if collection == "tables" else "figure" if collection == "pictures" else label
        element_id = stable_evidence_id(source_key, element_type, ref, reading_order)
        caption = _caption(item, document)
        element = {
            "element_id": element_id,
            "element_type": element_type,
            "text": text,
            "caption": caption,
            "page": page,
            "bbox": bbox,
            "heading_path": list(heading_path),
            "parent_id": str(item.get("parent", {}).get("$ref") if isinstance(item.get("parent"), dict) else item.get("parent") or ""),
            "reading_order": reading_order,
            "docling_ref": ref,
            "metadata": {"label": label, "collection": collection},
        }
        elements.append(element)
        if collection == "tables":
            table = _normalize_table(item, element, source_key)
            if not element["text"]:
                element["text"] = table["markdown"]
            tables.append(table)
    return elements, tables


def _normalize_table(item: dict[str, Any], element: dict[str, Any], source_key: str) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else item
    raw_cells = _as_list(data.get("table_cells") or data.get("cells"))
    num_rows = _int(data.get("num_rows") or data.get("n_rows"), 0)
    num_cols = _int(data.get("num_cols") or data.get("n_cols"), 0)
    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_cells):
        if not isinstance(raw, dict):
            continue
        r0, r1 = _span(raw, "row")
        c0, c1 = _span(raw, "col")
        num_rows = max(num_rows, r1)
        num_cols = max(num_cols, c1)
        page, bbox = _provenance(raw)
        if not page:
            page, bbox = element["page"], dict(element["bbox"])
        parsed.append({
            "row_index": r0,
            "column_index": c0,
            "row_span": max(1, r1 - r0),
            "column_span": max(1, c1 - c0),
            "text": str(raw.get("text") or raw.get("value") or "").strip(),
            "row_header": bool(raw.get("row_header") or raw.get("row_header_level")),
            "column_header": bool(raw.get("column_header") or raw.get("column_header_level")),
            "page": page,
            "bbox": bbox,
        })

    matrix = [["" for _ in range(num_cols)] for _ in range(num_rows)]
    for cell in parsed:
        if cell["row_index"] < num_rows and cell["column_index"] < num_cols:
            matrix[cell["row_index"]][cell["column_index"]] = cell["text"]

    header_rows = sorted({cell["row_index"] for cell in parsed if cell["column_header"]})
    headers = [matrix[index] for index in header_rows if index < len(matrix)]
    rows = [row for index, row in enumerate(matrix) if index not in header_rows]
    if not headers and matrix:
        # Preserve data without guessing that the first row is a semantic header.
        rows = matrix

    table_id = f"tbl-{element['element_id'][3:]}"
    cells = []
    for index, cell in enumerate(parsed):
        cell["cell_id"] = stable_evidence_id(source_key, "table_cell", f"{table_id}:{cell['row_index']}:{cell['column_index']}", index)
        cell["table_id"] = table_id
        cells.append(cell)
    return {
        "table_id": table_id,
        "element_id": element["element_id"],
        "caption": element["caption"],
        "section_path": element["heading_path"],
        "page": element["page"],
        "bbox": element["bbox"],
        "headers": headers,
        "rows": rows,
        "markdown": _matrix_to_markdown(headers, rows),
        "docling_ref": element["docling_ref"],
        "cells": cells,
        "metadata": {"num_rows": num_rows, "num_cols": num_cols},
    }


def _matrix_to_markdown(headers: list[list[str]], rows: list[list[str]]) -> str:
    if not headers and not rows:
        return ""
    width = max((len(row) for row in headers + rows), default=0)
    header = (headers[-1] if headers else [f"column_{i + 1}" for i in range(width)])[:width]
    lines = ["| " + " | ".join(_escape_cell(value) for value in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    lines.extend("| " + " | ".join(_escape_cell(value) for value in row[:width]) + " |" for row in rows)
    return "\n".join(lines)


def _caption(item: dict[str, Any], document: dict[str, Any]) -> str:
    value = item.get("caption") or item.get("captions") or ""
    if isinstance(value, str):
        return value.strip()
    refs = _as_list(value)
    text_by_ref: dict[str, str] = {}
    for collection in TEXT_COLLECTIONS:
        for text_item in _as_list(document.get(collection)):
            if isinstance(text_item, dict):
                text_by_ref[str(text_item.get("self_ref") or text_item.get("id") or "")] = _item_text(text_item)
    values = []
    for ref in refs:
        key = str(ref.get("$ref") if isinstance(ref, dict) else ref)
        if text_by_ref.get(key):
            values.append(text_by_ref[key])
    return " ".join(values).strip()


def _item_text(item: dict[str, Any]) -> str:
    value = item.get("text") or item.get("orig") or item.get("content") or ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return ""


def _provenance(item: dict[str, Any]) -> tuple[int, dict[str, float]]:
    prov = item.get("prov") or item.get("provenance") or []
    if isinstance(prov, dict):
        prov = [prov]
    first = prov[0] if prov and isinstance(prov[0], dict) else {}
    page = _int(first.get("page_no") or first.get("page") or item.get("page_no") or item.get("page"), 0)
    bbox = first.get("bbox") or item.get("bbox") or {}
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        bbox = {"l": bbox[0], "t": bbox[1], "r": bbox[2], "b": bbox[3]}
    if not isinstance(bbox, dict):
        bbox = {}
    clean = {}
    aliases = {"left": "l", "top": "t", "right": "r", "bottom": "b", "x0": "l", "y0": "t", "x1": "r", "y1": "b"}
    for key, value in bbox.items():
        try:
            clean[aliases.get(str(key), str(key))] = float(value)
        except (TypeError, ValueError):
            continue
    return page, clean


def _span(cell: dict[str, Any], axis: str) -> tuple[int, int]:
    span = cell.get(f"{axis}_span")
    if isinstance(span, (list, tuple)) and len(span) >= 2:
        return _int(span[0], 0), _int(span[1], _int(span[0], 0) + 1)
    start = _int(cell.get(f"start_{axis}_offset_idx") or cell.get(f"{axis}_index") or cell.get(axis), 0)
    end = _int(cell.get(f"end_{axis}_offset_idx"), start + _int(span, 1))
    return start, max(end, start + 1)


def _heading_level(item: dict[str, Any]) -> int:
    return max(1, _int(item.get("level") or item.get("heading_level"), 1))


def _page(item: dict[str, Any]) -> int:
    return _provenance(item)[0]


def _top(item: dict[str, Any]) -> float:
    bbox = _provenance(item)[1]
    return float(bbox.get("t", bbox.get("b", 0.0)))


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _body_refs(body: Any) -> list[str]:
    refs: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if ref:
            refs.append(str(ref))
        children = node.get("children")
        if isinstance(children, list):
            visit(children)

    visit(body)
    return list(dict.fromkeys(refs))


def _escape_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def parse_json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
