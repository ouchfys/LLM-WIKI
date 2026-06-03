from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
                        model_json(packet),
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
                        model_json(packet),
                        packet.source_hash,
                        now,
                        now,
                    ),
                )
            conn.commit()
        return source_id

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
                "SELECT packet_json FROM source_packets WHERE id = ? LIMIT 1",
                (source_packet_id,),
            ).fetchone()
        if not row:
            return None
        data = self.load_json(row["packet_json"])
        if not isinstance(data, dict):
            return None
        try:
            return SourcePacket(**data)
        except Exception:
            return None

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
        source_id = str(uuid.uuid4())
        now = self.now_iso()
        with closing(self._connect()) as conn:
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


def normalize_alias(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[\u2010-\u2015_+-]+", " ", value)
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def model_json(model: Any) -> str:
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json()
    return model.json(ensure_ascii=False)


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
