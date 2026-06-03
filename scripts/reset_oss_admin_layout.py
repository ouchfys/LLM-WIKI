"""Clean legacy OSS prefixes after switching to users/admin layout.

This removes only known old personal-workspace prefixes:
- dev/users/default/
- users/default/

It never deletes users/admin/.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.storage import get_object_storage


LEGACY_PREFIXES = [
    "dev/users/default/",
    "users/default/",
]


def main() -> None:
    storage = get_object_storage()
    if not storage.enabled:
        print(json.dumps({
            "backend": storage.backend,
            "skipped": "STORAGE_BACKEND is not oss",
        }, ensure_ascii=False, indent=2))
        return

    deleted: dict[str, int] = {}
    for prefix in LEGACY_PREFIXES:
        if prefix.strip("/").lower() == "users/admin":
            raise RuntimeError("Refusing to delete the active admin prefix.")
        uri = f"oss://{storage.bucket_name}/{prefix.strip('/')}/"
        deleted[prefix] = storage.delete_prefix(uri, limit=10000)

    print(json.dumps({
        "backend": storage.backend,
        "active_prefix": storage.root_prefix,
        "deleted": deleted,
        "admin_sample": storage.list("data", limit=20),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
