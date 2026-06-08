from __future__ import annotations

import argparse
import json
from pathlib import Path

from system.storage import get_object_storage, get_storage_layout
from system.wiki.maintenance.store import WikiMaintenanceStore
from system.wiki.maintenance.validator import WikiValidator


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic wiki validation.")
    parser.add_argument("--check-storage", action="store_true", help="Check OSS/local object existence.")
    parser.add_argument("--create-repair-tasks", action="store_true", help="Create repair task rows for validation errors.")
    parser.add_argument("--no-persist", action="store_true", help="Do not write validation run to DB/OSS.")
    args = parser.parse_args()

    report = WikiValidator().validate_all(check_storage=args.check_storage)
    artifact_uri = ""
    if not args.no_persist:
        layout = get_storage_layout()
        out_dir = layout.query_artifact_path("validation_reports", report["run_id"], ".json").parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{report['run_id']}.json"
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        out_path.write_text(payload, encoding="utf-8")
        artifact_uri = get_object_storage().upload_text(
            get_object_storage().key_for_local_path(out_path),
            payload,
            content_type="application/json; charset=utf-8",
        )
        store = WikiMaintenanceStore()
        store.add_validation_run(report=report, artifact_uri=artifact_uri)
        if args.create_repair_tasks:
            store.create_repair_tasks_from_report(report)

    print(json.dumps({"artifact_uri": artifact_uri, **report}, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
