"""Reproduce the repeated-plan/page failures from a real explanation turn.

These checks use real scheduling and temporary plan files, never a model API.
"""
from collections import Counter
from copy import deepcopy

import pytest

from system.agent_runtime.control import RunControl
from system.agent_runtime.task_plans import TaskPlanStore
from system.conversation.context_budget import ContextBudget, ContextPolicy
from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService


PLAN = """# Goal
Read eleven papers and produce a research report.
# Sources / Objects
- [ ] Eleven supplied papers
# Steps
- [ ] Finish reading
# Done When
- Every supplied paper has been compared.
# Progress
- Imports are incomplete.
# Blocked / Open Questions
- None
"""
SESSION_ID = "current-session"
PROJECT_ID = "test-project"


class _Sessions:
    def __init__(self):
        self.results = []

    def get_session_project_id(self, session_id):
        return PROJECT_ID

    def save_tool_result(self, session_id, tool, arguments, payload):
        self.results.append(deepcopy(payload))
        return len(self.results)


class _Wiki:
    def __init__(self):
        self.cards = {
            key: {"id": key, "title": f"Paper {key}", "page_type": "PaperPage",
                  "summary": f"Summary {key}", "updated_at": "2026-09-24T00:00:00Z",
                  "content_json": {}, "markdown_path": ""}
            for key in ("A", "B", "C")
        }
        self.bodies = {key: f"BODY_{key}_ORIGINAL" for key in self.cards}

    def get_card(self, card_id):
        return deepcopy(self.cards.get(card_id))

    def list_linked_pages(self, card_id, limit=12):
        return []


@pytest.fixture
def chat(tmp_path, monkeypatch):
    service = WikiChatService(
        _Wiki(), wiki_resolver=object(), llm=object(), session_store=_Sessions(),
        task_plans=TaskPlanStore(tmp_path / "tasks"),
        context_budget=ContextBudget(ContextPolicy()),
    )
    service.body_reads = Counter()

    def read_body(card):
        service.body_reads[card["id"]] += 1
        return service.wiki_store.bodies[card["id"]]

    monkeypatch.setattr(service, "_read_card_markdown", read_body)
    return service


def _run(chat, controller, monkeypatch, *, max_steps=16):
    monkeypatch.setattr(chat, "_next_agent_tool_calls", controller)
    control = RunControl(None, "", "Explain agent self-improvement from existing papers.")
    control.session_id = SESSION_ID
    with control.bind():
        return chat._run_tool_loop(control.message, "", [], max_steps=max_steps)


def _old_plan(chat):
    return chat.task_plans.write(PLAN, project_id=PROJECT_ID, session_id=SESSION_ID)


def test_repeated_retired_plan_calls_receive_errors_then_feedback(chat, monkeypatch):
    old = _old_plan(chat)
    rounds = []

    def controller(**kwargs):
        observed = kwargs["observations"]
        rounds.append([item.tool for item in observed])
        if any(item.tool == "wiki_open" for item in observed):
            return []
        if any(item.tool == "runtime_call_feedback" for item in observed):
            return [AgentToolCall("wiki_open", {"card_ids": ["A"]})]
        return [AgentToolCall("task_plan_read", {"task_id": old["task_id"]})]

    result = _run(chat, controller, monkeypatch)
    tools = [item.name for item in result["plan"].tools]
    assert tools.count("task_plan_read") == 3
    rejected = [o for o in result["trace"]["tool_observations"] if o["tool"] == "task_plan_read"]
    assert all(o["status"] == "error" and "not registered" in o["summary"] for o in rejected)
    assert tools.count("wiki_open") == 1
    assert len(rounds) <= 7
    assert result["trace"]["stop_reason"] == "model_finished"
    assert not any("runtime_completion_check" in turn for turn in rounds)
    assert chat.task_plans.read(project_id=PROJECT_ID, session_id=SESSION_ID)["version"] == 1


@pytest.mark.parametrize("resume, expected_checks", [(False, 0), (True, 0)])
def test_reference_plan_does_not_activate_old_done_when(chat, monkeypatch, resume, expected_checks):
    old = _old_plan(chat)

    def controller(**kwargs):
        if kwargs["step_index"] == 0:
            return [AgentToolCall("task_plan_read", {"task_id": old["task_id"], "resume": resume})]
        return []

    result = _run(chat, controller, monkeypatch)
    observations = result["trace"]["tool_observations"]
    assert sum(item["tool"] == "runtime_completion_check" for item in observations) == expected_checks
    assert not any(item["tool"] == "task_plan_write" for item in observations)


def test_old_plan_is_not_injected_into_current_prompt(chat):
    old = _old_plan(chat)
    control = RunControl(None, "", "Explain the Wiki")
    control.session_id = SESSION_ID
    with control.bind():
        prompt = chat._tool_loop_prompt(control.message, "", [], [], 0, 6)
    assert old["task_id"] not in prompt
    assert "task_plan_read" not in prompt
    assert "Current conversation checklist" not in prompt


