from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.storage import get_object_storage, get_storage_layout
from system.wiki.maintenance.candidate_processor import MaintenanceCandidateProcessor
from system.wiki.maintenance.index_generator import WikiIndexGenerator
from system.wiki.maintenance.planner import MaintenancePlanner
from system.wiki.maintenance.query_insight import QueryInsightDistiller
from system.wiki.maintenance.repair_agent import WikiRepairAgent
from system.wiki.maintenance.repair_processor import DeterministicRepairProcessor
from system.wiki.maintenance.store import WikiMaintenanceStore
from system.wiki.maintenance.validator import WikiValidator
from system.wiki.maintenance.web_source_ingestion import WebSourceIngestionProcessor


class WikiMaintenanceRunner:
    """Run the deterministic maintenance loop.

    This is the Phase-1/Phase-2 bridge from the runbook. It does not call LLM
    repair agents yet; it prepares typed repair tasks for later processing.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        planner_llm: Any = None,
        repair_llm: Any = None,
        query_insight_llm: Any = None,
        candidate_llm: Any = None,
    ):
        self.db_path = str(db_path) if db_path else None
        self.planner_llm = planner_llm
        self.repair_llm = repair_llm
        self.query_insight_llm = query_insight_llm
        self.candidate_llm = candidate_llm
        self.store = WikiMaintenanceStore(db_path=db_path)
        self.layout = get_storage_layout()
        self.storage = get_object_storage()

    def run_once(
        self,
        *,
        check_storage: bool = False,
        create_repair_tasks: bool = True,
        process_deterministic_repairs: bool = True,
        process_llm_repairs: bool = False,
        distill_query_insights: bool = False,
        process_candidates: bool = False,
        process_web_sources: bool = False,
        generate_indices: bool = True,
        upload_indices: bool = True,
    ) -> dict[str, Any]:
        validator = WikiValidator(db_path=self.db_path)
        report = validator.validate_all(check_storage=check_storage)
        artifact_uri = self._write_validation_artifact(report)
        run_id = self.store.add_validation_run(report=report, artifact_uri=artifact_uri)

        repair_task_ids: list[str] = []
        planner_result: dict[str, Any] = {}
        if create_repair_tasks:
            planner_result = MaintenancePlanner(llm=self.planner_llm).plan(report)
            repair_task_ids = self._create_repair_tasks_from_plan(run_id, planner_result)

        repair_result: dict[str, Any] = {}
        if process_deterministic_repairs and repair_task_ids:
            repair_result = DeterministicRepairProcessor(db_path=self.db_path).process_pending(
                limit=len(repair_task_ids),
                upload_indices=upload_indices,
            )

        llm_repair_result: dict[str, Any] = {}
        if process_llm_repairs:
            llm_repair_result = WikiRepairAgent(
                db_path=self.db_path,
                llm=self.repair_llm,
            ).process_pending(limit=max(len(repair_task_ids), 10), upload=upload_indices)

        query_insight_result: dict[str, Any] = {}
        if distill_query_insights:
            query_insight_result = QueryInsightDistiller(
                db_path=self.db_path,
                llm=self.query_insight_llm,
            ).distill_pending(limit=10)

        candidate_result: dict[str, Any] = {}
        if process_candidates:
            candidate_result = MaintenanceCandidateProcessor(
                db_path=self.db_path,
                llm=self.candidate_llm,
            ).process_pending(
                limit=20,
                auto_merge=True,
            )

        web_source_result: dict[str, Any] = {}
        if process_web_sources:
            web_source_result = WebSourceIngestionProcessor(db_path=self.db_path).process_queued(
                limit=10,
                auto_merge=True,
            )

        post_validation: dict[str, Any] = {}
        changed = bool(
            (llm_repair_result.get("processed") if isinstance(llm_repair_result, dict) else 0)
            or (query_insight_result.get("processed") if isinstance(query_insight_result, dict) else 0)
            or (candidate_result.get("processed") if isinstance(candidate_result, dict) else 0)
            or (web_source_result.get("processed") if isinstance(web_source_result, dict) else 0)
        )
        if changed:
            post_validation = WikiValidator(db_path=self.db_path).validate_all(check_storage=check_storage)
            post_artifact_uri = self._write_validation_artifact(post_validation)
            post_run_id = self.store.add_validation_run(
                report=post_validation,
                artifact_uri=post_artifact_uri,
            )
            post_validation["stored_run_id"] = post_run_id
            post_validation["artifact_uri"] = post_artifact_uri

        index_result: dict[str, Any] = {}
        if generate_indices:
            index_result = WikiIndexGenerator(db_path=self.db_path).generate_all(upload=upload_indices)

        return {
            "ok": bool(report.get("ok")),
            "run_id": run_id,
            "artifact_uri": artifact_uri,
            "validation": report,
            "planner": planner_result,
            "repair_task_ids": repair_task_ids,
            "repair_task_count": len(repair_task_ids),
            "deterministic_repairs": repair_result,
            "llm_repairs": llm_repair_result,
            "query_insights": query_insight_result,
            "candidates": candidate_result,
            "web_sources": web_source_result,
            "post_validation": post_validation,
            "indices": index_result,
        }

    def _write_validation_artifact(self, report: dict[str, Any]) -> str:
        path = self.layout.query_artifact_path(
            "validation_reports",
            str(report.get("run_id") or "validation"),
            ".json",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        path.write_text(payload, encoding="utf-8")
        return self.storage.upload_text(
            self.storage.key_for_local_path(path),
            payload,
            content_type="application/json; charset=utf-8",
        )

    def _create_repair_tasks_from_plan(
        self,
        run_id: str,
        planner_result: dict[str, Any],
    ) -> list[str]:
        task_ids: list[str] = []
        for task in planner_result.get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            repair_target = str(task.get("repair_target") or "")
            if not repair_target or repair_target == "none":
                continue
            task_ids.append(
                self.store.add_repair_task(
                    validation_run_id=run_id,
                    task_type=str(task.get("task_type") or "validation_error"),
                    repair_target=repair_target,
                    target_entity_type=str(task.get("target_entity_type") or ""),
                    target_entity_id=str(task.get("target_entity_id") or ""),
                    payload=task,
                )
            )
        return task_ids
