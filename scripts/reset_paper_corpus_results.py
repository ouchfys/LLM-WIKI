"""Reset generated paper/Wiki state while preserving source PDFs.

This is intentionally narrower than a repository reset.  It creates a local
backup, validates every filesystem target, clears only pipeline-derived SQLite
tables, removes generated caches, and never deletes sources/papers/originals.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.storage import get_object_storage


DB_TABLES = [
    "agent_events", "agent_checkpoints", "agent_approvals", "agent_runs",
    "claim_evidence", "wiki_claims", "wiki_revisions",
    "document_table_cells", "document_tables", "document_elements", "source_documents",
    "wiki_merge_audit", "review_reports", "distilled_candidates",
    "wiki_card_sources", "wiki_card_links", "wiki_aliases", "wiki_chunks",
    "source_packets", "paper_blocks", "papers", "wiki_pages",
    "ingestion_jobs", "wiki_maintenance_candidates", "wiki_repair_tasks",
    "wiki_validation_runs", "wiki_query_insights",
]
LOCAL_GENERATED = [
    REPO_ROOT / "wiki",
    REPO_ROOT / "sources" / "paper_pdf",
    REPO_ROOT / "queries",
    REPO_ROOT / "maintenance",
    REPO_ROOT / "test" / "paper_ingestion_runs",
]
OSS_GENERATED_PREFIXES = ["wiki", "sources/paper_pdf", "queries", "maintenance"]
ORIGINALS = (REPO_ROOT / "sources" / "papers" / "originals").resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--db-path", default=str(REPO_ROOT / "sessions.db"))
    parser.add_argument("--skip-oss", action="store_true")
    args = parser.parse_args()

    pdfs = sorted(ORIGINALS.glob("*.pdf"))
    if not ORIGINALS.is_dir() or not pdfs:
        raise RuntimeError(f"Refusing reset: no original PDF corpus at {ORIGINALS}")
    if any(ORIGINALS == target.resolve() or ORIGINALS in target.resolve().parents for target in LOCAL_GENERATED):
        raise RuntimeError("Refusing reset: a generated target overlaps the originals directory")

    db_path = Path(args.db_path).resolve()
    counts = _db_counts(db_path)
    local_counts = {str(path.relative_to(REPO_ROOT)): _file_count(path) for path in LOCAL_GENERATED}
    storage = get_object_storage()
    oss_counts = {}
    if storage.enabled and not args.skip_oss:
        for prefix in OSS_GENERATED_PREFIXES:
            oss_counts[prefix] = len(storage.list(prefix, limit=10000))

    report = {
        "mode": "execute" if args.execute else "dry-run",
        "original_pdfs_preserved": len(pdfs),
        "db_rows": counts,
        "local_files": local_counts,
        "oss_objects": oss_counts,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = REPO_ROOT / "backups" / f"pre-evidence-reingest-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    if db_path.exists():
        shutil.copy2(db_path, backup_dir / db_path.name)
    for source in LOCAL_GENERATED:
        if source.exists():
            shutil.copytree(source, backup_dir / source.name, dirs_exist_ok=True)

    deleted_db = _clear_db(db_path)
    removed_local = []
    for target in LOCAL_GENERATED:
        resolved = target.resolve()
        if REPO_ROOT.resolve() not in resolved.parents or resolved == REPO_ROOT.resolve():
            raise RuntimeError(f"Refusing unsafe generated path: {resolved}")
        if resolved.exists():
            shutil.rmtree(resolved)
            removed_local.append(str(resolved.relative_to(REPO_ROOT.resolve())))
        resolved.mkdir(parents=True, exist_ok=True)

    deleted_oss = {}
    if storage.enabled and not args.skip_oss:
        for prefix in OSS_GENERATED_PREFIXES:
            deleted_oss[prefix] = storage.delete_prefix(prefix, limit=10000)

    final = {
        "ok": True,
        "backup_dir": str(backup_dir.relative_to(REPO_ROOT)),
        "original_pdfs_preserved": len(list(ORIGINALS.glob("*.pdf"))),
        "deleted_db_rows": deleted_db,
        "removed_local": removed_local,
        "deleted_oss_objects": deleted_oss,
    }
    (backup_dir / "reset-report.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0


def _db_counts(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    with sqlite3.connect(path) as conn:
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in DB_TABLES if table in existing}


def _clear_db(path: Path) -> dict[str, int]:
    deleted = {}
    with sqlite3.connect(path) as conn:
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.execute("BEGIN IMMEDIATE")
        for table in DB_TABLES:
            if table not in existing:
                continue
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            deleted[table] = count
        for fts in ("wiki_pages_fts", "wiki_chunks_fts"):
            if fts in existing:
                conn.execute(f"INSERT INTO {fts}({fts}) VALUES ('rebuild')")
        conn.commit()
    return deleted


def _file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file()) if path.exists() else 0


if __name__ == "__main__":
    raise SystemExit(main())
