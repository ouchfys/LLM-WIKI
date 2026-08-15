"""First-class table resolution and sandboxed cross-paper analysis."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import duckdb

from system.core.llm_call import invoke_deterministic, invoke_structured
import pandas as pd

from system.wiki.paper_pipeline.store import PaperWikiPipelineStore


@dataclass
class TableResolution:
    table_id: str
    source_packet_id: str
    source_title: str
    caption: str
    page: int
    section_path: list[str]
    headers: list[list[str]]
    rows: list[list[str]]
    score: float
    match_reason: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class TableResolver:
    def __init__(self, store: PaperWikiPipelineStore):
        self.store = store

    def resolve(
        self,
        query: str,
        *,
        card_ids: list[str] | None = None,
        source_packet_ids: list[str] | None = None,
        limit: int = 6,
    ) -> list[TableResolution]:
        scope = self._source_scope(card_ids or [], source_packet_ids or [])
        terms = _terms(query)
        with self.store._connect() as conn:
            sql = """SELECT t.*, sp.title AS source_title
                     FROM document_tables t
                     JOIN source_packets sp ON sp.id = t.source_packet_id"""
            params: list[Any] = []
            if scope:
                sql += " WHERE t.source_packet_id IN (" + ",".join("?" for _ in scope) + ")"
                params.extend(scope)
            rows = conn.execute(sql, params).fetchall()
        ranked: list[TableResolution] = []
        for row in rows:
            headers = self.store.load_json(row["headers_json"])
            values = self.store.load_json(row["rows_json"])
            section_path = self.store.load_json(row["section_path_json"])
            fields = {
                "source title": str(row["source_title"] or ""),
                "caption": str(row["caption"] or ""),
                "section": " ".join(section_path),
                "headers": " ".join(str(cell) for header in headers for cell in header),
                "cells": " ".join(str(cell) for value_row in values[:80] for cell in value_row),
            }
            weights = {"source title": 2.0, "caption": 3.0, "section": 1.8, "headers": 2.5, "cells": 0.8}
            score = 0.0
            reasons = []
            for label, text in fields.items():
                matched = terms & _terms(text)
                if matched:
                    score += weights[label] * len(matched) / max(len(terms), 1)
                    reasons.append(f"{label}: {', '.join(sorted(matched)[:6])}")
            if not terms:
                score = 0.1
                reasons.append("bounded source scope")
            if score > 0:
                ranked.append(TableResolution(
                    table_id=row["id"], source_packet_id=row["source_packet_id"],
                    source_title=row["source_title"], caption=row["caption"], page=int(row["page"] or 0),
                    section_path=section_path, headers=headers, rows=values, score=round(score, 4),
                    match_reason=reasons,
                ))
        ranked.sort(key=lambda item: (-item.score, item.source_title, item.page, item.table_id))
        return ranked[: max(1, min(int(limit), 20))]

    def evidence_rows(self, table_ids: list[str]) -> list[dict[str, Any]]:
        if not table_ids:
            return []
        placeholders = ",".join("?" for _ in table_ids)
        with self.store._connect() as conn:
            tables = conn.execute(
                f"""SELECT t.*, sp.title AS source_title FROM document_tables t
                    JOIN source_packets sp ON sp.id=t.source_packet_id
                    WHERE t.id IN ({placeholders})""", table_ids,
            ).fetchall()
            output = []
            for table in tables:
                cells = conn.execute(
                    "SELECT * FROM document_table_cells WHERE table_id=? ORDER BY row_index,column_index",
                    (table["id"],),
                ).fetchall()
                header_by_col: dict[int, str] = {}
                row_header: dict[int, str] = {}
                for cell in cells:
                    if cell["column_header"] and str(cell["text"] or "").strip():
                        for column in range(int(cell["column_index"]), int(cell["column_index"]) + int(cell["column_span"] or 1)):
                            previous = header_by_col.get(column, "")
                            current = str(cell["text"] or "").strip()
                            header_by_col[column] = " / ".join(value for value in (previous, current) if value)
                    if cell["row_header"] and str(cell["text"] or "").strip():
                        for row in range(int(cell["row_index"]), int(cell["row_index"]) + int(cell["row_span"] or 1)):
                            previous = row_header.get(row, "")
                            current = str(cell["text"] or "").strip()
                            row_header[row] = " / ".join(value for value in (previous, current) if value)
                for cell in cells:
                    value = str(cell["text"] or "")
                    output.append({
                        "source_packet_id": table["source_packet_id"],
                        "source_title": table["source_title"],
                        "table_id": table["id"],
                        "caption": table["caption"],
                        "page": int(cell["page"] or table["page"] or 0),
                        "row_index": int(cell["row_index"]),
                        "column_index": int(cell["column_index"]),
                        "row_label": row_header.get(int(cell["row_index"]), ""),
                        "column_label": header_by_col.get(int(cell["column_index"]), ""),
                        "value": value,
                        "numeric_value": _numeric(value),
                        "cell_id": cell["id"],
                        "bbox": self.store.load_json(cell["bbox_json"]),
                    })
        return output

    def _source_scope(self, card_ids: list[str], source_ids: list[str]) -> list[str]:
        result = [value for value in source_ids if value]
        if card_ids:
            placeholders = ",".join("?" for _ in card_ids)
            with self.store._connect() as conn:
                rows = conn.execute(
                    f"SELECT DISTINCT source_packet_id FROM wiki_card_sources WHERE card_id IN ({placeholders})",
                    card_ids,
                ).fetchall()
            result.extend(str(row["source_packet_id"]) for row in rows if row["source_packet_id"])
        return list(dict.fromkeys(result))


class ReadOnlyTableEngine:
    BLOCKED = re.compile(
        r"\b(insert|update|delete|drop|alter|create|copy|attach|detach|install|load|export|import|pragma|call|set|vacuum|read_[a-z_]+|sqlite_scan|postgres_scan)\b",
        flags=re.IGNORECASE,
    )

    def execute(self, sql: str, rows: list[dict[str, Any]], limit: int = 200) -> list[dict[str, Any]]:
        query = (sql or "").strip()
        if not re.match(r"^(select|with)\b", query, flags=re.IGNORECASE):
            raise ValueError("Only SELECT/CTE queries are allowed.")
        if ";" in query.rstrip(";") or self.BLOCKED.search(query):
            raise ValueError("Query contains a blocked operation or external-access function.")
        frame = pd.DataFrame(rows, columns=[
            "source_packet_id", "source_title", "table_id", "caption", "page",
            "row_index", "column_index", "row_label", "column_label", "value",
            "numeric_value", "cell_id", "bbox",
        ])
        frame["bbox"] = frame["bbox"].map(lambda value: json.dumps(value or {}, ensure_ascii=False))
        connection = duckdb.connect(database=":memory:", config={"enable_external_access": "false"})
        try:
            connection.register("evidence_cells_df", frame)
            connection.execute("CREATE TABLE evidence_cells AS SELECT * FROM evidence_cells_df")
            cursor = connection.execute(f"SELECT * FROM ({query.rstrip(';')}) AS result LIMIT {max(1, min(int(limit), 1000))}")
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, record)) for record in cursor.fetchall()]
        finally:
            connection.close()


class TableQuestionAnswerer:
    def __init__(self, store: PaperWikiPipelineStore, llm=None):
        self.resolver = TableResolver(store)
        self.engine = ReadOnlyTableEngine()
        self.llm = llm

    def answer(
        self,
        question: str,
        *,
        card_ids: list[str] | None = None,
        source_packet_ids: list[str] | None = None,
        sql: str = "",
        limit: int = 6,
    ) -> dict[str, Any]:
        resolved = self.resolver.resolve(
            question, card_ids=card_ids, source_packet_ids=source_packet_ids, limit=limit,
        )
        table_ids = [item.table_id for item in resolved]
        cells = self.resolver.evidence_rows(table_ids)
        planned_sql = sql.strip()
        if not planned_sql and self.llm and cells:
            planned_sql = self._plan_sql(question, resolved, cells)
        execution_error = ""
        used_fallback = False
        if planned_sql:
            try:
                result_rows = self.engine.execute(planned_sql, cells)
            except Exception as exc:
                execution_error = str(exc)[:500]
                result_rows = []
            if not result_rows:
                result_rows = _rank_cells(question, cells, limit=30)
                used_fallback = True
        else:
            result_rows = _rank_cells(question, cells, limit=30)
        citations = _citations(_citation_rows(question, result_rows, cells))
        answer = self._render_answer(question, result_rows, citations, planned_sql)
        return {
            "answer": answer,
            "sql": planned_sql,
            "tables": [item.as_dict() for item in resolved],
            "rows": result_rows,
            "citations": citations,
            "engine": "table-resolver-fallback" if used_fallback else "duckdb-read-only" if planned_sql else "table-resolver",
            "sql_error": execution_error,
        }

    def _plan_sql(
        self,
        question: str,
        tables: list[TableResolution],
        cells: list[dict[str, Any]],
    ) -> str:
        schema = (
            "evidence_cells(source_packet_id, source_title, table_id, caption, page, "
            "row_index, column_index, row_label, column_label, value, numeric_value, cell_id, bbox)"
        )
        table_context = []
        for item in tables:
            table_cells = [cell for cell in cells if cell.get("table_id") == item.table_id]
            table_context.append({
                "table_id": item.table_id,
                "source_title": item.source_title,
                "caption": item.caption,
                "headers": item.headers,
                "row_labels": list(dict.fromkeys(
                    str(cell.get("row_label") or "") for cell in table_cells if cell.get("row_label")
                ))[:80],
                "column_labels": list(dict.fromkeys(
                    str(cell.get("column_label") or "") for cell in table_cells if cell.get("column_label")
                ))[:40],
                "sample_rows": item.rows[:14],
            })
        prompt = (
            "Generate one DuckDB SELECT query that answers the question from evidence_cells. "
            "Use only the listed table and columns; no external functions. Text labels often contain "
            "qualifiers, so use lower(row_label) LIKE and lower(column_label) LIKE instead of guessed "
            "exact equality. Use only table IDs and labels shown below. The SELECT must retain "
            "source_title, table_id, caption, page, row_index, column_index, row_label, column_label, "
            "value, numeric_value, and cell_id so every result remains auditable. Return strict JSON "
            "as {\"sql\":\"SELECT ...\"}.\n"
            f"Schema: {schema}\nQuestion: {question}\nTables: "
            f"{json.dumps(table_context, ensure_ascii=False)}"
        )
        try:
            payload = _json_object(
                invoke_structured(self.llm, prompt, temperature=0.0, max_tokens=700)
            )
            return str(payload.get("sql") or "").strip()
        except Exception:
            return ""

    def _render_answer(
        self,
        question: str,
        rows: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        sql: str,
    ) -> str:
        if not rows:
            return "没有在限定的结构化表格证据中找到可以回答该问题的行或单元格。"
        if self.llm:
            prompt = (
                "Answer the table question in Chinese using only QUERY RESULT. Cite evidence as "
                "[T1], [T2]. If the result is insufficient, say so. Do not invent values.\n"
                f"Question: {question}\nSQL: {sql or '(cell resolution)'}\n"
                f"QUERY RESULT: {json.dumps(rows[:100], ensure_ascii=False, default=str)}\n"
                f"CITATIONS: {json.dumps(citations, ensure_ascii=False)}"
            )
            try:
                return invoke_deterministic(
                    self.llm, prompt, temperature=0.0, max_tokens=900
                ).strip()
            except Exception:
                pass
        return f"结构化表格查询返回 {len(rows)} 行；请根据 citations 中的表格、页码和 cell_id 审核结果。"


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[0-9a-zA-Z_.%+-]+|[\u4e00-\u9fff]", (value or "").lower()))


def _numeric(value: str) -> float | None:
    text = str(value or "").strip().replace(",", "")
    scientific = re.fullmatch(
        r"([-+]?\d+)\s*\.\s*(\d+)\s*[·×x]\s*10\s*\^?\s*([-+]?\d+)",
        text,
        flags=re.IGNORECASE,
    )
    if scientific:
        return float(f"{scientific.group(1)}.{scientific.group(2)}") * (10 ** int(scientific.group(3)))
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*(%)?", text)
    if not match:
        return None
    number = float(match.group(1))
    return number / 100.0 if match.group(2) else number


def _rank_cells(question: str, rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    terms = _terms(question)
    scored = []
    for row in rows:
        haystack = " ".join(str(row.get(key) or "") for key in ("source_title", "caption", "row_label", "column_label", "value"))
        score = len(terms & _terms(haystack)) / max(len(terms), 1)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("source_title")), int(item[1].get("row_index") or 0)))
    return [row for _, row in scored[:limit]]


def _citations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for row in rows:
        key = (row.get("table_id"), row.get("cell_id"))
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "label": f"T{len(result) + 1}", "source_title": row.get("source_title", ""),
            "table_id": row.get("table_id", ""), "caption": row.get("caption", ""),
            "page": row.get("page", 0), "cell_id": row.get("cell_id", ""),
            "row_index": row.get("row_index"), "column_index": row.get("column_index"),
        })
        if len(result) >= 20:
            break
    return result


def _citation_rows(
    question: str,
    result_rows: list[dict[str, Any]],
    cells: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    direct = [row for row in result_rows if row.get("cell_id")]
    if direct:
        return direct
    values = {
        str(value)
        for row in result_rows
        for value in row.values()
        if value is not None and not isinstance(value, (dict, list))
    }
    matched = [cell for cell in cells if str(cell.get("value")) in values]
    return _rank_cells(question, matched or cells, limit=20)


def _json_object(value: Any) -> dict[str, Any]:
    match = re.search(r"\{.*\}", str(value or ""), flags=re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
