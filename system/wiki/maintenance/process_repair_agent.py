from __future__ import annotations

import argparse
import json

from system.core.config import SILICONFLOW_MAINTENANCE_MODEL
from system.core.siliconflow_client import SiliconFlowChat
from system.wiki.maintenance.repair_agent import WikiRepairAgent


def main() -> int:
    parser = argparse.ArgumentParser(description="Process semantic wiki repair tasks with the LLM Repair Agent.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--no-llm", action="store_true", help="Do not initialize the LLM; pending tasks may remain pending.")
    parser.add_argument("--no-upload", action="store_true", help="Write local candidate artifacts only.")
    args = parser.parse_args()

    llm = None if args.no_llm else SiliconFlowChat(
        model=SILICONFLOW_MAINTENANCE_MODEL,
        temperature=0.0,
        max_tokens=3200,
    )
    result = WikiRepairAgent(llm=llm).process_pending(
        limit=max(1, args.limit),
        upload=not args.no_upload,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
