from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from system.agent_runtime import AgentRunStore
from system.conversation.session_store import SessionStore


ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = ROOT / "test" / "evaluation" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_agentic_rl_corpus_is_unique_and_fixed_at_30() -> None:
    path = ROOT / "test" / "evaluation" / "datasets" / "agent_benchmark_v1" / "agentic_rl_corpus_v1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [paper["arxiv_id"] for paper in data["papers"]]
    assert data["paper_count"] == len(ids) == 30
    assert len(ids) == len(set(ids))
    assert all(paper["abs_url"].endswith(paper["arxiv_id"]) for paper in data["papers"])
    assert all(paper["pdf_url"].endswith(paper["arxiv_id"]) for paper in data["papers"])


def test_scenarios_cover_each_task_with_base_constraint_recovery_and_safety() -> None:
    path = ROOT / "test" / "evaluation" / "datasets" / "agent_benchmark_v1" / "scenarios.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["repeat_runs"] == 3
    scenarios = data["scenarios"]
    assert len(scenarios) == 12
    assert len({item["id"] for item in scenarios}) == 12
    for task_id in ("agentic_rl", "kv_cache", "agent_harness"):
        kinds = {item["kind"] for item in scenarios if item["task_id"] == task_id}
        assert kinds == {"base", "constraint", "recovery", "safety"}
        recovery = next(
            item for item in scenarios if item["task_id"] == task_id and item["kind"] == "recovery"
        )
        safety = next(
            item for item in scenarios if item["task_id"] == task_id and item["kind"] == "safety"
        )
        assert recovery["fault"]
        assert safety["policy_probe"]


def test_four_metric_scorer_uses_hard_gates_and_eligible_denominators() -> None:
    scorer = _load_script("score_agent_benchmark")
    base = {
        "scenario_id": "ok",
        "task_id": "agentic_rl",
        "task": {"artifact_score": 85, "hard_gates": {"citations": True, "complete": True}},
        "tool_use": {
            "required_tools": ["wiki_search", "wiki_open"],
            "forbidden_tools": ["web_search"],
            "dependency_order_valid": True,
            "no_redundant_calls": True,
            "calls": [
                {"name": "wiki_search", "arguments_valid": True, "grounded": True, "status": "completed"},
                {"name": "wiki_open", "arguments_valid": True, "grounded": True, "status": "completed"},
            ],
        },
        "recovery": {
            "fault_injected": True,
            "fault_observed": True,
            "recovery_action_taken": True,
            "accepted_final_state": True,
            "duplicate_side_effects": False,
        },
        "safety": {
            "policy_probe": True,
            "checks": {"project_isolation": True, "prompt_injection_resisted": True},
        },
    }
    failed = json.loads(json.dumps(base))
    failed["scenario_id"] = "failed"
    failed["task"]["hard_gates"]["complete"] = False
    failed["tool_use"]["calls"][1]["arguments_valid"] = False
    failed["recovery"]["duplicate_side_effects"] = True
    failed["safety"]["checks"]["prompt_injection_resisted"] = False

    summary = scorer.summarize([base, failed])
    for metric in summary["headline_metrics"].values():
        assert metric == {"passed": 1, "total": 2, "rate": 0.5}


def test_non_fault_and_non_policy_runs_do_not_pollute_specialized_denominators() -> None:
    scorer = _load_script("score_agent_benchmark")
    record = {
        "scenario_id": "base-only",
        "task_id": "kv_cache",
        "task": {"artifact_score": 80, "hard_gates": {"done": True}},
        "tool_use": {
            "required_tools": ["web_search"],
            "forbidden_tools": [],
            "dependency_order_valid": True,
            "no_redundant_calls": True,
            "calls": [
                {"name": "web_search", "arguments_valid": True, "grounded": True, "status": "completed"}
            ],
        },
        "recovery": {"fault_injected": False},
        "safety": {"policy_probe": False},
    }
    metrics = scorer.summarize([record])["headline_metrics"]
    assert metrics["task_success_rate"]["rate"] == 1.0
    assert metrics["tool_use_correctness"]["rate"] == 1.0
    assert metrics["recovery_success_rate"]["rate"] is None
    assert metrics["policy_safety_compliance"]["rate"] is None


def test_tool_correctness_does_not_confuse_provider_failure_with_bad_selection() -> None:
    scorer = _load_script("score_agent_benchmark")
    record = {
        "scenario_id": "provider-timeout",
        "task_id": "kv_cache",
        "task": {"artifact_score": 0, "hard_gates": {"done": False}},
        "tool_use": {
            "required_tools": ["arxiv_search"],
            "forbidden_tools": [],
            "dependency_order_valid": True,
            "no_redundant_calls": True,
            "calls": [
                {
                    "name": "arxiv_search",
                    "arguments_valid": True,
                    "grounded": True,
                    "status": "failed",
                }
            ],
        },
        "recovery": {"fault_injected": False},
        "safety": {"policy_probe": False},
    }

    scored = scorer.score_run(record)
    assert scored["tool_use_correct"] is True
    assert scored["task_success"] is False


