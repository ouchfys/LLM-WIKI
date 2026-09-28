import json
import sqlite3
import uuid

import pytest

from system.agent_runtime.task_plans import TaskPlanStore


STEPS = [{"step": "Read both papers", "status": "in_progress"},
         {"step": "Compare the findings", "status": "pending"}]
PLAN = """# Goal
Read the supplied papers and compare their approaches.
# Sources / Objects
- [ ] paper-a
# Steps
- [x] Open paper-a
- [ ] Compare them
# Done When
- Both papers have tool observations.
# Progress
- Opened one paper.
# Blocked / Open Questions
- None.
"""


def test_structured_plan_survives_reopen_without_task_files(tmp_path):
    root = tmp_path / "tasks"
    store = TaskPlanStore(root)
    created = store.write(steps=STEPS, goal="Compare papers", project_id="p", session_id="s")
    reopened = TaskPlanStore(root).read(project_id="p", session_id="s")
    assert reopened == created
    assert reopened["steps"] == STEPS
    assert reopened["storage"] == "session_database"
    assert "path" not in reopened and "markdown" not in reopened
    assert not root.exists()
    with pytest.raises(ValueError, match="current project"):
        store.read(task_id=created["task_id"], project_id="other", session_id="s")


def test_step_updates_reuse_plan_and_derive_completion(tmp_path):
    store = TaskPlanStore(tmp_path / "tasks")
    first = store.write(steps=STEPS, goal="Compare papers", project_id="p", session_id="s")
    second = store.write(steps=[{**s, "status": "completed"} for s in STEPS],
                         project_id="p", session_id="s", task_id=first["task_id"])
    assert first["task_id"] == second["task_id"]
    assert second["version"] == 2 and second["status"] == "completed"
    assert second["goal"] == "Compare papers"
    assert store.current_task_id(project_id="p", session_id="s") == ""
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM task_plans").fetchone()[0] == 1


@pytest.mark.parametrize("steps", [[], "read a paper", [{"step": "", "status": "pending"}],
                                   [{"step": "Read", "status": "invented"}]])
def test_invalid_steps_do_not_create_a_plan(tmp_path, steps):
    store = TaskPlanStore(tmp_path / "tasks")
    with pytest.raises(ValueError):
        store.write(steps=steps, project_id="p", session_id="s")
    assert store.list_active(project_id="p") == []


def test_new_conversation_does_not_receive_unrelated_plans(tmp_path):
    store = TaskPlanStore(tmp_path / "tasks")
    created = store.write(steps=STEPS, goal="Private previous task", project_id="p", session_id="old")
    context = store.prompt_context(project_id="p", session_id="new")
    assert created["task_id"] not in context and "Private previous task" not in context
    assert store.current_task_id(project_id="p", session_id="new") == ""
    # Explicit lookup is still available when a user asks to continue old work.
    assert store.read(project_id="p", session_id="new")["active_tasks"][0]["task_id"] == created["task_id"]


def test_reference_read_does_not_attach_but_explicit_resume_does(tmp_path):
    store = TaskPlanStore(tmp_path / "tasks")
    created = store.write(steps=STEPS, project_id="p", session_id="old")
    args = dict(task_id=created["task_id"], project_id="p", session_id="new")
    for _ in range(2):
        item = store.read(**args)
        assert item["plan_scope"] == "reference" and item["version"] == 1
    assert store.current_task_id(project_id="p", session_id="new") == ""
    attached = store.attach(**args)
    assert attached["attached_to_session"]
    assert store.current_task_id(project_id="p", session_id="new") == created["task_id"]


def test_cache_token_tracks_updates_and_project_index(tmp_path):
    store = TaskPlanStore(tmp_path / "tasks")
    args = dict(project_id="p", session_id="new")
    empty = store.cache_token(**args)
    store.write(steps=STEPS, project_id="other", session_id="old")
    assert store.cache_token(**args) == empty
    item = store.write(steps=STEPS, project_id="p", session_id="old")
    assert store.cache_token(**args) != empty
    current = dict(project_id="p", session_id="old", task_id=item["task_id"])
    before = store.cache_token(**current)
    store.read(**current)
    assert store.cache_token(**current) == before
    store.write(steps=[{"step": "Done", "status": "completed"}], **current)
    assert store.cache_token(**current) != before
    assert store.cache_token(**args) == empty


def test_separate_tasks_remain_distinct_in_same_conversation(tmp_path):
    store = TaskPlanStore(tmp_path / "tasks")
    first = store.write(steps=STEPS, project_id="p", session_id="s")
    second = store.write(steps=STEPS, project_id="p", session_id="s", create_new=True)
    assert first["task_id"] != second["task_id"]
    store.write(steps=[{"step": "Done", "status": "completed"}], project_id="p",
                session_id="s", task_id=second["task_id"])
    assert store.current_task_id(project_id="p", session_id="s") == first["task_id"]


def test_legacy_import_is_lossless_idempotent_and_not_resurrected(tmp_path):
    root = tmp_path / "tasks"
    task_id = str(uuid.uuid4())
    folder = root / task_id
    folder.mkdir(parents=True)
    (folder / "PLAN.md").write_text(PLAN, encoding="utf-8")
    (folder / "metadata.json").write_text(json.dumps({
        "task_id": task_id, "project_id": "p", "session_ids": ["s"], "version": 3,
        "status": "active", "reason": "old progress",
    }), encoding="utf-8")
    store = TaskPlanStore(root)
    item = store.read(task_id=task_id, project_id="p", session_id="s")
    assert item["markdown"] == PLAN and item["version"] == 3
    assert item["steps"] == [{"step": "Open paper-a", "status": "completed"},
                             {"step": "Compare them", "status": "pending"}]
    assert TaskPlanStore(root).read(task_id=task_id, project_id="p", session_id="s") == item
    store.detach_sessions(["s"])
    restarted = TaskPlanStore(root)
    with pytest.raises(ValueError, match="not found"):
        restarted.read(task_id=task_id, project_id="p", session_id="s")
    assert (folder / "PLAN.md").read_text(encoding="utf-8") == PLAN


def test_legacy_interrupted_call_writes_database_only(tmp_path):
    root = tmp_path / "tasks"
    store = TaskPlanStore(root)
    item = store.write(PLAN, project_id="p", session_id="s", status="active")
    assert item["markdown"] == PLAN and not root.exists()
    updated = store.write(steps=STEPS, task_id=item["task_id"], project_id="p", session_id="s")
    assert "markdown" not in updated


def test_plan_detach_rolls_back_with_session_transaction(tmp_path):
    store = TaskPlanStore(tmp_path / "tasks")
    item = store.write(steps=STEPS, project_id="p", session_id="s")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        store.detach_sessions(["s"], conn=conn)
        conn.rollback()
    assert store.read(project_id="p", session_id="s")["task_id"] == item["task_id"]
