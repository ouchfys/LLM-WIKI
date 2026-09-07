from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.storage.layout import get_storage_layout
from system.storage.object_storage import get_object_storage
from system.wiki.paper_pipeline.models import DistilledCandidate, ReviewReport, SourcePacket


class PaperWikiPipelineStore:
    def __init__(self, db_path: str = None):
        repo_root = Path(__file__).resolve().parents[3]
        path = Path(db_path) if db_path else repo_root / "sessions.db"
        self.db_path = str(path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def dump_json(data: Any) -> str:
        return json.dumps(data if data is not None else {}, ensure_ascii=False)

    @staticmethod
    def load_json(data: str) -> Any:
        if not data:
            return {}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {}

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            wiki_columns = {row[1] for row in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()}
            if wiki_columns and "current_revision_id" not in wiki_columns:
                conn.execute("ALTER TABLE wiki_pages ADD COLUMN current_revision_id TEXT DEFAULT ''")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_packets (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_urls_json TEXT DEFAULT '[]',
                    raw_source_path TEXT DEFAULT '',
                    pdf_storage_uri TEXT DEFAULT '',
                    parser_used TEXT DEFAULT '',
                    packet_json TEXT NOT NULL,
                    source_hash TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source_packets_hash ON source_packets(source_hash)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_documents (
                    id TEXT PRIMARY KEY,
                    source_packet_id TEXT NOT NULL UNIQUE,
                    content_hash TEXT DEFAULT '',
                    parser TEXT DEFAULT '',
                    parser_version TEXT DEFAULT '',
                    process_config_json TEXT DEFAULT '{}',
                    original_uri TEXT DEFAULT '',
                    docling_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            source_document_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(source_documents)").fetchall()
            }
            for name, declaration in (
                ("docling_json_uri", "TEXT DEFAULT ''"),
                ("docling_json_hash", "TEXT DEFAULT ''"),
                ("docling_json_size", "INTEGER DEFAULT 0"),
            ):
                if source_document_columns and name not in source_document_columns:
                    conn.execute(f"ALTER TABLE source_documents ADD COLUMN {name} {declaration}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS document_elements (
                    id TEXT PRIMARY KEY,
                    source_packet_id TEXT NOT NULL,
                    element_type TEXT NOT NULL,
                    page INTEGER DEFAULT 0,
                    bbox_json TEXT DEFAULT '{}',
                    heading_path_json TEXT DEFAULT '[]',
                    parent_id TEXT DEFAULT '',
                    reading_order INTEGER DEFAULT 0,
                    text TEXT DEFAULT '',
                    caption TEXT DEFAULT '',
                    docling_ref TEXT DEFAULT '',
                    metadata_json TEXT DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_document_elements_packet ON document_elements(source_packet_id, reading_order)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_document_elements_page ON document_elements(source_packet_id, page)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS document_tables (
                    id TEXT PRIMARY KEY,
                    source_packet_id TEXT NOT NULL,
                    element_id TEXT NOT NULL,
                    caption TEXT DEFAULT '',
                    section_path_json TEXT DEFAULT '[]',
                    page INTEGER DEFAULT 0,
                    bbox_json TEXT DEFAULT '{}',
                    headers_json TEXT DEFAULT '[]',
                    rows_json TEXT DEFAULT '[]',
                    markdown TEXT DEFAULT '',
                    docling_ref TEXT DEFAULT '',
                    metadata_json TEXT DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_document_tables_packet ON document_tables(source_packet_id, page)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS document_table_cells (
                    id TEXT PRIMARY KEY,
                    table_id TEXT NOT NULL,
                    source_packet_id TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    column_index INTEGER NOT NULL,
                    row_span INTEGER DEFAULT 1,
                    column_span INTEGER DEFAULT 1,
                    text TEXT DEFAULT '',
                    row_header INTEGER DEFAULT 0,
                    column_header INTEGER DEFAULT 0,
                    page INTEGER DEFAULT 0,
                    bbox_json TEXT DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_table_cells_lookup ON document_table_cells(table_id, row_index, column_index)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS distilled_candidates (
                    id TEXT PRIMARY KEY,
                    source_packet_id TEXT NOT NULL,
                    candidate_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    status TEXT DEFAULT 'pending_review',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_distilled_candidates_source ON distilled_candidates(source_packet_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_reports (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_reports_candidate ON review_reports(candidate_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_card_sources (
                    id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL,
                    source_card_id TEXT DEFAULT '',
                    source_packet_id TEXT NOT NULL,
                    raw_source_path TEXT DEFAULT '',
                    source_url TEXT DEFAULT '',
                    section_id TEXT DEFAULT '',
                    evidence_text TEXT DEFAULT '',
                    claim_text TEXT DEFAULT '',
                    confidence REAL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_card_sources_card ON wiki_card_sources(card_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_card_sources_packet ON wiki_card_sources(source_packet_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_card_links (
                    id TEXT PRIMARY KEY,
                    from_card_id TEXT NOT NULL,
                    to_card_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    source_packet_id TEXT DEFAULT '',
                    evidence_text TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_card_links_from ON wiki_card_links(from_card_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_card_links_to ON wiki_card_links(to_card_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_aliases (
                    card_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_aliases_normalized ON wiki_aliases(normalized_alias)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_merge_audit (
                    id TEXT PRIMARY KEY,
                    source_packet_id TEXT NOT NULL,
                    paper_card_id TEXT DEFAULT '',
                    candidate_id TEXT DEFAULT '',
                    candidate_title TEXT DEFAULT '',
                    candidate_type TEXT DEFAULT '',
                    action TEXT NOT NULL,
                    target_card_id TEXT DEFAULT '',
                    result_card_id TEXT DEFAULT '',
                    status TEXT DEFAULT '',
                    plan_json TEXT DEFAULT '{}',
                    report_json TEXT DEFAULT '{}',
                    evidence_text TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_merge_audit_packet ON wiki_merge_audit(source_packet_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_merge_audit_result ON wiki_merge_audit(result_card_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_revisions (
                    id TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL,
                    parent_revision_id TEXT DEFAULT '',
                    patch TEXT DEFAULT '',
                    before_markdown TEXT DEFAULT '',
                    full_markdown TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    source_ids_json TEXT DEFAULT '[]',
                    review_status TEXT DEFAULT 'proposed',
                    verification_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    committed_at TEXT DEFAULT '',
                    rolled_back_at TEXT DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_revisions_page ON wiki_revisions(page_id, created_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_claims (
                    id TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    status TEXT DEFAULT 'supported',
                    relation TEXT DEFAULT 'supports',
                    supersedes_claim_id TEXT DEFAULT '',
                    confidence REAL DEFAULT 1.0,
                    source_packet_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_claims_page ON wiki_claims(page_id, status)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS claim_evidence (
                    claim_id TEXT NOT NULL,
                    element_id TEXT NOT NULL,
                    relation TEXT DEFAULT 'supports',
                    verifier_result TEXT DEFAULT 'unknown',
                    verifier_reason TEXT DEFAULT '',
                    verified_at TEXT DEFAULT '',
                    PRIMARY KEY (claim_id, element_id)
                )
                """
            )
            conn.commit()

    def upsert_source_packet(self, packet: SourcePacket) -> str:
        now = self.now_iso()
        with closing(self._connect()) as conn:
            existing = None
            if packet.source_hash:
                existing = conn.execute(
                    "SELECT id FROM source_packets WHERE source_hash = ? LIMIT 1",
                    (packet.source_hash,),
                ).fetchone()
            if existing:
                source_id = existing["id"]
                packet.source_id = source_id
                conn.execute(
                    """
                    UPDATE source_packets
                    SET source_type = ?, title = ?, source_urls_json = ?, raw_source_path = ?,
                        pdf_storage_uri = ?, parser_used = ?, packet_json = ?, source_hash = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        packet.source_type,
                        packet.title,
                        self.dump_json(packet.source_urls),
                        packet.raw_source_path,
                        packet.pdf_storage_uri,
                        packet.parser_used,
                        packet_json(packet),
                        packet.source_hash,
                        now,
                        source_id,
                    ),
                )
            else:
                source_id = packet.source_id or str(uuid.uuid4())
                packet.source_id = source_id
                conn.execute(
                    """
                    INSERT INTO source_packets
                    (id, source_type, title, source_urls_json, raw_source_path, pdf_storage_uri,
                     parser_used, packet_json, source_hash, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        packet.source_type,
                        packet.title,
                        self.dump_json(packet.source_urls),
                        packet.raw_source_path,
                        packet.pdf_storage_uri,
                        packet.parser_used,
                        packet_json(packet),
                        packet.source_hash,
                        now,
                        now,
                    ),
                )
            conn.commit()
        packet.source_id = source_id
        self.replace_source_evidence(packet)
        return source_id

    def replace_source_evidence(self, packet: SourcePacket) -> None:
        """Persist an optional parser artifact and its queryable projection."""
        raw_document = self.dump_json(packet.docling_json)
        raw_bytes = raw_document.encode("utf-8")
        document_hash = hashlib.sha256(raw_bytes).hexdigest() if packet.docling_json else ""
        document_uri = ""
        if packet.docling_json:
            key = get_storage_layout().source_document_key(packet.source_id, packet.source_hash or document_hash)
            document_uri = get_object_storage().write_text(
                key,
                raw_document,
                content_type="application/json; charset=utf-8",
            )
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM document_table_cells WHERE source_packet_id = ?", (packet.source_id,))
            conn.execute("DELETE FROM document_tables WHERE source_packet_id = ?", (packet.source_id,))
            conn.execute("DELETE FROM document_elements WHERE source_packet_id = ?", (packet.source_id,))
            conn.execute("DELETE FROM source_documents WHERE source_packet_id = ?", (packet.source_id,))
            conn.execute(
                """
                INSERT INTO source_documents
                (id, source_packet_id, content_hash, parser, parser_version, process_config_json,
                 original_uri, docling_json, docling_json_uri, docling_json_hash,
                 docling_json_size, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), packet.source_id, packet.source_hash, packet.parser_used,
                    str(packet.metadata.get("model_version") or packet.metadata.get("arxiv_id") or ""),
                    self.dump_json({
                        "parser_attempts": packet.metadata.get("parser_attempts") or [],
                        "quality_gate": packet.metadata.get("quality_gate") or {},
                        "degraded": bool(packet.metadata.get("degraded")),
                    }),
                    packet.pdf_storage_uri or packet.raw_source_path,
                    "{}", document_uri, document_hash, len(raw_bytes), self.now_iso(),
                ),
            )
            for element in packet.elements:
                conn.execute(
                    """INSERT INTO document_elements
                    (id, source_packet_id, element_type, page, bbox_json, heading_path_json,
                     parent_id, reading_order, text, caption, docling_ref, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (element.element_id, packet.source_id, element.element_type, element.page,
                     self.dump_json(element.bbox), self.dump_json(element.heading_path), element.parent_id,
                     element.reading_order, element.text, element.caption, element.docling_ref,
                     self.dump_json(element.metadata)),
                )
            for table in packet.tables:
                conn.execute(
                    """INSERT INTO document_tables
                    (id, source_packet_id, element_id, caption, section_path_json, page, bbox_json,
                     headers_json, rows_json, markdown, docling_ref, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (table.table_id, packet.source_id, table.element_id, table.caption,
                     self.dump_json(table.section_path), table.page, self.dump_json(table.bbox),
                     self.dump_json(table.headers), self.dump_json(table.rows), table.markdown,
                     table.docling_ref, self.dump_json(table.metadata)),
                )
                for cell in table.cells:
                    conn.execute(
                        """INSERT INTO document_table_cells
                        (id, table_id, source_packet_id, row_index, column_index, row_span, column_span,
                         text, row_header, column_header, page, bbox_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (cell.cell_id, table.table_id, packet.source_id, cell.row_index, cell.column_index,
                         cell.row_span, cell.column_span, cell.text, int(cell.row_header), int(cell.column_header),
                         cell.page, self.dump_json(cell.bbox)),
                    )
            conn.commit()

    def load_source_document_json(self, source_packet_id: str) -> dict[str, Any]:
        """Load the optional parser artifact, with legacy DB compatibility."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT docling_json, docling_json_uri, docling_json_hash
                   FROM source_documents WHERE source_packet_id = ? LIMIT 1""",
                (source_packet_id,),
            ).fetchone()
        if not row:
            return {}
        inline = self.load_json(row["docling_json"])
        if isinstance(inline, dict) and inline:
            return inline
        uri = str(row["docling_json_uri"] or "")
        if not uri:
            return {}
        raw = get_object_storage().read_text(uri)
        expected_hash = str(row["docling_json_hash"] or "")
        if expected_hash and hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected_hash:
            raise ValueError(f"Parser artifact checksum mismatch for source {source_packet_id}.")
        payload = self.load_json(raw)
        return payload if isinstance(payload, dict) else {}

    def get_evidence(self, evidence_ids: list[str]) -> list[dict[str, Any]]:
        if not evidence_ids:
            return []
        placeholders = ",".join("?" for _ in evidence_ids)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT *, 'element' AS evidence_kind FROM document_elements WHERE id IN ({placeholders})",
                evidence_ids,
            ).fetchall()
            cells = conn.execute(
                f"SELECT *, 'table_cell' AS evidence_kind FROM document_table_cells WHERE id IN ({placeholders})",
                evidence_ids,
            ).fetchall()
            tables = conn.execute(
                f"SELECT *, 'table' AS evidence_kind FROM document_tables WHERE id IN ({placeholders})",
                evidence_ids,
            ).fetchall()
        result = []
        for row in [*rows, *cells, *tables]:
            item = dict(row)
            for key in ("bbox_json", "heading_path_json", "metadata_json"):
                if key in item:
                    item[key[:-5]] = self.load_json(item.pop(key))
            if item.get("evidence_kind") == "table":
                caption = str(item.get("caption") or "")
                markdown = str(item.get("markdown") or "")
                item["text"] = "\n\n".join(part for part in (caption, markdown) if part)
            result.append(item)
        return result

    def find_evidence(
        self,
        source_packet_id: str,
        *,
        section_id: str = "",
        text: str = "",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM document_elements
                   WHERE source_packet_id = ? ORDER BY reading_order LIMIT 500""",
                (source_packet_id,),
            ).fetchall()
            tables = conn.execute(
                """SELECT * FROM document_tables
                   WHERE source_packet_id = ? ORDER BY page, id""",
                (source_packet_id,),
            ).fetchall()
        query_terms = set(re.findall(r"[0-9a-zA-Z\u4e00-\u9fff]+", (text or "").lower()))
        ranked = []
        for row in rows:
            item = dict(row)
            heading = " ".join(self.load_json(item.get("heading_path_json", "[]")))
            haystack = f"{heading} {item.get('text', '')} {item.get('caption', '')}".lower()
            terms = set(re.findall(r"[0-9a-zA-Z\u4e00-\u9fff]+", haystack))
            score = len(query_terms & terms) / max(len(query_terms), 1) if query_terms else 0.0
            if section_id and normalize_alias(section_id).replace(" ", "") in normalize_alias(heading).replace(" ", ""):
                score += 1.0
            if score > 0 or (not query_terms and not section_id):
                item["score"] = score
                ranked.append(item)
        for row in tables:
            item = dict(row)
            section_path = " ".join(self.load_json(item.get("section_path_json", "[]")))
            haystack = " ".join(
                str(value or "") for value in (
                    section_path, item.get("caption"), item.get("headers_json"),
                    item.get("rows_json"), item.get("markdown"),
                )
            ).lower()
            terms = set(re.findall(r"[0-9a-zA-Z\u4e00-\u9fff]+", haystack))
            score = len(query_terms & terms) / max(len(query_terms), 1) if query_terms else 0.0
            if "table" in (text or "").lower() and "table" in haystack:
                score += 0.75
            if section_id and normalize_alias(section_id).replace(" ", "") in normalize_alias(section_path).replace(" ", ""):
                score += 1.0
            if score > 0:
                item["score"] = score
                item["reading_order"] = int(item.get("page") or 0) * 10000
                ranked.append(item)
        ranked.sort(key=lambda item: (-item["score"], item["reading_order"]))
        return ranked[: max(1, min(limit, 50))]

    def list_source_tables(self, source_packet_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM document_tables WHERE source_packet_id = ? ORDER BY page, id",
                (source_packet_id,),
            ).fetchall()
            result = []
            for row in rows:
                table = dict(row)
                for key in ("section_path_json", "bbox_json", "headers_json", "rows_json", "metadata_json"):
                    table[key[:-5]] = self.load_json(table.pop(key, ""))
                cells = conn.execute(
                    """SELECT * FROM document_table_cells
                       WHERE table_id = ? ORDER BY row_index, column_index""",
                    (table["id"],),
                ).fetchall()
                table["cells"] = []
                for cell_row in cells:
                    cell = dict(cell_row)
                    cell["bbox"] = self.load_json(cell.pop("bbox_json", "{}"))
                    table["cells"].append(cell)
                result.append(table)
        return result

    def insert_candidate(self, candidate: DistilledCandidate) -> str:
        candidate_id = candidate.id or str(uuid.uuid4())
        candidate.id = candidate_id
        now = self.now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO distilled_candidates
                (id, source_packet_id, candidate_type, title, candidate_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending_review', ?)
                """,
                (
                    candidate_id,
                    candidate.source_packet_id,
                    candidate.candidate_type,
                    candidate.title,
                    model_json(candidate),
                    now,
                ),
            )
            conn.commit()
        return candidate_id

    def get_source_packet(self, source_packet_id: str) -> SourcePacket | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT sp.packet_json, sd.id AS source_document_id, sd.parser AS source_document_parser,
                          sd.docling_json_uri
                   FROM source_packets sp
                   LEFT JOIN source_documents sd ON sd.source_packet_id = sp.id
                   WHERE sp.id = ? LIMIT 1""",
                (source_packet_id,),
            ).fetchone()
        if not row:
            return None
        data = self.load_json(row["packet_json"])
        if not isinstance(data, dict):
            return None
        # The relational primary key is authoritative. Older upserts could
        # retain a freshly generated ID inside packet_json even though the row
        # was updated by source_hash.
        data["source_id"] = source_packet_id
        if not data.get("docling_json") and row["source_document_id"] and row["docling_json_uri"]:
            # Large legacy payloads remain external to operational SQLite.
            data["docling_json"] = {
                "persisted_source_document_id": str(row["source_document_id"]),
                "storage_uri": str(row["docling_json_uri"] or ""),
            }
        try:
            return SourcePacket(**data)
        except Exception:
            return None

    def find_source_packet_by_hash(self, source_hash: str) -> SourcePacket | None:
        if not source_hash:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id FROM source_packets WHERE source_hash = ? LIMIT 1",
                (source_hash,),
            ).fetchone()
        return self.get_source_packet(str(row["id"])) if row else None

    def update_candidate_status(self, candidate_id: str, status: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE distilled_candidates SET status = ? WHERE id = ?", (status, candidate_id))
            conn.commit()

    def insert_review_report(self, report: ReviewReport) -> str:
        report_id = report.id or str(uuid.uuid4())
        report.id = report_id
        now = self.now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO review_reports
                (id, candidate_id, status, report_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (report_id, report.candidate_id, report.status, model_json(report), now),
            )
            conn.commit()
        return report_id

    def add_aliases(self, card_id: str, aliases: list[str]) -> None:
        with closing(self._connect()) as conn:
            for alias in aliases:
                normalized = normalize_alias(alias)
                if not normalized:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO wiki_aliases(card_id, alias, normalized_alias)
                    VALUES (?, ?, ?)
                    """,
                    (card_id, alias.strip(), normalized),
                )
            conn.commit()

    def find_card_by_alias(self, aliases: list[str]) -> dict[str, Any] | None:
        normalized_aliases = [normalize_alias(alias) for alias in aliases if normalize_alias(alias)]
        if not normalized_aliases:
            return None
        with closing(self._connect()) as conn:
            for normalized in normalized_aliases:
                row = conn.execute(
                    "SELECT card_id, alias, normalized_alias FROM wiki_aliases WHERE normalized_alias = ? LIMIT 1",
                    (normalized,),
                ).fetchone()
                if row:
                    return dict(row)
            rows = conn.execute("SELECT card_id, alias, normalized_alias FROM wiki_aliases").fetchall()
            for row in rows:
                if normalize_alias(row["alias"]) in normalized_aliases:
                    return dict(row)
        return None

    def add_card_source(
        self,
        card_id: str,
        source_packet_id: str,
        source_card_id: str = "",
        raw_source_path: str = "",
        source_url: str = "",
        section_id: str = "",
        evidence_text: str = "",
        claim_text: str = "",
        confidence: float = 1.0,
    ) -> str:
        now = self.now_iso()
        with closing(self._connect()) as conn:
            existing = conn.execute(
                """SELECT id FROM wiki_card_sources
                   WHERE card_id=? AND source_packet_id=? AND source_card_id=?
                     AND raw_source_path=? AND source_url=? AND section_id=? AND claim_text=?
                   LIMIT 1""",
                (
                    card_id, source_packet_id, source_card_id, raw_source_path,
                    source_url, section_id, claim_text,
                ),
            ).fetchone()
            if existing:
                return str(existing["id"])
            source_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO wiki_card_sources
                (id, card_id, source_card_id, source_packet_id, raw_source_path, source_url,
                 section_id, evidence_text, claim_text, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    card_id,
                    source_card_id,
                    source_packet_id,
                    raw_source_path,
                    source_url,
                    section_id,
                    evidence_text,
                    claim_text,
                    confidence,
                    now,
                ),
            )
            conn.commit()
        return source_id

    def add_card_link(
        self,
        from_card_id: str,
        to_card_id: str,
        relation_type: str,
        source_packet_id: str = "",
        evidence_text: str = "",
    ) -> str:
        with closing(self._connect()) as conn:
            existing = conn.execute(
                """
                SELECT id FROM wiki_card_links
                WHERE from_card_id = ? AND to_card_id = ? AND relation_type = ? AND source_packet_id = ?
                LIMIT 1
                """,
                (from_card_id, to_card_id, relation_type, source_packet_id),
            ).fetchone()
            if existing:
                return existing["id"]
            link_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO wiki_card_links
                (id, from_card_id, to_card_id, relation_type, source_packet_id, evidence_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (link_id, from_card_id, to_card_id, relation_type, source_packet_id, evidence_text, self.now_iso()),
            )
            conn.commit()
        return link_id

    def list_card_links(self, card_id: str) -> dict[str, list[dict[str, Any]]]:
        with closing(self._connect()) as conn:
            outgoing = conn.execute(
                """
                SELECT l.*, p.title AS target_title, p.page_type AS target_page_type
                FROM wiki_card_links l
                LEFT JOIN wiki_pages p ON p.id = l.to_card_id
                WHERE l.from_card_id = ?
                ORDER BY l.created_at DESC
                """,
                (card_id,),
            ).fetchall()
            incoming = conn.execute(
                """
                SELECT l.*, p.title AS source_title, p.page_type AS source_page_type
                FROM wiki_card_links l
                LEFT JOIN wiki_pages p ON p.id = l.from_card_id
                WHERE l.to_card_id = ?
                ORDER BY l.created_at DESC
                """,
                (card_id,),
            ).fetchall()
            sources = conn.execute(
                """
                SELECT s.*, p.title AS source_card_title
                FROM wiki_card_sources s
                LEFT JOIN wiki_pages p ON p.id = s.source_card_id
                WHERE s.card_id = ?
                ORDER BY s.created_at DESC
                """,
                (card_id,),
            ).fetchall()
        return {
            "outgoing": [dict(row) for row in outgoing],
            "incoming": [dict(row) for row in incoming],
            "sources": [dict(row) for row in sources],
        }

    def list_aliases(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT
                    a.card_id,
                    a.alias,
                    a.normalized_alias,
                    p.title,
                    p.page_type,
                    p.content_json,
                    p.related_topics_json
                FROM wiki_aliases a
                JOIN wiki_pages p ON p.id = a.card_id
                ORDER BY length(a.alias) DESC, lower(a.alias)
                """
            ).fetchall()
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["card_id"], normalize_alias(row["alias"]))
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "card_id": row["card_id"],
                "title": row["title"],
                "alias": row["alias"],
                "normalized_alias": row["normalized_alias"],
                "page_type": row["page_type"],
            })
            content = self.load_json(row["content_json"])
            for alias in _alias_values_from_content(content):
                normalized = normalize_alias(alias)
                key = (row["card_id"], normalized)
                if not normalized or key in seen:
                    continue
                seen.add(key)
                items.append({
                    "card_id": row["card_id"],
                    "title": row["title"],
                    "alias": alias,
                    "normalized_alias": normalized,
                    "page_type": row["page_type"],
                })
            related_topics = self.load_json(row["related_topics_json"])
            if not isinstance(related_topics, list):
                related_topics = []
            for topic in related_topics:
                normalized = normalize_alias(str(topic))
                key = (row["card_id"], normalized)
                if not normalized or key in seen:
                    continue
                seen.add(key)
                items.append({
                    "card_id": row["card_id"],
                    "title": row["title"],
                    "alias": str(topic),
                    "normalized_alias": normalized,
                    "page_type": row["page_type"],
                })
        return sorted(items, key=lambda item: len(item["alias"]), reverse=True)

    def add_merge_audit(
        self,
        *,
        source_packet_id: str,
        action: str,
        paper_card_id: str = "",
        candidate_id: str = "",
        candidate_title: str = "",
        candidate_type: str = "",
        target_card_id: str = "",
        result_card_id: str = "",
        status: str = "",
        plan: Any = None,
        report: Any = None,
        evidence_text: str = "",
    ) -> str:
        audit_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO wiki_merge_audit
                (id, source_packet_id, paper_card_id, candidate_id, candidate_title,
                 candidate_type, action, target_card_id, result_card_id, status,
                 plan_json, report_json, evidence_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    source_packet_id,
                    paper_card_id,
                    candidate_id,
                    candidate_title,
                    candidate_type,
                    action,
                    target_card_id,
                    result_card_id,
                    status,
                    self.dump_json(plan),
                    self.dump_json(report),
                    evidence_text,
                    self.now_iso(),
                ),
            )
            conn.commit()
        return audit_id

    def list_merge_audit(
        self,
        source_packet_id: str = "",
        card_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if source_packet_id:
            where.append("source_packet_id = ?")
            params.append(source_packet_id)
        if card_id:
            where.append("(paper_card_id = ? OR target_card_id = ? OR result_card_id = ?)")
            params.extend([card_id, card_id, card_id])
        clause = "WHERE " + " AND ".join(where) if where else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM wiki_merge_audit
                {clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params + [limit],
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["plan"] = self.load_json(item.pop("plan_json", "{}"))
            item["report"] = self.load_json(item.pop("report_json", "{}"))
            result.append(item)
        return result

    def create_revision(
        self,
        *,
        page_id: str,
        parent_revision_id: str,
        patch: str,
        before_markdown: str,
        full_markdown: str,
        reason: str,
        source_ids: list[str],
        verification: dict[str, Any],
        review_status: str = "proposed",
    ) -> str:
        revision_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO wiki_revisions
                (id, page_id, parent_revision_id, patch, before_markdown, full_markdown,
                 reason, source_ids_json, review_status, verification_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (revision_id, page_id, parent_revision_id, patch, before_markdown, full_markdown,
                 reason, self.dump_json(source_ids), review_status, self.dump_json(verification), self.now_iso()),
            )
            conn.commit()
        return revision_id

    def finalize_revision(self, revision_id: str, page_id: str, status: str = "committed") -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE wiki_revisions SET review_status = ?, committed_at = ? WHERE id = ?",
                (status, self.now_iso() if status == "committed" else "", revision_id),
            )
            if status == "committed":
                conn.execute("UPDATE wiki_pages SET current_revision_id = ? WHERE id = ?", (revision_id, page_id))
            elif status == "rejected":
                row = conn.execute(
                    "SELECT parent_revision_id FROM wiki_revisions WHERE id = ?",
                    (revision_id,),
                ).fetchone()
                parent_id = row["parent_revision_id"] if row else ""
                conn.execute(
                    """UPDATE wiki_pages SET current_revision_id = ?
                       WHERE id = ? AND current_revision_id = ?""",
                    (parent_id, page_id, revision_id),
                )
            conn.commit()

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM wiki_revisions WHERE id = ?", (revision_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["source_ids"] = self.load_json(item.pop("source_ids_json", "[]"))
        item["verification"] = self.load_json(item.pop("verification_json", "{}"))
        return item

    def update_revision_verification(self, revision_id: str, verification: dict[str, Any]) -> None:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE wiki_revisions SET verification_json=? WHERE id=?",
                (self.dump_json(verification), revision_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Wiki revision not found: {revision_id}")
            conn.commit()

    def append_revision_post_commit_effect(self, revision_id: str, effect: dict[str, Any]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT verification_json, review_status FROM wiki_revisions WHERE id=?",
                (revision_id,),
            ).fetchone()
            if not row or row["review_status"] != "proposed":
                raise ValueError("Post-commit effects can only be attached to a proposed revision.")
            verification = self.load_json(row["verification_json"])
            effects = [item for item in verification.get("post_commit_effects") or [] if isinstance(item, dict)]
            effects.append(effect)
            verification["post_commit_effects"] = effects
            conn.execute(
                "UPDATE wiki_revisions SET verification_json=? WHERE id=?",
                (self.dump_json(verification), revision_id),
            )
            conn.commit()

    def list_revisions(self, page_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM wiki_revisions WHERE page_id = ? ORDER BY created_at DESC LIMIT ?",
                (page_id, max(1, min(int(limit), 200))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source_ids"] = self.load_json(item.pop("source_ids_json", "[]"))
            item["verification"] = self.load_json(item.pop("verification_json", "{}"))
            result.append(item)
        return result

    def list_page_claims(self, page_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            claims = conn.execute(
                "SELECT * FROM wiki_claims WHERE page_id = ? ORDER BY created_at, id",
                (page_id,),
            ).fetchall()
            output = []
            for claim_row in claims:
                claim = dict(claim_row)
                evidence_rows = conn.execute(
                    """SELECT ce.*, de.source_packet_id, de.element_type, de.page,
                              de.bbox_json, de.heading_path_json, de.text, de.caption, de.docling_ref,
                              tc.table_id, tc.row_index, tc.column_index, tc.text AS cell_text,
                              tc.page AS cell_page, tc.bbox_json AS cell_bbox_json
                       FROM claim_evidence ce
                       LEFT JOIN document_elements de ON de.id = ce.element_id
                       LEFT JOIN document_table_cells tc ON tc.id = ce.element_id
                       WHERE ce.claim_id = ? ORDER BY ce.element_id""",
                    (claim["id"],),
                ).fetchall()
                claim["evidence"] = []
                for evidence_row in evidence_rows:
                    item = dict(evidence_row)
                    is_cell = bool(item.get("table_id"))
                    item["evidence_kind"] = "table_cell" if is_cell else "element"
                    item["page"] = item.get("cell_page") if is_cell else item.get("page")
                    item["text"] = item.get("cell_text") if is_cell else item.get("text")
                    item["bbox"] = self.load_json(item.get("cell_bbox_json") if is_cell else item.get("bbox_json"))
                    item["heading_path"] = self.load_json(item.get("heading_path_json") or "[]")
                    claim["evidence"].append(item)
                output.append(claim)
        return output

    def replace_revision_claims(
        self,
        *,
        page_id: str,
        revision_id: str,
        claims: list[dict[str, Any]],
        verification: list[dict[str, Any]],
    ) -> None:
        verification_by_id = {str(item.get("claim_id") or ""): item for item in verification}
        with closing(self._connect()) as conn:
            active_ids = [str(claim.get("id") or "") for claim in claims if str(claim.get("id") or "")]
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                conn.execute(
                    f"UPDATE wiki_claims SET status = 'removed' WHERE page_id = ? AND id NOT IN ({placeholders})",
                    [page_id, *active_ids],
                )
            else:
                conn.execute("UPDATE wiki_claims SET status = 'removed' WHERE page_id = ?", (page_id,))
            for claim in claims:
                claim_id = str(claim.get("id") or "")
                statement = str(claim.get("statement") or "").strip()
                if not claim_id or not statement:
                    continue
                conn.execute(
                    """INSERT INTO wiki_claims
                    (id, page_id, revision_id, statement, status, relation, supersedes_claim_id,
                     confidence, source_packet_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET revision_id=excluded.revision_id,
                    status=excluded.status, relation=excluded.relation, confidence=excluded.confidence""",
                    (claim_id, page_id, revision_id, statement, str(claim.get("status") or "supported"),
                     str(claim.get("relation") or "supports"), str(claim.get("supersedes_claim_id") or ""),
                     float(claim.get("confidence") or 0),
                     str((claim.get("source_packet_ids") or [""])[0]), self.now_iso()),
                )
                result = verification_by_id.get(claim_id, {})
                for evidence_id in claim.get("evidence_ids") or []:
                    conn.execute(
                        """INSERT INTO claim_evidence
                        (claim_id, element_id, relation, verifier_result, verifier_reason, verified_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(claim_id, element_id) DO UPDATE SET
                        relation=excluded.relation, verifier_result=excluded.verifier_result,
                        verifier_reason=excluded.verifier_reason, verified_at=excluded.verified_at""",
                        (claim_id, str(evidence_id), str(claim.get("relation") or "supports"),
                         str(result.get("result") or "unknown"), str(result.get("reason") or ""), self.now_iso()),
                    )
            conn.commit()

    def mark_revision_rolled_back(self, revision_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE wiki_revisions SET review_status = 'rolled_back', rolled_back_at = ? WHERE id = ?",
                (self.now_iso(), revision_id),
            )
            conn.commit()


def normalize_alias(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[\u2010-\u2015_+-]+", " ", value)
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def model_json(model: Any) -> str:
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json()
    return model.json(ensure_ascii=False)


def packet_json(packet: SourcePacket) -> str:
    """Serialize packet metadata without duplicating a large parser artifact.

    The lossless artifact lives in object storage; packet_json keeps the
    normalized elements/tables required by pipeline replay.
    """
    if hasattr(packet, "model_dump"):
        payload = packet.model_dump(exclude={"docling_json"})
    else:
        payload = packet.dict(exclude={"docling_json"})
    return json.dumps(payload, ensure_ascii=False)


def _alias_values_from_content(content: Any) -> list[str]:
    if not isinstance(content, dict):
        return []
    values: list[str] = []
    for key in ("aliases", "related_topics"):
        raw = content.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item).strip())
        elif isinstance(raw, str) and raw.strip():
            values.append(raw)
    return values