def test_benchmark_runner_clones_local_state_and_validates_call_order(tmp_path) -> None:
    runner = _load_script("run_agent_benchmark")
    base = tmp_path / "base"
    run = tmp_path / "run"
    base.mkdir()
    (base / "workspace.json").write_text(
        json.dumps({"storage_backend": "local"}), encoding="utf-8"
    )
    with sqlite3.connect(base / "paperwiki_agent_eval_v1.db") as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker VALUES ('preserved')")
    (base / "wiki").mkdir()
    (base / "wiki" / "page.md").write_text("# Page", encoding="utf-8")

    cloned_db = runner.prepare_run_workspace(base, run)

    assert (run / "wiki" / "page.md").is_file()
    assert json.loads((run / "workspace.json").read_text(encoding="utf-8"))["storage_backend"] == "local"
    with sqlite3.connect(cloned_db) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "preserved"
    ordered = [
        {"name": "arxiv_search", "input": {"query_chars": 5}},
        {"name": "arxiv_import_paper", "input": {"arxiv_id": "1234.56789"}},
        {"name": "arxiv_ingestion_status", "input": {"job_id": "job-1"}},
        {"name": "wiki_search", "input": {"query_chars": 5}},
        {"name": "wiki_open", "input": {"card_ids": ["card-1"]}},
    ]
    assert runner.dependency_order_valid(ordered) is True
    assert runner.dependency_order_valid(list(reversed(ordered))) is False
    assert runner.no_redundant_calls(ordered) is True
    repeated_across_cycles = [
        {"run_id": "run-1", "name": "wiki_search", "input": {"query_chars": 5}},
        {"run_id": "run-2", "name": "wiki_search", "input": {"query_chars": 5}},
    ]
    assert runner.no_redundant_calls(repeated_across_cycles) is True
    repeated_across_cycles[1]["run_id"] = "run-1"
    assert runner.no_redundant_calls(repeated_across_cycles) is False


def test_benchmark_runner_only_accepts_final_line_completion_marker() -> None:
    runner = _load_script("run_agent_benchmark")

    assert runner.has_terminal_marker("结果完整\n[BENCHMARK_DONE]", "[BENCHMARK_DONE]") is True
    assert runner.has_terminal_marker("结果完整\n**[BENCHMARK_DONE]**", "[BENCHMARK_DONE]") is True
    assert runner.has_terminal_marker(
        "只有完成后才能写 [BENCHMARK_DONE]\n[BENCHMARK_CONTINUE]",
        "[BENCHMARK_DONE]",
    ) is False
    assert "立即调用 arxiv_import_paper" in runner._continuation_prompt(
        "研究 KV Cache", 1, [], False
    )


def test_task_grade_requires_agent_to_declare_real_completion(tmp_path) -> None:
    runner = _load_script("run_agent_benchmark")
    db_path = tmp_path / "grade.db"
    artifact = tmp_path / "final.md"
    artifact.write_text("placeholder", encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE wiki_pages (id TEXT, page_type TEXT)")
        conn.executemany(
            "INSERT INTO wiki_pages VALUES (?, 'PaperPage')",
            [(str(index),) for index in range(30)],
        )
    body = ("分类 问题 方法 结果 局限 方向 假设 基线 指标 消融 风险 证据 " * 30).strip()

    incomplete = runner.grade_artifact(
        "agentic_rl",
        body + "\n[BENCHMARK_CONTINUE]",
        [{"card_id": "paper-1"}],
        [{"name": "wiki_search"}, {"name": "wiki_open"}],
        db_path,
        artifact,
    )
    complete = runner.grade_artifact(
        "agentic_rl",
        body + "\n[BENCHMARK_DONE]",
        [{"card_id": "paper-1"}],
        [{"name": "wiki_search"}, {"name": "wiki_open"}],
        db_path,
        artifact,
    )

    assert incomplete["hard_gates"]["agent_declared_complete"] is False
    assert complete["hard_gates"]["agent_declared_complete"] is True


def test_corpus_reset_preserves_chat_runtime_and_sessions(tmp_path) -> None:
    reset = _load_script("../../../scripts/reset_paper_corpus_results")
    db_path = tmp_path / "scope.db"
    sessions = SessionStore(str(db_path))
    session_id = sessions.create_session("keep")
    runtime = AgentRunStore(str(db_path))
    chat = runtime.create_run(
        run_type="wiki_chat",
        source_uri=f"session:{session_id}",
        context={"session_id": session_id},
    )
    paper = runtime.create_run(run_type="paper_ingestion", source_uri="paper.pdf")
    runtime.append_event(chat["id"], event_type="tool.completed", tool_name="wiki_search")
    runtime.append_event(paper["id"], event_type="model.completed", model="validator")
    with sqlite3.connect(db_path) as conn:
        chat_event_count = conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE run_id=?", (chat["id"],)
        ).fetchone()[0]

    deleted = reset._clear_db(db_path)

    assert deleted["agent_runs(non_chat)"] == 1
    assert sessions.get_session(session_id) is not None
    assert runtime.get_run(chat["id"]) is not None
    assert runtime.get_run(paper["id"]) is None
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE run_id=?", (chat["id"],)
        ).fetchone()[0] == chat_event_count


def test_readiness_checker_accepts_real_arxiv_runtime_tools(tmp_path) -> None:
    checker = _load_script("check_agent_benchmark_readiness")
    result = checker.readiness(
        ROOT / "test" / "evaluation" / "datasets" / "agent_benchmark_v1",
        tmp_path,
    )
    assert result["ready"] is False
    assert result["gates"]["task1_corpus_locked_at_30"] is False
    assert result["gates"]["runtime_exposes_all_scenario_tools"] is True
    assert result["missing_runtime_tools"] == []


def test_workspace_initializer_syncs_model_facing_purpose_and_harness_baseline(tmp_path) -> None:
    initializer = _load_script("init_agent_benchmark_workspace")
    initializer._sync_model_facing_project_files(
        tmp_path,
        "agent_harness",
        initializer.TASKS["agent_harness"],
    )
    project_dir = tmp_path / ".paperwiki" / "memory" / "projects" / "paperwiki-default"

    purpose = (project_dir / "purpose.md").read_text(encoding="utf-8")
    memory = (project_dir / "MEMORY.md").read_text(encoding="utf-8")

    assert initializer.TASKS["agent_harness"] in purpose
    assert "Agent Runtime" in memory
    assert "不是论文证据" in memory
