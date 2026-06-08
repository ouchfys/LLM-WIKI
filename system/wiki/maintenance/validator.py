from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.storage import get_object_storage


CURRENT_PAGE_TYPES = {
    "ConceptPage",
    "PaperPage",
    "MethodPage",
    "ComparePage",
    "InterviewQA",
    "MistakeNote",
    "StudyPlan",
    "SourceNote",
}

FUTURE_PAGE_TYPES = {
    "paper",
    "concept",
    "method",
    "claim",
    "benchmark",
    "question",
    "interview_note",
    "source_note",
}

ALLOWED_PAGE_TYPES = CURRENT_PAGE_TYPES | FUTURE_PAGE_TYPES
ALLOWED_CONFIDENCE = {"verified", "source_reported", "source-reported", "inferred", "speculative", ""}
LEGACY_PATH_MARKERS = ("data/raw_sources", "data\\raw_sources", "data/generated/wiki", "data\\generated\\wiki")


class WikiValidator:
    """Deterministic health checks for the wiki database and storage paths."""

    def __init__(self, db_path: str | Path | None = None):
        repo_root = Path(__file__).resolve().parents[3]
        self.repo_root = repo_root
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self.storage = get_object_storage()

    def validate_all(self, *, check_storage: bool = False) -> dict[str, Any]:
        run_id = f"validation-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        with closing(self._connect()) as conn:
            pages = self._fetch_dicts(conn, "SELECT rowid, * FROM wiki_pages ORDER BY updated_at DESC")
            chunks = self._fetch_dicts(conn, "SELECT rowid, * FROM wiki_chunks ORDER BY card_id, chunk_index")
            aliases = self._fetch_dicts(conn, "SELECT rowid, * FROM wiki_aliases")
            links = self._fetch_dicts(conn, "SELECT rowid, * FROM wiki_card_links")
            sources = self._fetch_dicts(conn, "SELECT rowid, * FROM wiki_card_sources")
            packets = self._fetch_dicts(conn, "SELECT rowid, * FROM source_packets")

            page_ids = {row["id"] for row in pages}
            packet_ids = {row["id"] for row in packets}
            pages_by_id = {row["id"]: row for row in pages}

            self._validate_pages(pages, errors, warnings, check_storage)
            self._validate_chunks(chunks, page_ids, errors, warnings)
            self._validate_aliases(aliases, page_ids, errors, warnings)
            self._validate_links(links, page_ids, errors, warnings)
            self._validate_sources(sources, page_ids, packet_ids, errors, warnings, check_storage)
            self._validate_source_packets(packets, errors, warnings, check_storage)
            self._validate_fts_counts(conn, errors, warnings)
            self._validate_orphans(pages_by_id, links, sources, warnings)

        return {
            "ok": not any(item.get("severity") == "error" for item in errors),
            "run_id": run_id,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "check_storage": check_storage,
            "summary": {
                "error_count": len(errors),
                "warning_count": len(warnings),
            },
            "errors": errors,
            "warnings": warnings,
        }

    def validate_card(self, card_id: str, *, check_storage: bool = False) -> dict[str, Any]:
        report = self.validate_all(check_storage=check_storage)
        report["errors"] = [
            item for item in report["errors"]
            if item.get("entity_id") in {card_id, ""} or item.get("card_id") == card_id
        ]
        report["warnings"] = [
            item for item in report["warnings"]
            if item.get("entity_id") in {card_id, ""} or item.get("card_id") == card_id
        ]
        report["ok"] = not report["errors"]
        report["summary"] = {
            "error_count": len(report["errors"]),
            "warning_count": len(report["warnings"]),
        }
        return report

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _fetch_dicts(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
        try:
            return [dict(row) for row in conn.execute(sql).fetchall()]
        except sqlite3.OperationalError:
            return []

    def _validate_pages(
        self,
        pages: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        check_storage: bool,
    ) -> None:
        for page in pages:
            card_id = page.get("id", "")
            title = str(page.get("title") or "").strip()
            page_type = str(page.get("page_type") or "").strip()
            markdown_path = str(page.get("markdown_path") or "")
            summary = str(page.get("summary") or "").strip()
            content = self._json(page.get("content_json"), default={})
            source_urls = self._json(page.get("source_urls_json"), default=[])
            related = self._json(page.get("related_topics_json"), default=[])

            if not card_id:
                self._error(errors, "missing_required_field", "wiki_page", "", "wiki page id is empty", "deterministic_fixer")
            if not title:
                self._error(errors, "missing_required_field", "wiki_page", card_id, "title is empty", "repair_agent")
            if page_type not in ALLOWED_PAGE_TYPES:
                self._error(errors, "unsupported_page_type", "wiki_page", card_id, f"unsupported page_type: {page_type}", "repair_agent")
            if not isinstance(content, dict):
                self._error(errors, "schema_error", "wiki_page", card_id, "content_json must be an object", "repair_agent")
            if not summary:
                self._warning(warnings, "weak_summary", "wiki_page", card_id, "summary is empty", "repair_agent")
            if not isinstance(source_urls, list):
                self._error(errors, "schema_error", "wiki_page", card_id, "source_urls_json must be a list", "deterministic_fixer")
            if not isinstance(related, list):
                self._error(errors, "schema_error", "wiki_page", card_id, "related_topics_json must be a list", "deterministic_fixer")

            confidence = str(content.get("confidence") or content.get("review_confidence") or "")
            if confidence and confidence not in ALLOWED_CONFIDENCE:
                self._error(errors, "unsupported_confidence", "wiki_page", card_id, f"unsupported confidence: {confidence}", "repair_agent")

            self._validate_reference_path(
                value=markdown_path,
                expected_prefix="wiki/",
                entity_type="wiki_page",
                entity_id=card_id,
                field="markdown_path",
                errors=errors,
                warnings=warnings,
                check_storage=check_storage,
            )
            for field in ("raw_source_path", "pdf_storage_uri"):
                value = str(content.get(field) or "")
                if value:
                    self._validate_reference_path(
                        value=value,
                        expected_prefix="sources/" if field == "raw_source_path" else "sources/papers/",
                        entity_type="wiki_page",
                        entity_id=card_id,
                        field=field,
                        errors=errors,
                        warnings=warnings,
                        check_storage=check_storage,
                    )

    def _validate_chunks(
        self,
        chunks: list[dict[str, Any]],
        page_ids: set[str],
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        seen: set[tuple[str, int]] = set()
        for chunk in chunks:
            chunk_id = str(chunk.get("id") or "")
            card_id = str(chunk.get("card_id") or "")
            index = int(chunk.get("chunk_index") or 0)
            if card_id not in page_ids:
                self._error(errors, "missing_card", "wiki_chunk", chunk_id, f"chunk references missing card_id {card_id}", "deterministic_fixer")
            key = (card_id, index)
            if key in seen:
                self._warning(warnings, "duplicate_chunk_index", "wiki_chunk", chunk_id, f"duplicate chunk index {index} for card {card_id}", "deterministic_fixer")
            seen.add(key)
            if not str(chunk.get("text") or "").strip():
                self._error(errors, "empty_chunk", "wiki_chunk", chunk_id, "chunk text is empty", "deterministic_fixer")

    def _validate_aliases(
        self,
        aliases: list[dict[str, Any]],
        page_ids: set[str],
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        owner_by_alias: dict[str, str] = {}
        for alias in aliases:
            card_id = str(alias.get("card_id") or "")
            normalized = str(alias.get("normalized_alias") or "").strip()
            if card_id not in page_ids:
                self._error(errors, "missing_card", "wiki_alias", normalized, f"alias references missing card_id {card_id}", "deterministic_fixer")
            if not normalized:
                self._warning(warnings, "empty_alias", "wiki_alias", card_id, "normalized alias is empty", "deterministic_fixer")
                continue
            previous = owner_by_alias.get(normalized)
            if previous and previous != card_id:
                self._error(errors, "alias_conflict", "wiki_alias", normalized, f"alias maps to both {previous} and {card_id}", "merge_agent")
            owner_by_alias[normalized] = card_id

    def _validate_links(
        self,
        links: list[dict[str, Any]],
        page_ids: set[str],
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        seen: set[tuple[str, str, str]] = set()
        for link in links:
            link_id = str(link.get("id") or "")
            from_id = str(link.get("from_card_id") or "")
            to_id = str(link.get("to_card_id") or "")
            relation = str(link.get("relation_type") or "")
            if from_id not in page_ids:
                self._error(errors, "broken_related_link", "wiki_card_link", link_id, f"from_card_id does not exist: {from_id}", "merge_agent")
            if to_id not in page_ids:
                self._error(errors, "broken_related_link", "wiki_card_link", link_id, f"to_card_id does not exist: {to_id}", "merge_agent")
            if not relation:
                self._warning(warnings, "missing_relation_type", "wiki_card_link", link_id, "relation_type is empty", "merge_agent")
            key = (from_id, to_id, relation)
            if key in seen:
                self._warning(warnings, "duplicate_link", "wiki_card_link", link_id, "duplicate card link", "deterministic_fixer")
            seen.add(key)

    def _validate_sources(
        self,
        sources: list[dict[str, Any]],
        page_ids: set[str],
        packet_ids: set[str],
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        check_storage: bool,
    ) -> None:
        for source in sources:
            source_id = str(source.get("id") or "")
            card_id = str(source.get("card_id") or "")
            packet_id = str(source.get("source_packet_id") or "")
            if card_id not in page_ids:
                self._error(errors, "missing_card", "wiki_card_source", source_id, f"source references missing card_id {card_id}", "deterministic_fixer")
            if packet_id and packet_id not in packet_ids:
                self._error(errors, "missing_source", "wiki_card_source", source_id, f"source_packet_id does not exist: {packet_id}", "extract_agent")
            raw_path = str(source.get("raw_source_path") or "")
            if raw_path:
                self._validate_reference_path(
                    value=raw_path,
                    expected_prefix="sources/",
                    entity_type="wiki_card_source",
                    entity_id=source_id,
                    field="raw_source_path",
                    errors=errors,
                    warnings=warnings,
                    check_storage=check_storage,
                )

    def _validate_source_packets(
        self,
        packets: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        check_storage: bool,
    ) -> None:
        for packet in packets:
            packet_id = str(packet.get("id") or "")
            raw_path = str(packet.get("raw_source_path") or "")
            pdf_uri = str(packet.get("pdf_storage_uri") or "")
            packet_json = self._json(packet.get("packet_json"), default={})
            if not isinstance(packet_json, dict):
                self._error(errors, "schema_error", "source_packet", packet_id, "packet_json must be an object", "extract_agent")
            if raw_path:
                self._validate_reference_path(raw_path, "sources/", "source_packet", packet_id, "raw_source_path", errors, warnings, check_storage)
            if pdf_uri:
                self._validate_reference_path(pdf_uri, "sources/papers/", "source_packet", packet_id, "pdf_storage_uri", errors, warnings, check_storage)

    def _validate_fts_counts(
        self,
        conn: sqlite3.Connection,
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        for base_table, fts_table in (("wiki_pages", "wiki_pages_fts"), ("wiki_chunks", "wiki_chunks_fts")):
            try:
                base_count = conn.execute(f"SELECT COUNT(*) FROM {base_table}").fetchone()[0]
                fts_count = conn.execute(f"SELECT COUNT(*) FROM {fts_table}").fetchone()[0]
            except sqlite3.OperationalError:
                self._error(errors, "missing_fts_table", "database", fts_table, f"{fts_table} is missing", "deterministic_fixer")
                continue
            if base_count != fts_count:
                self._error(errors, "fts_count_mismatch", "database", fts_table, f"{fts_table} count {fts_count} != {base_table} count {base_count}", "deterministic_fixer")

    def _validate_orphans(
        self,
        pages_by_id: dict[str, dict[str, Any]],
        links: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        if len(pages_by_id) <= 1:
            return
        connected: set[str] = set()
        for link in links:
            connected.add(str(link.get("from_card_id") or ""))
            connected.add(str(link.get("to_card_id") or ""))
        sourced = {str(source.get("card_id") or "") for source in sources}
        for card_id, page in pages_by_id.items():
            if page.get("page_type") == "PaperPage":
                continue
            if card_id not in connected and card_id not in sourced:
                self._warning(warnings, "orphan_card", "wiki_page", card_id, "card has no links or source evidence", "merge_agent")

    def _validate_reference_path(
        self,
        value: str,
        expected_prefix: str,
        entity_type: str,
        entity_id: str,
        field: str,
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        check_storage: bool,
    ) -> None:
        normalized = value.replace("\\", "/")
        if any(marker in normalized for marker in LEGACY_PATH_MARKERS):
            self._error(errors, "legacy_path", entity_type, entity_id, f"{field} uses legacy path: {value}", "deterministic_fixer")
            return
        key = self.storage.key_from_uri(value)
        if expected_prefix and expected_prefix not in key and not normalized.startswith(expected_prefix):
            self._warning(warnings, "unexpected_storage_prefix", entity_type, entity_id, f"{field} is outside {expected_prefix}: {value}", "deterministic_fixer")
        if check_storage:
            try:
                if not self.storage.exists(value):
                    self._error(errors, "missing_storage_object", entity_type, entity_id, f"{field} does not resolve: {value}", "extract_agent")
            except Exception as exc:
                self._warning(warnings, "storage_check_failed", entity_type, entity_id, f"{field} storage check failed: {exc}", "extract_agent")

    @staticmethod
    def _json(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _error(
        items: list[dict[str, Any]],
        error_type: str,
        entity_type: str,
        entity_id: str,
        message: str,
        repair_target: str,
    ) -> None:
        items.append({
            "severity": "error",
            "error_type": error_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "message": message,
            "repair_target": repair_target,
        })

    @staticmethod
    def _warning(
        items: list[dict[str, Any]],
        error_type: str,
        entity_type: str,
        entity_id: str,
        message: str,
        repair_target: str,
    ) -> None:
        items.append({
            "severity": "warning",
            "error_type": error_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "message": message,
            "repair_target": repair_target,
        })
