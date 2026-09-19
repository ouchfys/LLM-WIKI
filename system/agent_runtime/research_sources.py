"""Source ledger for pasted research material and unresolved candidate claims."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CONTENT_KINDS = {"ai_conversation", "ai_answer", "article", "user_note", "pasted_text", "unknown"}
PROVIDERS = ("ChatGPT", "Claude", "Gemini", "DeepSeek", "Kimi", "豆包", "通义千问")
ROLE_PATTERN = re.compile(
    r"(?im)^\s*(?:#{1,4}\s*)?(user|human|assistant|ai|用户|提问|助手|回答)\s*[:：]\s*"
)


class ResearchSourceStore:
    """Preserve raw pasted sources; never promote their claims to Wiki knowledge."""

    def __init__(self, db_path: str | None = None):
        repo_root = Path(__file__).resolve().parents[2]
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _load(value: str | None, default: Any) -> Any:
        try:
            return json.loads(value) if value else default
        except (json.JSONDecodeError, TypeError):
            return default

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_sources (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'unknown',
                    detected_type TEXT NOT NULL DEFAULT 'unknown',
                    detection_confidence REAL NOT NULL DEFAULT 0,
                    segments_json TEXT NOT NULL DEFAULT '[]',
                    signals_json TEXT NOT NULL DEFAULT '[]',
                    title TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL,
                    raw_ref TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_research_sources_project
                    ON research_sources(project_id, updated_at);
                CREATE TABLE IF NOT EXISTS candidate_claims (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    aspect TEXT NOT NULL,
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    statement TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidate_claims_group
                    ON candidate_claims(project_id, subject, aspect, status);
                CREATE TABLE IF NOT EXISTS claim_relations (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    left_claim_id TEXT NOT NULL,
                    right_claim_id TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'uncertain',
                    decision_source TEXT NOT NULL DEFAULT 'deterministic_grouping',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(left_claim_id, right_claim_id)
                );
                CREATE TABLE IF NOT EXISTS dispute_groups (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    aspect TEXT NOT NULL,
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    claim_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'unresolved',
                    resolution TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dispute_groups_project
                    ON dispute_groups(project_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS research_state_snapshots (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    structured_json TEXT NOT NULL DEFAULT '{}',
                    markdown TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_state_project
                    ON research_state_snapshots(project_id, version);
                """
            )
            conn.commit()

    @staticmethod
    def detect_pasted_content(text: str) -> dict[str, Any]:
        raw = str(text or "")
        matches = list(ROLE_PATTERN.finditer(raw))
        segments: list[dict[str, str]] = []
        normalized_roles: list[str] = []
        for index, match in enumerate(matches):
            role_label = match.group(1).lower()
            role = "user" if role_label in {"user", "human", "用户", "提问"} else "assistant"
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            content = raw[start:end].strip()
            if content:
                segments.append({"role": role, "content": content})
                normalized_roles.append(role)
        alternations = sum(
            1 for left, right in zip(normalized_roles, normalized_roles[1:]) if left != right
        )
        signals: list[str] = []
        if len(matches) >= 3:
            signals.append("repeated_role_labels")
        if alternations >= 2:
            signals.append("multi_turn_structure")
        provider = "unknown"
        for candidate in PROVIDERS:
            if re.search(rf"(?i)(?<![\w-]){re.escape(candidate)}(?![\w-])", raw):
                provider = candidate
                signals.append("explicit_provider_name")
                break
        if len(raw) >= 120 and len(segments) >= 3 and alternations >= 2:
            kind, confidence = "ai_conversation", 0.98
        elif len(raw) >= 120 and len(segments) >= 2 and alternations >= 1:
            kind, confidence = "ai_conversation", 0.86
        else:
            kind, confidence, segments = "unknown", 0.0, []
        return {
            "content_kind": kind, "provider": provider, "confidence": confidence,
            "segments": segments, "signals": signals,
        }

    def capture(
        self, *, project_id: str, raw_text: str, source_type: str = "auto",
        origin: str = "unknown", title: str = "", claims: Iterable[dict[str, Any]] = (),
        detection: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_text = str(raw_text or "")
        if not raw_text.strip():
            raise ValueError("raw_text is required")
        detection = dict(detection or self.detect_pasted_content(raw_text))
        detected_type = str(detection.get("content_kind") or "unknown")
        if detected_type not in CONTENT_KINDS:
            detected_type = "unknown"
        source_type = detected_type if source_type in {"", "auto"} else str(source_type)
        if source_type not in CONTENT_KINDS:
            source_type = "unknown"
        explicit_provider = next(
            (candidate for candidate in PROVIDERS if re.search(
                rf"(?i)(?<![\w-]){re.escape(candidate)}(?![\w-])", raw_text
            )), "unknown",
        )
        origin = explicit_provider if explicit_provider != "unknown" else "unknown"
        content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        now = self._now()
        source_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM research_sources WHERE project_id=? AND content_hash=?",
                (str(project_id), content_hash),
            ).fetchone()
            if existing:
                source_id = str(existing["id"])
                conn.execute(
                    """UPDATE research_sources SET source_type=?, origin=?, detected_type=?,
                       detection_confidence=?, segments_json=?, signals_json=?, title=?,
                       metadata_json=?, updated_at=? WHERE id=?""",
                    (source_type, origin, detected_type, float(detection.get("confidence") or 0),
                     self._dump(detection.get("segments") or []), self._dump(detection.get("signals") or []),
                     str(title or ""), self._dump(metadata or {}), now, source_id),
                )
            else:
                conn.execute(
                    """INSERT INTO research_sources
                       (id,project_id,source_type,origin,detected_type,detection_confidence,
                        segments_json,signals_json,title,raw_text,content_hash,metadata_json,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source_id, str(project_id), source_type, origin, detected_type,
                     float(detection.get("confidence") or 0), self._dump(detection.get("segments") or []),
                     self._dump(detection.get("signals") or []), str(title or ""), raw_text,
                     content_hash, self._dump(metadata or {}), now, now),
                )
            conn.commit()
        created_claims = self.add_candidate_claims(str(project_id), source_id, claims)
        snapshot = self.refresh_snapshot(str(project_id))
        return {
            "source": self.get_source(source_id), "claims": created_claims,
            "snapshot": snapshot,
        }

    def auto_capture_if_ai_conversation(
        self, *, project_id: str, raw_text: str, metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        detection = self.detect_pasted_content(raw_text)
        if detection["content_kind"] != "ai_conversation" or detection["confidence"] < 0.9:
            return None
        return self.capture(
            project_id=project_id, raw_text=raw_text, source_type="ai_conversation",
            detection=detection, metadata=metadata,
        )

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM research_sources WHERE id=?", (str(source_id),)).fetchone()
        if not row:
            return None
        item = dict(row)
        for key, default in (("segments", []), ("signals", []), ("metadata", {})):
            item[key] = self._load(item.pop(f"{key}_json"), default)
        return item

    def list_sources(self, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM research_sources WHERE project_id=?
                   ORDER BY updated_at DESC LIMIT ?""",
                (str(project_id), max(1, min(int(limit), 200))),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key, default in (("segments", []), ("signals", []), ("metadata", {})):
                item[key] = self._load(item.pop(f"{key}_json"), default)
            item["raw_text_preview"] = str(item.pop("raw_text", ""))[:500]
            items.append(item)
        return items

    def correct_source(self, source_id: str, *, source_type: str, origin: str = "unknown") -> dict[str, Any]:
        source_type = str(source_type or "unknown")
        if source_type not in CONTENT_KINDS:
            raise ValueError("unsupported source_type")
        origin = str(origin or "unknown").strip() or "unknown"
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE research_sources SET source_type=?, origin=?, updated_at=? WHERE id=?",
                (source_type, origin, self._now(), str(source_id)),
            )
            conn.commit()
        if not cursor.rowcount:
            raise KeyError(source_id)
        return self.get_source(source_id) or {}

    def latest_snapshot(self, project_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM research_state_snapshots WHERE project_id=?
                   ORDER BY version DESC LIMIT 1""", (str(project_id),),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["structured"] = self._load(item.pop("structured_json"), {})
        return item

    def add_candidate_claims(
        self, project_id: str, source_id: str, claims: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for raw in claims or []:
            if not isinstance(raw, dict):
                continue
            statement = str(raw.get("statement") or "").strip()
            subject = str(raw.get("subject") or "").strip()
            aspect = str(raw.get("aspect") or "").strip()
            if not statement or not subject or not aspect:
                continue
            scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
            evidence_ids = [str(value) for value in raw.get("evidence_ids") or [] if str(value).strip()]
            claim_id = str(uuid.uuid4())
            now = self._now()
            with closing(self._connect()) as conn:
                duplicate = conn.execute(
                    """SELECT * FROM candidate_claims WHERE project_id=? AND source_id=?
                       AND lower(statement)=lower(?) LIMIT 1""",
                    (project_id, source_id, statement),
                ).fetchone()
                if duplicate:
                    created.append(self._claim_row(duplicate))
                    continue
                peers = conn.execute(
                    """SELECT * FROM candidate_claims WHERE project_id=?
                       AND lower(subject)=lower(?) AND lower(aspect)=lower(?)
                       AND scope_json=? AND status IN ('candidate','supported','disputed','accepted')""",
                    (project_id, subject, aspect, self._dump(scope)),
                ).fetchall()
                conn.execute(
                    """INSERT INTO candidate_claims
                       (id,project_id,source_id,subject,aspect,scope_json,statement,
                        evidence_ids_json,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?, 'candidate',?,?)""",
                    (claim_id, project_id, source_id, subject, aspect, self._dump(scope),
                     statement, self._dump(evidence_ids), now, now),
                )
                for peer in peers:
                    if str(peer["statement"]).strip().casefold() == statement.casefold():
                        continue
                    left_id, right_id = sorted((str(peer["id"]), claim_id))
                    conn.execute(
                        """INSERT OR IGNORE INTO claim_relations
                           (id,project_id,left_claim_id,right_claim_id,relation,decision_source,reason,created_at)
                           VALUES (?,?,?,?, 'uncertain','deterministic_grouping',
                                   'same subject, aspect and scope; semantic review required',?)""",
                        (str(uuid.uuid4()), project_id, left_id, right_id, now),
                    )
                    self._upsert_dispute(conn, project_id, subject, aspect, scope, [str(peer["id"]), claim_id], now)
                    conn.execute(
                        "UPDATE candidate_claims SET status='disputed', updated_at=? WHERE id IN (?,?)",
                        (now, str(peer["id"]), claim_id),
                    )
                conn.commit()
            claim = self.get_claim(claim_id)
            if claim:
                created.append(claim)
        return created

    def _upsert_dispute(
        self, conn: sqlite3.Connection, project_id: str, subject: str, aspect: str,
        scope: dict[str, Any], claim_ids: list[str], now: str,
    ) -> None:
        row = conn.execute(
            """SELECT * FROM dispute_groups WHERE project_id=? AND lower(subject)=lower(?)
               AND lower(aspect)=lower(?) AND scope_json=? AND status='unresolved' LIMIT 1""",
            (project_id, subject, aspect, self._dump(scope)),
        ).fetchone()
        if row:
            current = self._load(row["claim_ids_json"], [])
            merged = list(dict.fromkeys([*current, *claim_ids]))
            conn.execute(
                "UPDATE dispute_groups SET claim_ids_json=?, updated_at=? WHERE id=?",
                (self._dump(merged), now, row["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO dispute_groups
                   (id,project_id,subject,aspect,scope_json,claim_ids_json,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'unresolved',?,?)""",
                (str(uuid.uuid4()), project_id, subject, aspect, self._dump(scope),
                 self._dump(list(dict.fromkeys(claim_ids))), now, now),
            )

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM candidate_claims WHERE id=?", (str(claim_id),)).fetchone()
        return self._claim_row(row) if row else None

    def refresh_snapshot(self, project_id: str) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            claim_rows = conn.execute(
                "SELECT * FROM candidate_claims WHERE project_id=? ORDER BY created_at", (project_id,)
            ).fetchall()
            dispute_rows = conn.execute(
                "SELECT * FROM dispute_groups WHERE project_id=? AND status='unresolved' ORDER BY created_at",
                (project_id,),
            ).fetchall()
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM research_state_snapshots WHERE project_id=?",
                (project_id,),
            ).fetchone()[0])
            claims = [self._claim_row(row) for row in claim_rows]
            disputes = [{
                **dict(row), "scope": self._load(row["scope_json"], {}),
                "claim_ids": self._load(row["claim_ids_json"], []),
            } for row in dispute_rows]
            structured = {
                "goal": "", "accepted_facts": [item for item in claims if item["status"] == "accepted"],
                "disputed_claims": disputes, "decisions": [], "open_questions": [],
                "next_actions": ["verify candidate claims against primary sources"] if claims else [],
            }
            markdown = self._snapshot_markdown(structured, claims)
            snapshot_id = str(uuid.uuid4())
            now = self._now()
            conn.execute(
                """INSERT INTO research_state_snapshots
                   (id,project_id,structured_json,markdown,version,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (snapshot_id, project_id, self._dump(structured), markdown, version, now),
            )
            conn.commit()
        return {"id": snapshot_id, "project_id": project_id, "version": version, "markdown": markdown, "structured": structured}

    @staticmethod
    def _snapshot_markdown(structured: dict[str, Any], claims: list[dict[str, Any]]) -> str:
        disputed_ids = {
            claim_id for group in structured["disputed_claims"] for claim_id in group["claim_ids"]
        }
        lines = ["# Goal", "", structured.get("goal") or "尚未设置", "", "# Accepted Facts", ""]
        accepted = [item for item in claims if item["status"] == "accepted"]
        lines.extend([f"- {item['statement']}" for item in accepted] or ["- 暂无"])
        lines.extend(["", "# Disputed Claims", ""])
        lines.extend([f"- [{item['id']}] {item['statement']}" for item in claims if item["id"] in disputed_ids] or ["- 暂无"])
        lines.extend(["", "# Decisions", "", "- 暂无", "", "# Open Questions", ""])
        candidate = [item for item in claims if item["status"] == "candidate"]
        lines.extend([f"- 核验：{item['statement']}" for item in candidate] or ["- 暂无"])
        lines.extend(["", "# Next Actions", ""])
        actions = structured.get("next_actions") or []
        lines.extend([f"- {item}" for item in actions] or ["- 暂无"])
        return "\n".join(lines).strip() + "\n"

    def _claim_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["scope"] = self._load(item.pop("scope_json"), {})
        item["evidence_ids"] = self._load(item.pop("evidence_ids_json"), [])
        return item
