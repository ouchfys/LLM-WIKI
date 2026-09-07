"""Isolated manual UI fixture (PowerShell, from repository root):

$env:PYTHONPATH=(Get-Location).Path; python test/wiki/control_smoke_server.py

Use VITE_API_TARGET=http://127.0.0.1:8021 with a separate Vite instance.
No real LLM, OSS, Wiki content, or production database is touched.
"""
import tempfile
import time
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI

from backend.api import wiki, agent_runs
from backend.deps import get_session_store, get_wiki_chat, get_wiki_store
from system.agent_runtime import AgentRunStore
from system.memory.session_store import SessionStore
from system.wiki.wiki_chat import WikiChatService, WikiToolPlan


class SlowModel:
    model = "ui-test-only"

    def invoke(self, prompt, **kwargs):
        time.sleep(12)
        return "planning finished"

    def stream_invoke(self, prompt, **kwargs):
        for word in ["根据最新要求：", prompt, "。", "这是隔离测试回答，", "不会访问真实模型或知识库。"]:
            time.sleep(1)
            yield word


class DemoChat(WikiChatService):
    def _run_tool_loop(self, message, *args, **kwargs):
        self._invoke_llm(message, temperature=0, max_tokens=10)
        return {"plan": WikiToolPlan(), "cards": [], "web_results": [], "resources": [], "trace": {}}

    def _build_prompt(self, message, *args, **kwargs):
        return message

    def _update_profile_from_message(self, *args):
        return []

    def _save_turn(self, session_id, message, answer, *args):
        self.session_store.save_message(session_id, "user", message)
        self.session_store.save_message(session_id, "assistant", answer)


def main():
    with tempfile.TemporaryDirectory(prefix="wiki-control-ui-") as directory:
        db_path = directory + "/test.db"
        sessions = SessionStore(db_path)
        runtime = AgentRunStore(db_path)
        store = SimpleNamespace(db_path=db_path)
        chat = DemoChat(
            wiki_store=store, wiki_resolver=object(), llm=SlowModel(),
            runtime=runtime, session_store=sessions,
        )
        app = FastAPI()

        @app.post("/api/wiki/maintenance/query-insights/capture-conversation")
        def save_insight(payload: wiki.ConversationWikiPayload):
            sessions.save_message(payload.session_id, "user", "/wiki " + payload.instruction)
            answer = "测试：已执行排队的对话沉淀（未写真实 Wiki）。"
            sessions.save_message(payload.session_id, "assistant", answer)
            return {"ok": True, "answer": answer}

        @app.post("/api/wiki/sessions/{session_id}/compact")
        def compact(session_id: str):
            return wiki.compact_chat_session(
                session_id, wiki.SessionCompactPayload(use_llm=False),
                session_store=sessions, wiki_store=store,
            )

        app.include_router(wiki.router, prefix="/api/wiki")
        app.include_router(agent_runs.router, prefix="/api/agent-runs")
        app.dependency_overrides[get_session_store] = lambda: sessions
        app.dependency_overrides[get_wiki_store] = lambda: store
        app.dependency_overrides[get_wiki_chat] = lambda: chat
        uvicorn.run(app, host="127.0.0.1", port=8021)


if __name__ == "__main__":
    main()
