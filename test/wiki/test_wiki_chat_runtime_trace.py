from __future__ import annotations
from system.conversation.context_budget import ContextBudget, ContextPolicy

import tempfile
from pathlib import Path

import pytest

from system.agent_runtime import AgentRunStore
from system.wiki.wiki_chat import WikiChatService


class _WikiStore:
    def get_card(self, card_id: str):
        if card_id != "paper-1":
            return None
        return {
            "id": "paper-1",
            "title": "Tree of Thoughts",
            "page_type": "PaperPage",
            "summary": "A deliberate search method for language-model reasoning.",
            "markdown_path": "",
            "content_json": {"overview": "Tree of Thoughts explores multiple reasoning paths."},
        }

    def list_linked_pages(self, card_id: str, limit: int = 12):
        return []


class _Resolver:
    def resolve(self, query: str, limit: int = 6):
        return [{
            "card_id": "paper-1",
            "title": "Tree of Thoughts",
            "page_type": "PaperPage",
            "summary": "A deliberate search method.",
            "score": 1.0,
            "match_reason": "multilingual_vector",
        }]


class _BrokenResolver:
    def resolve(self, query: str, limit: int = 6):
        raise RuntimeError("resolver unavailable")


class _FakeLLM:
    model = "fake-chat-model"

    def __init__(self, first_call=None, answer=None):
        self.first_call = first_call or '{"tool_calls": [{"name": "wiki_open", "arguments": {"card_ids": ["paper-1"]}}]}'
        self.answer = answer or "Tree of Thoughts 会搜索并评估多条候选推理路径。"
        self.controller_prompts = []

    def invoke(self, prompt: str, **kwargs) -> str:
        if "tool-use controller" in prompt:
            self.controller_prompts.append(prompt)
            if len(self.controller_prompts) == 1:
                return self.first_call
            return '{"tool_calls": []}'
        return self.answer


class _TraceChatService(WikiChatService):
    def _load_history(self, session_id):
        return [("上一轮问题", "上一轮回答")]

    def _update_profile_from_message(self, *args, **kwargs):
        return []

    def _save_turn(self, *args, **kwargs):
        return None

    def _read_card_markdown(self, card):
        return str(card.get("content_json", {}).get("overview") or "")


def test_chat_turn_uses_runtime_event_model_and_aggregates_metrics() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        runtime = AgentRunStore(db_path=str(Path(tmp) / "chat-trace.db"))
        model = _FakeLLM()
        service = _TraceChatService(
            wiki_store=_WikiStore(),
            wiki_resolver=_Resolver(),
            llm=model,
            context_budget=ContextBudget(ContextPolicy()),
            runtime=runtime,
        )

        result = service.chat("思维树是怎么工作的？", session_id="session-1")

        assert len(model.controller_prompts) == 2
        run = runtime.list_runs(limit=1)[0]
        assert run["run_type"] == "wiki_chat"
        assert run["current_state"] == "COMPLETED"
        events = runtime.list_events(str(run["id"]))
        assert any(item["event_type"] == "model.completed" for item in events)
        assert {item["tool_name"] for item in events if item["event_type"] == "tool.completed"} == {"wiki_open"}
        summary = runtime.summarize_trace(str(run["id"]))
        assert summary["model_calls"] >= 2
        assert summary["tool_calls"] >= 1
        assert summary["token_usage"]["total_tokens"] > 0
        assert summary["token_usage"]["contains_estimates"] is True
        assert result.trace["runtime"]["run_id"] == run["id"]
        assert result.trace["runtime"]["current_state"] == "COMPLETED"


def test_unhandled_resolver_failure_marks_run_and_tool_span_failed() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        runtime = AgentRunStore(db_path=str(Path(tmp) / "chat-failure.db"))
        model = _FakeLLM(
            first_call='{"tool_calls": [{"name": "wiki_open", "arguments": {"query": "missing source"}}]}',
            answer="本次读取 Wiki 失败，暂时无法依据卡片回答。",
        )
        service = _TraceChatService(
            wiki_store=_WikiStore(),
            wiki_resolver=_BrokenResolver(),
            llm=model,
            context_budget=ContextBudget(ContextPolicy()),
            runtime=runtime,
        )

        with pytest.raises(RuntimeError, match="resolver unavailable"):
            service.chat("触发失败")

        assert len(model.controller_prompts) == 1
        run = runtime.list_runs(limit=1)[0]
        assert run["current_state"] == "FAILED"
        events = runtime.list_events(str(run["id"]))
        assert any(
            item["event_type"] == "tool.failed" and item["tool_name"] == "wiki_open"
            for item in events
        )


def test_command_failure_is_reported_to_model_and_turn_can_finish() -> None:
    class FailedShell:
        def run(self, command, **kwargs):
            return {"command": command, "cwd": "", "exit_code": 1,
                    "stdout": "", "stderr": "fixture command unavailable", "timed_out": False}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        runtime = AgentRunStore(db_path=str(Path(tmp) / "chat-tool-error.db"))
        model = _FakeLLM(
            first_call='{"tool_calls": [{"name": "local_shell", "arguments": {"command": "fixture-read"}}]}',
            answer="本地命令执行失败，暂时无法取得项目内容。",
        )
        service = _TraceChatService(
            wiki_store=_WikiStore(), wiki_resolver=_Resolver(), llm=model,
            local_shell=FailedShell(), context_budget=ContextBudget(ContextPolicy()), runtime=runtime,
        )
        result = service.chat("执行本地命令查看项目，失败后直接说明。")

        assert len(model.controller_prompts) == 2
        assert "fixture command unavailable" in model.controller_prompts[1]
        run = runtime.list_runs(limit=1)[0]
        assert run["current_state"] == "COMPLETED"
        assert result.trace["runtime"]["current_state"] == "COMPLETED"
        events = runtime.list_events(str(run["id"]))
        failures = [item for item in events if item["event_type"] == "tool.failed"]
        assert len(failures) == 1 and failures[0]["tool_name"] == "local_shell"
