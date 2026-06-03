"""Smoke test for the configured object storage backend."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.storage import get_object_storage


def main() -> None:
    storage = get_object_storage()
    key = "healthcheck/oss-storage-smoke.md"
    text = (
        "# OSS Storage Smoke Test\n\n"
        f"- created_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        "- source: scripts/test_oss_storage.py\n"
    )
    uri = storage.upload_text(key, text)
    read_back = storage.read_text(uri)
    listed = storage.list("healthcheck", limit=20)
    exists_before_delete = storage.exists(uri)
    deleted = storage.delete(uri)
    exists_after_delete = storage.exists(uri)
    print(f"backend={storage.backend}")
    print(f"root_prefix={storage.root_prefix}")
    print(f"uri={uri}")
    print(f"read_back_ok={read_back == text}")
    print(f"list_count={len(listed)}")
    print(f"exists_before_delete={exists_before_delete}")
    print(f"deleted={deleted}")
    print(f"exists_after_delete={exists_after_delete}")
    print("ok=true")


if __name__ == "__main__":
    main()
