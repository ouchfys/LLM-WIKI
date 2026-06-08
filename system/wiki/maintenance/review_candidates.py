from __future__ import annotations

import argparse
import json

from system.core.config import SILICONFLOW_MAINTENANCE_MODEL
from system.core.siliconflow_client import SiliconFlowChat
from system.wiki.maintenance.candidate_processor import MaintenanceCandidateProcessor


def main() -> int:
    parser = argparse.ArgumentParser(description="Review and conservatively merge staged maintenance candidates.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--no-auto-merge", action="store_true")
    parser.add_argument("--use-llm", action="store_true", help="Use the maintenance reviewer/merge-planner LLM.")
    args = parser.parse_args()

    llm = SiliconFlowChat(
        model=SILICONFLOW_MAINTENANCE_MODEL,
        temperature=0.0,
        max_tokens=3200,
    ) if args.use_llm else None
    result = MaintenanceCandidateProcessor(llm=llm).process_pending(
        limit=max(1, args.limit),
        auto_merge=not args.no_auto_merge,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
