from __future__ import annotations

import argparse
import json

from system.wiki.maintenance.runner import WikiMaintenanceRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one deterministic wiki maintenance cycle.")
    parser.add_argument("--check-storage", action="store_true", help="Check object storage existence.")
    parser.add_argument("--no-repair-tasks", action="store_true", help="Do not create repair task rows.")
    parser.add_argument("--no-process-repairs", action="store_true", help="Do not process deterministic repair tasks.")
    parser.add_argument("--process-llm-repairs", action="store_true", help="Process semantic repair tasks with the Repair Agent.")
    parser.add_argument("--distill-query-insights", action="store_true", help="Distill archived query insights into candidates.")
    parser.add_argument("--process-candidates", action="store_true", help="Review and conservatively merge staged maintenance candidates.")
    parser.add_argument("--process-web-sources", action="store_true", help="Process queued web_source ingestion jobs.")
    parser.add_argument("--no-indices", action="store_true", help="Do not generate query indices.")
    parser.add_argument("--no-upload-indices", action="store_true", help="Generate local indices only.")
    args = parser.parse_args()

    result = WikiMaintenanceRunner().run_once(
        check_storage=args.check_storage,
        create_repair_tasks=not args.no_repair_tasks,
        process_deterministic_repairs=not args.no_process_repairs,
        process_llm_repairs=args.process_llm_repairs,
        distill_query_insights=args.distill_query_insights,
        process_candidates=args.process_candidates,
        process_web_sources=args.process_web_sources,
        generate_indices=not args.no_indices,
        upload_indices=not args.no_upload_indices,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
