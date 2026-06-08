from __future__ import annotations

import argparse
import json

from system.wiki.maintenance.web_source_ingestion import WebSourceIngestionProcessor


def main() -> int:
    parser = argparse.ArgumentParser(description="Process queued web_source ingestion jobs.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--no-auto-merge", action="store_true")
    args = parser.parse_args()

    result = WebSourceIngestionProcessor().process_queued(
        limit=max(1, args.limit),
        auto_merge=not args.no_auto_merge,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
