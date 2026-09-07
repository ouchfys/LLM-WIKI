"""Move locally recoverable artifacts and SQLite URIs to a new OSS tenant.

Dry-run is the default.  ``--apply`` first creates an online SQLite backup,
uploads only durable/current artifacts, and updates legacy URI strings after
all uploads have been verified to exist in the configured object store.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.storage import get_object_storage


@dataclass(frozen=True)
class Artifact:
    local_path: Path
    key: str
    kind: str

    def report(self) -> dict[str, object]:
        return {
            "local_path": str(self.local_path),
            "key": self.key,
            "kind": self.kind,
            "bytes": self.local_path.stat().st_size if self.local_path.exists() else 0,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(REPO_ROOT / "sessions.db"))
    parser.add_argument("--old-bucket", required=True)
    parser.add_argument("--old-prefix", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup-dir",
        default=str(REPO_ROOT.parent / "migration-backups"),
    )
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    storage = get_object_storage()
    if not storage.enabled:
        raise RuntimeError("Tenant migration requires STORAGE_BACKEND=oss.")
    old_prefix = str(args.old_prefix).strip("/")
    new_prefix = storage.root_prefix.strip("/")
    if not old_prefix or old_prefix == new_prefix:
        raise ValueError("Old and new storage prefixes must be non-empty and different.")

    artifacts, missing = collect_artifacts(db_path, old_prefix=old_prefix)
    references = matching_references(
        db_path,
        old_bucket=args.old_bucket,
        old_prefix=old_prefix,
    )
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "database": str(db_path),
        "old_root": f"oss://{args.old_bucket}/{old_prefix}",
        "new_root": f"oss://{storage.bucket_name}/{new_prefix}",
        "tenant_id": storage.tenant_id,
        "artifact_count": len(artifacts),
        "artifact_bytes": sum(item.local_path.stat().st_size for item in artifacts),
        "missing_local_artifacts": missing,
        "database_reference_columns": references,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not args.apply:
        print("Dry-run only. Re-run with --apply after checking this report.", flush=True)
        return 0
    if missing:
        raise RuntimeError("Migration stopped because referenced local artifacts are missing.")

    backup = backup_database(db_path, Path(args.backup_dir))
    print(f"Backup: {backup}", flush=True)
    upload_artifacts(storage, artifacts)
    replacements = [
        (
            f"oss://{args.old_bucket}/{old_prefix}",
            f"oss://{storage.bucket_name}/{new_prefix}",
        ),
        (f"local://{old_prefix}", f"local://{new_prefix}"),
        (f"{old_prefix}/", f"{new_prefix}/"),
    ]
    changed = replace_database_references(db_path, replacements)
    remaining = matching_references(
        db_path,
        old_bucket=args.old_bucket,
        old_prefix=old_prefix,
    )
    if remaining:
        raise RuntimeError(f"Legacy storage references remain after migration: {remaining}")
    print(json.dumps({
        "complete": True,
        "backup": str(backup),
        "uploaded": len(artifacts),
        "database_cells_changed": changed,
        "new_root": f"oss://{storage.bucket_name}/{new_prefix}",
    }, ensure_ascii=False, indent=2), flush=True)
    return 0


def collect_artifacts(db_path: Path, *, old_prefix: str) -> tuple[list[Artifact], list[str]]:
    artifacts: dict[str, Artifact] = {}
    missing: list[str] = []
    with sqlite3.connect(str(db_path), timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        source_rows = conn.execute(
            "SELECT raw_source_path, pdf_storage_uri FROM source_packets ORDER BY id"
        ).fetchall()
        wiki_rows = conn.execute(
            "SELECT markdown_path FROM wiki_pages WHERE markdown_path != '' ORDER BY id"
        ).fetchall()

    for row in source_rows:
        for column, kind in (("raw_source_path", "raw_source"), ("pdf_storage_uri", "paper_original")):
            key = key_after_tenant(str(row[column] or ""), old_prefix=old_prefix)
            if not key:
                continue
            local_path = REPO_ROOT / key
            if kind == "paper_original" and not local_path.is_file():
                upload_copy = REPO_ROOT / "sources" / "papers" / "uploads" / local_path.name
                if upload_copy.is_file():
                    local_path = upload_copy
            add_artifact(artifacts, missing, local_path, key, kind)

    for row in wiki_rows:
        value = str(row["markdown_path"] or "")
        key = key_after_tenant(value, old_prefix=old_prefix) or value.replace("\\", "/").lstrip("/")
        if not key.startswith("wiki/"):
            continue
        add_artifact(artifacts, missing, REPO_ROOT / key, key, "wiki_page")

    for directory, kind in (("queries", "query_artifact"), ("maintenance", "maintenance_artifact")):
        base = REPO_ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                key = path.relative_to(REPO_ROOT).as_posix()
                add_artifact(artifacts, missing, path, key, kind)

    return sorted(artifacts.values(), key=lambda item: item.key), sorted(set(missing))


def add_artifact(
    artifacts: dict[str, Artifact],
    missing: list[str],
    local_path: Path,
    key: str,
    kind: str,
) -> None:
    resolved = local_path.resolve()
    if REPO_ROOT.resolve() not in resolved.parents:
        raise ValueError(f"Artifact escaped repository root: {resolved}")
    if not resolved.is_file():
        missing.append(str(resolved))
        return
    artifacts.setdefault(key, Artifact(local_path=resolved, key=key, kind=kind))


def key_after_tenant(reference: str, *, old_prefix: str) -> str:
    value = str(reference or "").replace("\\", "/")
    marker = f"/{old_prefix.strip('/')}/"
    if marker in value:
        return value.split(marker, 1)[1]
    if value.startswith(old_prefix.rstrip("/") + "/"):
        return value[len(old_prefix.rstrip("/") + "/"):]
    return ""


def matching_references(
    db_path: Path,
    *,
    old_bucket: str,
    old_prefix: str,
) -> dict[str, int]:
    needles = (f"oss://{old_bucket}/", old_prefix)
    matches: dict[str, int] = {}
    with sqlite3.connect(str(db_path), timeout=60) as conn:
        for table, column in text_columns(conn):
            count = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE instr("{column}", ?) > 0 OR instr("{column}", ?) > 0',
                needles,
            ).fetchone()[0]
            if count:
                matches[f"{table}.{column}"] = int(count)
    return matches


def text_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in tables:
        safe_table = str(table).replace('"', '""')
        for row in conn.execute(f'PRAGMA table_info("{safe_table}")'):
            column = str(row[1])
            declared_type = str(row[2] or "").upper()
            if "TEXT" in declared_type:
                result.append((safe_table, column.replace('"', '""')))
    return result


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    backup_dir = backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"{db_path.name}.pre-tenant-migration-{stamp}.bak"
    with sqlite3.connect(str(db_path), timeout=60) as source:
        with sqlite3.connect(str(destination), timeout=60) as target:
            source.backup(target)
    return destination


def upload_artifacts(storage, artifacts: list[Artifact]) -> None:
    for index, artifact in enumerate(artifacts, start=1):
        content_type = mimetypes.guess_type(artifact.local_path.name)[0] or "application/octet-stream"
        uri = storage.upload_file(artifact.local_path, key=artifact.key, content_type=content_type)
        if not storage.exists(uri):
            raise RuntimeError(f"Uploaded object could not be verified: {uri}")
        print(
            f"Upload {index}/{len(artifacts)} [{artifact.kind}]: {artifact.key}",
            flush=True,
        )


def replace_database_references(
    db_path: Path,
    replacements: list[tuple[str, str]],
) -> int:
    changed = 0
    with sqlite3.connect(str(db_path), timeout=120) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for table, column in text_columns(conn):
            for old, new in replacements:
                cursor = conn.execute(
                    f'UPDATE "{table}" SET "{column}" = replace("{column}", ?, ?) '
                    f'WHERE instr("{column}", ?) > 0',
                    (old, new, old),
                )
                changed += max(0, int(cursor.rowcount))
        conn.commit()
    return changed


if __name__ == "__main__":
    raise SystemExit(main())
