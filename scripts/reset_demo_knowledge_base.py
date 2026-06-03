from __future__ import annotations

import argparse
import sqlite3
import shutil
import sys
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.storage import get_object_storage


DB_TABLES = [
    "wiki_pages",
    "wiki_chunks",
    "papers",
    "paper_blocks",
    "source_packets",
    "distilled_candidates",
    "review_reports",
    "wiki_card_sources",
    "wiki_card_links",
    "wiki_aliases",
]

LOCAL_GENERATED_DIRS = [
    REPO_ROOT / "data" / "generated" / "wiki",
    REPO_ROOT / "data" / "wiki",
]

RAW_SOURCE_DIR = REPO_ROOT / "data" / "raw_sources"

OSS_GENERATED_PREFIXES = [
    "users/admin/data/generated/wiki",
    "users/admin/data/wiki",
    "users/admin/data/raw_sources",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reset generated demo Wiki knowledge without deleting original PDFs.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Show what would be deleted.")
    mode.add_argument("--execute", action="store_true", help="Apply the reset.")
    parser.add_argument("--db-path", default=str(REPO_ROOT / "sessions.db"), help="SQLite database path.")
    parser.add_argument("--keep-pdfs", action="store_true", default=True, help="Keep data/*.pdf files. Default: true.")
    parser.add_argument("--clear-pipeline-db", action="store_true", help="Clear paper/wiki pipeline SQLite tables.")
    parser.add_argument("--clear-local-raw-sources", action="store_true", help="Also remove data/raw_sources.")
    parser.add_argument("--clear-oss-generated", action="store_true", help="Delete generated OSS/local-storage prefixes.")
    parser.add_argument("--clear-oss-pdfs", action="store_true", help="Also delete users/admin/data/*.pdf objects.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    execute = bool(args.execute)

    print(f"Repo root: {REPO_ROOT}")
    print(f"Mode: {'execute' if execute else 'dry-run'}")
    print(f"Keep data/*.pdf: {args.keep_pdfs}")

    if args.clear_pipeline_db:
        reset_db(Path(args.db_path), execute=execute)
    else:
        print("DB cleanup skipped. Pass --clear-pipeline-db to clear pipeline tables.")

    reset_local_files(
        execute=execute,
        clear_raw_sources=args.clear_local_raw_sources,
    )

    if args.clear_oss_generated:
        reset_object_storage(
            execute=execute,
            clear_pdfs=args.clear_oss_pdfs,
        )
    else:
        print("OSS cleanup skipped. Pass --clear-oss-generated to remove generated object-storage artifacts.")

    return 0


def reset_db(db_path: Path, execute: bool) -> None:
    print(f"SQLite DB: {db_path}")
    if not db_path.exists():
        print("  DB does not exist; nothing to clear.")
        return

    with closing(sqlite3.connect(str(db_path))) as conn:
        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
        }
        for table in DB_TABLES:
            if table not in existing:
                print(f"  skip missing table: {table}")
                continue
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  clear {table}: {count} rows")
            if execute:
                conn.execute(f"DELETE FROM {table}")
        if "wiki_pages_fts" in existing:
            print("  rebuild wiki_pages_fts")
            if execute:
                conn.execute("INSERT INTO wiki_pages_fts(wiki_pages_fts) VALUES ('rebuild')")
        if execute:
            conn.commit()


def reset_local_files(execute: bool, clear_raw_sources: bool) -> None:
    dirs = list(LOCAL_GENERATED_DIRS)
    if clear_raw_sources:
        dirs.append(RAW_SOURCE_DIR)

    for path in dirs:
        if not path.exists():
            print(f"Local path missing: {path}")
            continue
        file_count = sum(1 for item in path.rglob("*") if item.is_file())
        print(f"Local delete: {path} ({file_count} files)")
        if execute:
            shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=True)


def reset_object_storage(execute: bool, clear_pdfs: bool) -> None:
    storage = get_object_storage()
    prefixes = list(OSS_GENERATED_PREFIXES)
    if clear_pdfs:
        prefixes.append("users/admin/data")

    for prefix in prefixes:
        if clear_pdfs and prefix.endswith("/data"):
            items = [item for item in storage.list(prefix, limit=2000) if str(item.get("key", "")).lower().endswith(".pdf")]
            print(f"Object delete PDFs under {prefix}: {len(items)} objects")
            if execute:
                for item in items:
                    storage.delete(str(item.get("uri") or item.get("key") or ""))
            continue
        items = storage.list(prefix, limit=2000)
        print(f"Object delete prefix {prefix}: {len(items)} objects")
        if execute and items:
            storage.delete_prefix(prefix, limit=2000)


if __name__ == "__main__":
    raise SystemExit(main())
