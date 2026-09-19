from system.agent_runtime.research_ledger import ResearchTaskLedgerStore
from system.wiki.wiki_chat import WikiChatService


def _card(card_id: str, title: str, summary: str):
    return {
        "id": card_id,
        "title": title,
        "summary": summary,
        "related_topics": [],
        "content_json": {},
    }


def test_research_ledger_survives_reopen_and_uses_count_coverage_and_read_gates(tmp_path):
    db_path = tmp_path / "ledger.db"
    store = ResearchTaskLedgerStore(str(db_path))
    task = store.ensure_task(
        project_id="project-1",
        session_id="session-1",
        task_key="kv_cache",
        target_papers=2,
        topic_minimum=1,
        required_topics={"quantization": ["quantization"], "offloading": ["offload"]},
        budget={"max_web_calls": 2},
        initial_card_ids=["paper-q", "paper-o"],
    )
    cards = [
        _card("paper-q", "KV cache quantization", "low-bit cache"),
        _card("paper-o", "KV cache offload", "CPU migration"),
    ]

    task = store.reconcile(task["id"], cards)
    assert task["phase"] == "READ_LOCAL_CORPUS"
    assert task["corpus_ready"] is True
    assert task["verified_count"] == 2

    store.record_tool_observation(
        task["id"], tool_name="wiki_open", status="done", items=[{"card_id": "paper-q"}]
    )
    task = store.reconcile(task["id"], cards)
    assert task["phase"] == "READ_LOCAL_CORPUS"
    assert "need to open 1" in " ".join(task["remaining_requirements"])

    store.record_tool_observation(
        task["id"], tool_name="workspace_read", status="done", items=[{"card_id": "paper-o"}]
    )
    task = store.reconcile(task["id"], cards)
    assert task["phase"] == "SYNTHESIZE"

    reopened = ResearchTaskLedgerStore(str(db_path)).get_active_for_session("session-1")
    assert reopened is not None
    assert reopened["opened_card_ids"] == ["paper-q", "paper-o"]
    rebound = store.attach_session(task["id"], "session-2")
    assert rebound["session_id"] == "session-2"
    assert store.get_active_for_project("project-1")["id"] == task["id"]


def test_research_ledger_requires_multiple_papers_per_topic(tmp_path):
    store = ResearchTaskLedgerStore(str(tmp_path / "ledger.db"))
    task = store.ensure_task(
        project_id="project-1",
        session_id="session-1",
        task_key="study",
        target_papers=2,
        topic_minimum=2,
        required_topics={"eviction": ["eviction"]},
        initial_card_ids=["paper-1", "paper-2"],
    )
    task = store.reconcile(
        task["id"],
        [_card("paper-1", "KV eviction", "eviction"), _card("paper-2", "Other", "unrelated")],
    )

    assert task["verified_count"] == 2
    assert task["corpus_ready"] is False
    assert task["phase"] == "DISCOVER"
    assert task["remaining_requirements"] == ["topic coverage below minimum 2: eviction", "need to open 2 verified Wiki card(s)"]


def test_explicit_long_research_request_starts_protocol_but_ordinary_qa_does_not(tmp_path):
    class Sessions:
        @staticmethod
        def get_session_project_id(session_id):
            return "project-1"

    class Wiki:
        @staticmethod
        def list_cards(**kwargs):
            return []

    ledger = ResearchTaskLedgerStore(str(tmp_path / "ledger.db"))
    service = WikiChatService(
        Wiki(), wiki_resolver=object(), session_store=Sessions(), research_ledger=ledger
    )

    assert service._ensure_research_task_for_message("什么是 KV Cache？", "ordinary") == {}
    task = service._ensure_research_task_for_message(
        "从空白任务库自主研究 KV Cache，生成技术图谱和路线图", "research"
    )

    assert task["task_key"] == "kv_cache"
    assert task["target_papers"] == 24
    assert task["topic_minimum"] == 3
    continued = service._ensure_research_task_for_message("继续上次的研究", "research-2")
    assert continued["id"] == task["id"]
    assert continued["session_id"] == "research-2"


def test_done_marker_only_completes_a_task_after_structured_gates_pass(tmp_path, monkeypatch):
    cards = [_card("paper-1", "KV eviction", "eviction")]

    class Wiki:
        @staticmethod
        def list_cards(**kwargs):
            return cards

    ledger = ResearchTaskLedgerStore(str(tmp_path / "ledger.db"))
    task = ledger.ensure_task(
        project_id="project-1",
        session_id="session-1",
        task_key="study",
        target_papers=1,
        required_topics={"eviction": ["eviction"]},
        initial_card_ids=["paper-1"],
    )
    task = ledger.reconcile(task["id"], cards)
    service = WikiChatService(Wiki(), wiki_resolver=object(), research_ledger=ledger)
    monkeypatch.setattr(service, "_active_research_task", lambda: ledger.get_task(task["id"]) or {})

    service._finalize_research_task_from_answer("<!-- PAPERWIKI_STATUS: DONE -->")
    assert ledger.get_task(task["id"])["phase"] == "READ_LOCAL_CORPUS"

    ledger.record_tool_observation(
        task["id"], tool_name="wiki_open", status="done", items=[{"card_id": "paper-1"}]
    )
    ledger.reconcile(task["id"], cards)
    service._finalize_research_task_from_answer("<!-- PAPERWIKI_STATUS: DONE -->")
    assert ledger.get_task(task["id"])["phase"] == "COMPLETE"
