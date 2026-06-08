from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.storage import get_object_storage, get_storage_layout
from system.wiki.markdown_reindexer import MarkdownWikiReindexer


def main() -> int:
    parser = argparse.ArgumentParser(description="Reindex canonical Markdown wiki cards into SQLite.")
    parser.add_argument("references", nargs="*", help="Markdown file paths, storage keys, local:// URIs, or oss:// URIs.")
    parser.add_argument("--wiki-dir", default="", help="Local wiki directory to scan when no references are provided.")
    parser.add_argument("--oss-prefix", default="", help="OSS/local storage prefix to scan for .md files.")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    references = list(args.references)
    if not references and args.oss_prefix:
        references = [
            item["uri"]
            for item in get_object_storage().list(args.oss_prefix, limit=args.limit)
            if str(item.get("key", "")).lower().endswith(".md")
        ]
    if not references:
        wiki_dir = Path(args.wiki_dir) if args.wiki_dir else get_storage_layout().wiki_dir
        references = [str(path) for path in sorted(wiki_dir.rglob("*.md"))]
    if not references:
        raise SystemExit("No Markdown references found.")

    reindexer = MarkdownWikiReindexer()
    rows = []
    for reference in references:
        try:
            row = reindexer.reindex_reference(reference)
            row["reference"] = reference
        except Exception as exc:
            row = {"ok": False, "reference": reference, "error": str(exc)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    summary = {
        "count": len(rows),
        "ok_count": sum(1 for row in rows if row.get("ok")),
        "failed_count": sum(1 for row in rows if not row.get("ok")),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
