"""Migrate an existing LLM-WIKI database to the scalable page-first layout.

Dry-run is the default.  ``--apply`` creates a timestamped database backup,
verifies every object write before clearing legacy inline Docling JSON, renders
clean Wiki Markdown, and rebuilds section-level search units.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.storage import get_object_storage, get_storage_layout
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.wiki_store import WikiStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(REPO_ROOT / "sessions.db"))
    parser.add_argument(
        "--backup-dir",
        default=str(REPO_ROOT.parent / "migration-backups"),
        help="Backup directory outside the repository by default.",
    )
    parser.add_argument("--apply", action="store_true", help="Execute migration; default is read-only dry-run.")
    parser.add_argument("--skip-docling", action="store_true")
    parser.add_argument("--skip-markdown", action="store_true")
    parser.add_argument("--embeddings", action="store_true", help="Backfill vectors after lexical migration.")
    parser.add_argument("--vacuum", action="store_true", help="Compact SQLite after large inline fields are cleared.")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    stats = inspect_database(db_path)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **stats}, ensure_ascii=False, indent=2))
    if not args.apply:
        print("Dry-run only. Re-run with --apply after checking the report.")
        return 0

    # Store initialization performs the additive schema migration only after
    # the operator explicitly opted into writes.
    PaperWikiPipelineStore(db_path=str(db_path))
    wiki = WikiStore(db_path=str(db_path))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(args.backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{db_path.name}.pre-scalable-wiki-{timestamp}.bak"
    shutil.copy2(db_path, backup)
    print(f"Backup: {backup}", flush=True)

    if not args.skip_docling:
        migrate_docling_json(db_path)
    if not args.skip_markdown:
        render_clean_markdown(wiki)
    rebuild_search_units(wiki)
    if args.embeddings:
        from backend.deps import get_wiki_embeddings

        embedder = get_wiki_embeddings()
        if embedder is None:
            raise RuntimeError("Embedding client is disabled or unavailable.")
        wiki.search_index.embedder = embedder
        total = 0
        while True:
            count = wiki.search_index.backfill_embeddings(limit=32)
            total += count
            print(f"Embedding units: {total}", flush=True)
            if not count:
                break
    if args.vacuum:
        with sqlite3.connect(str(db_path), timeout=60) as conn:
            conn.execute("VACUUM")
        print("SQLite VACUUM complete.", flush=True)

    print(json.dumps({"complete": True, **inspect_database(db_path)}, ensure_ascii=False, indent=2))
    return 0


def inspect_database(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(str(db_path)) as conn:
        pages = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
        documents = conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
        inline = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(length(docling_json)), 0)
               FROM source_documents
               WHERE docling_json IS NOT NULL AND docling_json NOT IN ('', '{}')"""
        ).fetchone()
        try:
            units = conn.execute("SELECT COUNT(*) FROM wiki_search_units").fetchone()[0]
            vectors = conn.execute(
                "SELECT COUNT(*) FROM wiki_search_units WHERE embedding IS NOT NULL"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            units = vectors = 0
    return {
        "database_bytes": db_path.stat().st_size,
        "wiki_pages": int(pages),
        "source_documents": int(documents),
        "inline_docling_documents": int(inline[0]),
        "inline_docling_characters": int(inline[1]),
        "search_units": int(units),
        "embedded_units": int(vectors),
    }


def migrate_docling_json(db_path: Path) -> None:
    storage = get_object_storage()
    layout = get_storage_layout()
    with sqlite3.connect(str(db_path), timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, source_packet_id, content_hash, docling_json
               FROM source_documents
               WHERE docling_json IS NOT NULL AND docling_json NOT IN ('', '{}')
               ORDER BY created_at"""
        ).fetchall()
    for index, row in enumerate(rows, start=1):
        raw = str(row["docling_json"] or "")
        # Refuse to clear malformed legacy payloads.
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"Docling payload is not an object: {row['source_packet_id']}")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        key = layout.docling_json_key(row["source_packet_id"], row["content_hash"] or digest)
        uri = storage.write_text(key, raw, content_type="application/json; charset=utf-8")
        verified = storage.read_text(uri)
        if hashlib.sha256(verified.encode("utf-8")).hexdigest() != digest:
            raise ValueError(f"Object verification failed: {row['source_packet_id']}")
        with sqlite3.connect(str(db_path), timeout=60) as conn:
            cursor = conn.execute(
                """UPDATE source_documents
                   SET docling_json = '{}', docling_json_uri = ?, docling_json_hash = ?,
                       docling_json_size = ?
                   WHERE id = ? AND docling_json = ?""",
                (uri, digest, len(raw.encode("utf-8")), row["id"], raw),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Concurrent source update detected: {row['source_packet_id']}")
            conn.commit()
        print(f"Docling {index}/{len(rows)}: {row['source_packet_id']} -> {uri}", flush=True)


def render_clean_markdown(wiki: WikiStore) -> None:
    with sqlite3.connect(wiki.db_path) as conn:
        page_ids = [row[0] for row in conn.execute("SELECT id FROM wiki_pages ORDER BY id").fetchall()]
    for processed, page_id in enumerate(page_ids, start=1):
        card = wiki.get_card(page_id)
        if not card:
            continue
        wiki.update_card(
            card["id"],
            title=card["title"],
            page_type=card["page_type"],
            summary=card["summary"],
            content_json=card["content_json"],
            source_level=card["source_level"],
            source_urls_json=card["source_urls"],
            related_topics_json=card["related_topics"],
        )
        print(f"Markdown pages: {processed}/{len(page_ids)}", flush=True)


def rebuild_search_units(wiki: WikiStore) -> None:
    with sqlite3.connect(wiki.db_path) as conn:
        page_ids = [row[0] for row in conn.execute("SELECT id FROM wiki_pages ORDER BY id").fetchall()]
    pages = units = 0
    for page_id in page_ids:
        card = wiki.get_card(page_id)
        if card:
            units += wiki.search_index.replace_page(card)
            pages += 1
    with sqlite3.connect(wiki.db_path) as conn:
        conn.execute("UPDATE wiki_pages SET index_status = 'ready'")
        conn.commit()
    print(f"Search index: {pages} pages / {units} units", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
