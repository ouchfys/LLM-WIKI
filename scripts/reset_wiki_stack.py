from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.storage import get_object_storage


KEEP_DB_BACKUP = os.environ.get("RESET_WIKI_KEEP_DB_BACKUP", "").strip() == "1"


DB_TABLES_TO_CLEAR = [
    "wiki_pages_fts",
    "wiki_pages",
    "wiki_chunks_fts",
    "wiki_chunks",
    "papers",
    "paper_blocks",
]


def backup_file(path: Path) -> str:
    if not path.exists() or not KEEP_DB_BACKUP:
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return str(backup.relative_to(REPO_ROOT))


def quarantine_database(path: Path) -> str:
    if not path.exists():
        return ""
    if not KEEP_DB_BACKUP:
        path.unlink()
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.malformed-{stamp}")
    shutil.move(str(path), str(target))
    return str(target.relative_to(REPO_ROOT))


def migrate_legacy_raw_sources() -> int:
    legacy = REPO_ROOT / "data" / "wiki" / "raw_sources"
    target = REPO_ROOT / "data" / "raw_sources" / "markdown"
    if not legacy.exists():
        return 0

    storage = get_object_storage()
    migrated = 0
    for src in legacy.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(legacy)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(src, dest)
            migrated += 1
        storage.upload_file(dest, content_type="text/markdown; charset=utf-8")
    return migrated


def clear_database() -> dict[str, int]:
    db_path = REPO_ROOT / "sessions.db"
    if not db_path.exists():
        return {}

    deleted: dict[str, int] = {}
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        for table in DB_TABLES_TO_CLEAR:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                conn.execute(f"DELETE FROM {table}")
                deleted[table] = count
            except sqlite3.OperationalError:
                deleted[table] = -1
        conn.commit()
    except sqlite3.DatabaseError:
        if conn is not None:
            conn.close()
            conn = None
        malformed_backup = quarantine_database(db_path)
        deleted["__malformed_database_replaced__"] = 1
        deleted["__malformed_database_backup__"] = malformed_backup
    finally:
        if conn is not None:
            conn.close()

    return deleted


def remove_generated_wiki_dirs() -> list[str]:
    removed = []
    targets = [
        REPO_ROOT / "data" / "wiki",
        REPO_ROOT / "data" / "generated" / "wiki",
    ]
    for target in targets:
        if not target.exists():
            continue
        resolved = target.resolve()
        data_root = (REPO_ROOT / "data").resolve()
        if data_root not in resolved.parents:
            raise RuntimeError(f"Refusing to delete outside data/: {target}")
        shutil.rmtree(target)
        removed.append(str(target.relative_to(REPO_ROOT)))
    return removed


def main() -> None:
    db_backup = backup_file(REPO_ROOT / "sessions.db")
    migrated_raw_sources = migrate_legacy_raw_sources()
    deleted_rows = clear_database()
    removed_dirs = remove_generated_wiki_dirs()

    print(json.dumps({
        "db_backup": db_backup,
        "migrated_raw_sources": migrated_raw_sources,
        "deleted_rows": deleted_rows,
        "removed_dirs": removed_dirs,
        "preserved": [
            "data/*.pdf",
            "data/raw_sources/**",
            ".env",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
