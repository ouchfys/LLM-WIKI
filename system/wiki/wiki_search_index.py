"""Section-level lexical/vector index for compiled Wiki pages.

Markdown remains the knowledge product.  This table stores only small search
units and optional embeddings, so resolving a query never requires loading the
entire Wiki corpus into Python or into the language model.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from array import array
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from system.wiki.markdown_vault import SYSTEM_CONTENT_KEYS


SEARCH_EXCLUDED_KEYS = SYSTEM_CONTENT_KEYS | {
    "markdown_status", "review_status_text", "evidence", "links",
}


class WikiSearchIndex:
    def __init__(self, db_path: str | None = None, embedder: Any = None):
        base_dir = Path(__file__).resolve().parents[2]
        self.db_path = str(Path(db_path) if db_path else base_dir / "sessions.db")
        self.embedder = embedder
        self._fts_enabled = False
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS wiki_search_units (
                    id TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL,
                    page_type TEXT DEFAULT '',
                    unit_kind TEXT NOT NULL,
                    section TEXT DEFAULT '',
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    aliases_text TEXT DEFAULT '',
                    lexical_terms TEXT DEFAULT '',
                    content_hash TEXT NOT NULL,
                    embedding BLOB,
                    embedding_dim INTEGER DEFAULT 0,
                    embedding_model TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wiki_search_units_page ON wiki_search_units(page_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wiki_search_units_embedding ON wiki_search_units(embedding_model, embedding_dim)"
            )
            try:
                conn.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS wiki_search_units_fts
                       USING fts5(unit_id UNINDEXED, title, section, text, aliases, terms,
                                  tokenize='unicode61')"""
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False
            conn.commit()

    def replace_page(self, card: dict[str, Any]) -> int:
        """Replace lexical units and keep unchanged vectors by content hash."""
        units = build_search_units(card)
        page_id = str(card.get("id") or "")
        if not page_id:
            return 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            previous = {
                row["id"]: row
                for row in conn.execute(
                    """SELECT id, content_hash, embedding, embedding_dim, embedding_model
                       FROM wiki_search_units WHERE page_id = ?""",
                    (page_id,),
                ).fetchall()
            }
            conn.execute("DELETE FROM wiki_search_units WHERE page_id = ?", (page_id,))
            if self._fts_enabled:
                conn.execute("DELETE FROM wiki_search_units_fts WHERE unit_id LIKE ?", (f"{page_id}:%",))
            for unit in units:
                old = previous.get(unit["id"])
                unchanged = bool(old and old["content_hash"] == unit["content_hash"])
                conn.execute(
                    """INSERT INTO wiki_search_units
                       (id, page_id, page_type, unit_kind, section, title, text,
                        aliases_text, lexical_terms, content_hash, embedding,
                        embedding_dim, embedding_model, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        unit["id"], page_id, unit["page_type"], unit["unit_kind"],
                        unit["section"], unit["title"], unit["text"], unit["aliases_text"],
                        unit["lexical_terms"], unit["content_hash"],
                        old["embedding"] if unchanged else None,
                        int(old["embedding_dim"] or 0) if unchanged else 0,
                        str(old["embedding_model"] or "") if unchanged else "",
                        now,
                    ),
                )
                if self._fts_enabled:
                    conn.execute(
                        """INSERT INTO wiki_search_units_fts
                           (unit_id, title, section, text, aliases, terms)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            unit["id"], unit["title"], unit["section"], unit["text"],
                            unit["aliases_text"], unit["lexical_terms"],
                        ),
                    )
            conn.commit()
        return len(units)

    def delete_page(self, page_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM wiki_search_units WHERE page_id = ?", (page_id,))
            if self._fts_enabled:
                conn.execute("DELETE FROM wiki_search_units_fts WHERE unit_id LIKE ?", (f"{page_id}:%",))
            conn.commit()

    def search_fts(
        self,
        query: str,
        limit: int = 30,
        page_types: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        fts_query = _fts_query(query)
        if not self._fts_enabled or not fts_query:
            return self._search_like(query, limit, page_types)
        allowed = [str(item) for item in (page_types or []) if str(item)]
        where_type = ""
        params: list[Any] = [fts_query]
        if allowed:
            placeholders = ",".join("?" for _ in allowed)
            where_type = f" AND u.page_type IN ({placeholders})"
            params.extend(allowed)
        params.append(max(1, min(int(limit), 200)))
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    f"""SELECT u.*, bm25(wiki_search_units_fts, 0, 3, 1.4, 1, 2, 1.5) AS lexical_score
                        FROM wiki_search_units_fts f
                        JOIN wiki_search_units u ON u.id = f.unit_id
                        WHERE wiki_search_units_fts MATCH ? {where_type}
                        ORDER BY lexical_score ASC LIMIT ?""",
                    params,
                ).fetchall()
            return [_unit_result(row, score=-float(row["lexical_score"] or 0)) for row in rows]
        except sqlite3.OperationalError:
            return self._search_like(query, limit, page_types)

    def _search_like(
        self,
        query: str,
        limit: int,
        page_types: Iterable[str] | None,
    ) -> list[dict[str, Any]]:
        allowed = [str(item) for item in (page_types or []) if str(item)]
        where_type = ""
        params: list[Any] = [f"%{query}%", f"%{query}%", f"%{query}%"]
        if allowed:
            placeholders = ",".join("?" for _ in allowed)
            where_type = f" AND page_type IN ({placeholders})"
            params.extend(allowed)
        params.append(max(1, min(int(limit), 200)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT * FROM wiki_search_units
                    WHERE (title LIKE ? OR text LIKE ? OR aliases_text LIKE ?) {where_type}
                    ORDER BY updated_at DESC LIMIT ?""",
                params,
            ).fetchall()
        return [_unit_result(row, score=0.0) for row in rows]

    def pending_embedding_count(self, model: str = "") -> int:
        with closing(self._connect()) as conn:
            if model:
                row = conn.execute(
                    """SELECT COUNT(*) FROM wiki_search_units
                       WHERE embedding IS NULL OR embedding_model <> ?""",
                    (model,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM wiki_search_units WHERE embedding IS NULL"
                ).fetchone()
        return int(row[0] if row else 0)

    def backfill_embeddings(self, limit: int = 64) -> int:
        if self.embedder is None:
            return 0
        model = str(getattr(self.embedder, "model", "embedding"))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT id, title, section, text, aliases_text FROM wiki_search_units
                   WHERE embedding IS NULL OR embedding_model <> ?
                   ORDER BY updated_at ASC LIMIT ?""",
                (model, max(1, min(int(limit), 256))),
            ).fetchall()
        if not rows:
            return 0
        texts = [_embedding_text(dict(row)) for row in rows]
        vectors = self.embedder.embed_documents(texts)
        if len(vectors) != len(rows):
            raise RuntimeError("Embedding API returned an unexpected vector count.")
        with closing(self._connect()) as conn:
            for row, vector in zip(rows, vectors):
                packed, dimension = _pack_vector(vector)
                conn.execute(
                    """UPDATE wiki_search_units
                       SET embedding = ?, embedding_dim = ?, embedding_model = ?
                       WHERE id = ?""",
                    (packed, dimension, model, row["id"]),
                )
            conn.commit()
        return len(rows)

    def search_vector(
        self,
        query: str,
        limit: int = 30,
        page_types: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.embedder is None or not (query or "").strip():
            return []
        model = str(getattr(self.embedder, "model", "embedding"))
        allowed = [str(item) for item in (page_types or []) if str(item)]
        where_type = ""
        params: list[Any] = [model]
        if allowed:
            placeholders = ",".join("?" for _ in allowed)
            where_type = f" AND page_type IN ({placeholders})"
            params.extend(allowed)
        with closing(self._connect()) as conn:
            count = conn.execute(
                f"""SELECT COUNT(*) FROM wiki_search_units
                    WHERE embedding IS NOT NULL AND embedding_model = ? {where_type}""",
                params,
            ).fetchone()[0]
            if not count:
                return []
            rows = conn.execute(
                f"""SELECT * FROM wiki_search_units
                    WHERE embedding IS NOT NULL AND embedding_model = ? {where_type}""",
                params,
            ).fetchall()
        query_vector = self.embedder.embed_query(query)
        if not query_vector:
            return []
        query_norm = _normalized_vector(query_vector)
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            vector = array("f")
            vector.frombytes(row["embedding"])
            if len(vector) != len(query_norm):
                continue
            score = sum(left * right for left, right in zip(query_norm, vector))
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [
            _unit_result(row, score=score)
            for score, row in ranked[: max(1, min(int(limit), 200))]
        ]


def build_search_units(card: dict[str, Any]) -> list[dict[str, str]]:
    page_id = str(card.get("id") or "")
    title = str(card.get("title") or "").strip()
    page_type = str(card.get("page_type") or "")
    content = card.get("content_json") if isinstance(card.get("content_json"), dict) else {}
    aliases = _string_values(content.get("aliases"))
    aliases.extend(_string_values(card.get("related_topics")))
    aliases = _unique(aliases)
    aliases_text = " ".join(aliases)
    values: list[tuple[str, str, str]] = []
    summary = str(card.get("summary") or "").strip()
    if summary:
        values.append(("summary", "Summary", summary))
    for key, value in content.items():
        if key in SEARCH_EXCLUDED_KEYS or key.startswith("_") or len(key) > 64:
            continue
        text = _public_text(value)
        if text:
            values.append(("section", key.replace("_", " ").title(), text))
    if not values:
        values.append(("title", "Title", title))

    units: list[dict[str, str]] = []
    seen_text: set[str] = set()
    for index, (kind, section, text) in enumerate(values):
        text = re.sub(r"\s+", " ", text).strip()[:6000]
        normalized = text.casefold()
        if not text or normalized in seen_text:
            continue
        seen_text.add(normalized)
        digest = hashlib.sha1(f"{section}\n{text}".encode("utf-8")).hexdigest()
        unit_id = f"{page_id}:{index}:{digest[:12]}"
        lexical_terms = " ".join(_lexical_terms(f"{title} {aliases_text} {section} {text}"))
        units.append({
            "id": unit_id,
            "page_type": page_type,
            "unit_kind": kind,
            "section": section,
            "title": title,
            "text": text,
            "aliases_text": aliases_text,
            "lexical_terms": lexical_terms,
            "content_hash": digest,
        })
    return units


def _unit_result(row: sqlite3.Row, score: float) -> dict[str, Any]:
    return {
        "unit_id": row["id"],
        "page_id": row["page_id"],
        "page_type": row["page_type"],
        "section": row["section"],
        "title": row["title"],
        "snippet": str(row["text"] or "")[:500],
        "score": float(score),
    }


def _fts_query(query: str) -> str:
    terms = _lexical_terms(query)
    return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:32] if term)


def _lexical_terms(text: str) -> list[str]:
    normalized = str(text or "").casefold()
    terms = re.findall(r"[a-z][a-z0-9_.+-]{1,}|\d+(?:\.\d+)?", normalized)
    for block in re.findall(r"[\u3400-\u9fff]{2,}", normalized):
        if len(block) <= 3:
            terms.append(block)
        terms.extend(block[index:index + 2] for index in range(len(block) - 1))
    return _unique(terms)[:256]


def _public_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_public_text(item) for item in value if _public_text(item))
    if isinstance(value, dict):
        return "\n".join(
            f"{key}: {_public_text(nested)}" for key, nested in value.items()
            if key not in SEARCH_EXCLUDED_KEYS and _public_text(nested)
        )
    return str(value)


def _string_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def _embedding_text(row: dict[str, Any]) -> str:
    return "\n".join(
        part for part in (
            f"Title: {row.get('title', '')}",
            f"Section: {row.get('section', '')}",
            str(row.get("text") or ""),
            f"Aliases: {row.get('aliases_text', '')}" if row.get("aliases_text") else "",
        ) if part
    )[:8000]


def _normalized_vector(vector: Iterable[float]) -> array:
    values = array("f", (float(item) for item in vector))
    norm = math.sqrt(sum(item * item for item in values)) or 1.0
    return array("f", (item / norm for item in values))


def _pack_vector(vector: Iterable[float]) -> tuple[bytes, int]:
    values = _normalized_vector(vector)
    return values.tobytes(), len(values)
