from __future__ import annotations

import argparse
import json

from system.wiki.maintenance.query_insight import QueryInsightDistiller


def main() -> int:
    parser = argparse.ArgumentParser(description="Distill archived query answers into candidate insights.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--use-llm", action="store_true", help="Use the configured summary LLM instead of deterministic fallback.")
    args = parser.parse_args()

    llm = None
    if args.use_llm:
        try:
            from backend.deps import get_summary_llm
            llm = get_summary_llm()
        except Exception as exc:
            print(f"[distill_query_insights] summary LLM unavailable: {exc}")

    result = QueryInsightDistiller(llm=llm).distill_pending(limit=max(1, args.limit))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
