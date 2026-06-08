from __future__ import annotations

import argparse
import json

from system.wiki.maintenance.repair_processor import DeterministicRepairProcessor


def main() -> int:
    parser = argparse.ArgumentParser(description="Process deterministic wiki repair tasks.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--no-upload-indices", action="store_true")
    args = parser.parse_args()

    result = DeterministicRepairProcessor().process_pending(
        limit=max(1, args.limit),
        upload_indices=not args.no_upload_indices,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
