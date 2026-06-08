from __future__ import annotations

import argparse
import json

from system.wiki.maintenance.index_generator import WikiIndexGenerator


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic wiki query indices.")
    parser.add_argument("--no-upload", action="store_true", help="Write local query files without uploading to object storage.")
    args = parser.parse_args()

    result = WikiIndexGenerator().generate_all(upload=not args.no_upload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
