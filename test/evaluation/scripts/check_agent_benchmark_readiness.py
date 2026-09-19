"""Fail fast when a benchmark would measure a missing runtime capability."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DATASET = REPO_ROOT / "test" / "evaluation" / "datasets" / "agent_benchmark_v1"
DEFAULT_WORKSPACES = REPO_ROOT / "test" / "evaluation" / "workspaces" / "paperwiki_agent_eval_v1"


def runtime_tool_names() -> set[str]:
    from system.wiki.wiki_chat import WikiChatService

    return {
        str(item.get("function", {}).get("name") or "")
        for item in WikiChatService._native_tool_specs()
        if isinstance(item, dict)
    }


def readiness(dataset_dir: Path, workspace_base: Path) -> dict:
    scenarios = json.loads((dataset_dir / "scenarios.json").read_text(encoding="utf-8"))["scenarios"]
    available = runtime_tool_names()
    required = {tool for item in scenarios for tool in item.get("required_tools") or []}
    task1_root = workspace_base / "agentic_rl"
    lock_path = task1_root / "sources" / "papers" / "corpus.lock.json"
    db_path = task1_root / "paperwiki_agent_eval_v1.db"
    paper_pages = 0
    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            paper_pages = conn.execute(
                "SELECT COUNT(*) FROM wiki_pages WHERE page_type='PaperPage'"
            ).fetchone()[0]
    corpus_count = 0
    if lock_path.exists():
        corpus_count = int(json.loads(lock_path.read_text(encoding="utf-8")).get("paper_count") or 0)
    missing_tools = sorted(required - available)
    workspace_modes = {}
    for task_id in ("agentic_rl", "kv_cache", "agent_harness"):
        descriptor_path = workspace_base / task_id / "workspace.json"
        descriptor = (
            json.loads(descriptor_path.read_text(encoding="utf-8"))
            if descriptor_path.exists() else {}
        )
        workspace_modes[task_id] = str(descriptor.get("storage_backend") or "")
    runner_path = REPO_ROOT / "test" / "evaluation" / "scripts" / "run_agent_benchmark.py"
    gates = {
        "task1_corpus_locked_at_30": corpus_count == 30,
        "task1_all_30_paper_pages_compiled": paper_pages == 30,
        "runtime_exposes_all_scenario_tools": not missing_tools,
        "all_task_workspaces_are_local": all(mode == "local" for mode in workspace_modes.values()),
        "long_horizon_runner_present": runner_path.is_file(),
    }
    return {
        "ready": all(gates.values()),
        "gates": gates,
        "available_runtime_tools": sorted(available),
        "missing_runtime_tools": missing_tools,
        "task1_corpus_count": corpus_count,
        "task1_paper_page_count": paper_pages,
        "workspace_storage_backends": workspace_modes,
        "runner_path": str(runner_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--workspace-base", type=Path, default=DEFAULT_WORKSPACES)
    args = parser.parse_args()
    result = readiness(args.dataset_dir.resolve(), args.workspace_base.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