def test_overlapping_and_reordered_batches_reuse_page_bodies(chat, monkeypatch):
    batches = iter([["A", "B"], ["B", "C"], ["B", "A"]])

    def controller(**kwargs):
        ids = next(batches, None)
        return [AgentToolCall("wiki_open", {"card_ids": ids})] if ids else []

    result = _run(chat, controller, monkeypatch)
    assert chat.body_reads == Counter({"A": 1, "B": 1, "C": 1})
    assert {card["id"]: card["_full_text"] for card in result["cards"]} == chat.wiki_store.bodies
    assert result["trace"]["stop_reason"] == "model_finished"
    # The final controller view retains every needed page without duplicating B.
    observations = [AgentToolObservation(**{
        key: item[key] for key in ("tool", "query", "status", "summary", "items")
    }) for item in result["trace"]["tool_observations"]]
    context = chat._observation_context(observations)
    for body in chat.wiki_store.bodies.values():
        assert context.count(body) == 1


def test_changing_query_does_not_reread_the_same_full_page(chat, monkeypatch):
    queries = iter(["method", "experiments", "limitations"])

    def controller(**kwargs):
        query = next(queries, None)
        return [AgentToolCall("wiki_open", {"card_ids": ["A"], "query": query})] if query else []

    result = _run(chat, controller, monkeypatch)
    assert chat.body_reads == Counter({"A": 1})
    assert result["cards"][0]["_full_text"] == "BODY_A_ORIGINAL"


@pytest.mark.parametrize("refresh", [False, True])
def test_updated_or_explicitly_refreshed_page_replaces_cached_body(chat, monkeypatch, refresh):
    def controller(**kwargs):
        step = kwargs["step_index"]
        if step > 1:
            return []
        args = {"card_ids": ["A"]}
        if step == 1:
            chat.wiki_store.bodies["A"] = "BODY_A_REVISED"
            if refresh:
                args.update(refresh=True, reason="Recheck the corrected experiment table.")
            else:
                chat.wiki_store.cards["A"]["updated_at"] = "2026-09-25T00:00:00Z"
        return [AgentToolCall("wiki_open", args)]

    result = _run(chat, controller, monkeypatch)
    assert chat.body_reads == Counter({"A": 2})
    assert len(result["cards"]) == 1
    assert result["cards"][0]["_full_text"] == "BODY_A_REVISED"
    # An explicit/updated read is preserved in the full audit trace, too.
    assert sum(item["tool"] == "wiki_open" for item in result["trace"]["tool_observations"]) == 2


def test_context_keeps_latest_page_body_without_mutating_full_trace(chat):
    observations = [
        AgentToolObservation("wiki_open", "", "done", items=[
            {"card_id": "A", "title": "Paper A", "content": "STALE_BODY_A"},
            {"card_id": "B", "title": "Paper B", "content": "BODY_B_KEEP"},
        ], result_id=41),
        AgentToolObservation("wiki_open", "", "done", items=[
            {"card_id": "A", "title": "Paper A", "content": "CURRENT_BODY_A"},
            {"card_id": "C", "title": "Paper C", "content": "BODY_C_KEEP"},
        ], result_id=42),
    ]
    context = chat._observation_context(observations)
    assert "STALE_BODY_A" not in context
    for body in ("CURRENT_BODY_A", "BODY_B_KEEP", "BODY_C_KEEP"):
        assert context.count(body) == 1
    assert observations[0].items[0]["content"] == "STALE_BODY_A"
    assert observations[0].result_id == 41


def test_parallel_overlapping_open_calls_read_shared_page_once(chat, monkeypatch):
    def controller(**kwargs):
        if kwargs["step_index"]:
            return []
        return [
            AgentToolCall("wiki_open", {"card_ids": ["A", "B"]}),
            AgentToolCall("wiki_open", {"card_ids": ["B", "C"]}),
        ]

    result = _run(chat, controller, monkeypatch)
    assert chat.body_reads == Counter({"A": 1, "B": 1, "C": 1})
    assert {card["id"]: card["_full_text"] for card in result["cards"]} == chat.wiki_store.bodies
    opened = [item for item in result["trace"]["tool_observations"] if item["tool"] == "wiki_open"]
    assert [[card["card_id"] for card in item["items"]] for item in opened] == [["A", "B"], ["C"]]


def test_retired_plan_write_leaves_saved_historical_plan_unchanged(chat):
    old = _old_plan(chat)
    control = RunControl(None, "", "Continue")
    control.session_id = SESSION_ID
    with control.bind():
        result = chat._execute_agent_tool_call(
            AgentToolCall("task_plan_write", {"task_id": old["task_id"],
                "steps": [{"step": "overwrite", "status": "completed"}]}), [], [], [], 6)
    assert result.status == "error"
    assert "not registered" in result.summary
    assert chat.task_plans.read(project_id=PROJECT_ID, session_id=SESSION_ID)["version"] == old["version"]


def test_refresh_without_reason_is_rejected_before_reading_page(chat):
    observation = chat._execute_agent_tool_call_impl(
        AgentToolCall("wiki_open", {"card_ids": ["A"], "refresh": True}), [], [], [], 6,
    )
    assert observation.status == "error"
    assert "reason" in observation.summary
    assert chat.body_reads == Counter()
