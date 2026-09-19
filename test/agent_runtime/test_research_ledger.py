from system.agent_runtime.research_ledger import ResearchTaskLedgerStore
from system.wiki.wiki_chat import AgentToolCall, WikiChatService


def _card(card_id: str, title: str, summary: str):
    return {
        "id": card_id,
        "title": title,
        "summary": summary,
        "related_topics": [],
        "content_json": {},
    }


def _receipt(dimension: str, evidence_id: str, *, novelty: str = "supporting"):
    return {
        "covered_dimensions": [dimension],
        "claims": [{"statement": f"claim about {dimension}", "evidence_ids": [evidence_id]}],
        "experimental_settings": {},
        "novelty": novelty,
        "conflicts": [],
        "open_questions": [],
        "needs_follow_up": False,
    }


def test_research_ledger_survives_reopen_and_requires_verified_reading_receipts(tmp_path):
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
    assert task["phase"] == "BUILD_QUEUE"
    assert task["corpus_ready"] is True
    assert task["verified_count"] == 2

    batch = store.next_batch(task["id"], cards, batch_size=2)
    assert batch["assigned_card_ids"] == ["paper-q", "paper-o"]

    store.record_tool_observation(
        task["id"], tool_name="wiki_open", status="done", items=[{"card_id": "paper-q"}]
    )
    task = store.reconcile(task["id"], cards)
    assert task["phase"] == "READING"
    assert task["completed_count"] == 0

    store.submit_reading(
        task["id"], batch["id"], "paper-q", _receipt("quantization", "ev-q"),
        allowed_evidence_ids=["ev-q"],
    )
    store.submit_reading(
        task["id"], batch["id"], "paper-o", _receipt("offloading", "ev-o"),
        allowed_evidence_ids=["ev-o"],
    )
    task = store.reconcile(task["id"], cards)
    assert task["phase"] == "SYNTHESIZE"
    assert task["completed_card_ids"] == ["paper-q", "paper-o"]

    reopened = ResearchTaskLedgerStore(str(db_path)).get_active_for_session("session-1")
    assert reopened is not None
    assert reopened["opened_card_ids"] == ["paper-q"]
    assert reopened["completed_card_ids"] == ["paper-q", "paper-o"]
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
    assert task["remaining_requirements"] == ["corpus coverage below minimum 2: eviction"]


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
    assert ledger.get_task(task["id"])["phase"] == "BUILD_QUEUE"

    batch = ledger.next_batch(task["id"], cards, batch_size=1)
    ledger.record_tool_observation(
        task["id"], tool_name="wiki_open", status="done", items=[{"card_id": "paper-1"}]
    )
    ledger.reconcile(task["id"], cards)
    service._finalize_research_task_from_answer("<!-- PAPERWIKI_STATUS: DONE -->")
    assert ledger.get_task(task["id"])["phase"] == "READING"

    ledger.submit_reading(
        task["id"], batch["id"], "paper-1", _receipt("eviction", "ev-1"),
        allowed_evidence_ids=["ev-1"],
    )
    ledger.reconcile(task["id"], cards)
    service._finalize_research_task_from_answer("<!-- PAPERWIKI_STATUS: DONE -->")
    assert ledger.get_task(task["id"])["phase"] == "COMPLETE"


def test_chat_tools_require_receipt_and_reuse_completed_reading(tmp_path, monkeypatch):
    cards = [{
        **_card("paper-1", "KV eviction", "eviction"),
        "page_type": "PaperPage",
        "markdown_path": "",
        "content_json": {"claims": [{"statement": "eviction works", "evidence_ids": ["ev-1"]}]},
    }]

    class Wiki:
        @staticmethod
        def list_cards(**kwargs):
            return cards

        @staticmethod
        def get_card(card_id):
            return cards[0] if card_id == "paper-1" else None

        @staticmethod
        def list_linked_pages(card_id, limit=12):
            return []

    ledger = ResearchTaskLedgerStore(str(tmp_path / "ledger.db"))
    task = ledger.ensure_task(
        project_id="project-1", session_id="session-1", task_key="study",
        target_papers=1, required_topics={"eviction": ["eviction"]},
        initial_card_ids=["paper-1"],
    )
    ledger.reconcile(task["id"], cards)
    batch = ledger.next_batch(task["id"], cards, batch_size=1)
    service = WikiChatService(Wiki(), wiki_resolver=object(), research_ledger=ledger)
    monkeypatch.setattr(service, "_active_research_task", lambda: ledger.get_task(task["id"]) or {})

    policy_calls = service._apply_query_tool_policy(
        [AgentToolCall("wiki_open", {})], message="继续研究", effective_query="继续研究",
        observations=[], limit=4,
    )
    assert policy_calls[0].name == "wiki_open"
    assert policy_calls[0].arguments["card_ids"] == ["paper-1"]

    opened = service._execute_agent_tool_call(
        AgentToolCall("wiki_open", {"card_ids": ["paper-1"]}), [], [], [], 4,
    )
    assert opened.status == "done"
    assert ledger.get_task(task["id"])["completed_count"] == 0

    submitted = service._execute_agent_tool_call(
        AgentToolCall("submit_paper_reading", {
            "batch_id": batch["id"], "card_id": "paper-1",
            "receipt": _receipt("eviction", "ev-1"),
        }), [], [], [], 4,
    )
    assert submitted.status == "done"
    assert ledger.get_task(task["id"])["phase"] == "SYNTHESIZE"

    cached = service._execute_agent_tool_call(
        AgentToolCall("wiki_open", {"card_ids": ["paper-1"]}), [], [], [], 4,
    )
    assert cached.status == "done"
    assert "existing reading receipt" in cached.summary


def test_thirty_paper_batches_are_unique_recoverable_and_gate_synthesis(tmp_path):
    db_path = tmp_path / "ledger.db"
    topics = ["planning", "tools", "memory", "runtime", "evaluation"]
    cards = []
    card_topic = {}
    for index in range(30):
        topic = topics[index % len(topics)]
        card_id = f"paper-{index:02d}"
        cards.append(_card(card_id, f"Paper {index}: {topic}", topic))
        card_topic[card_id] = topic

    store = ResearchTaskLedgerStore(str(db_path))
    task = store.ensure_task(
        project_id="project-1", session_id="session-1", task_key="thirty-paper-study",
        target_papers=30, topic_minimum=3,
        required_topics={topic: [topic] for topic in topics},
        initial_card_ids=[card["id"] for card in cards],
    )
    task = store.reconcile(task["id"], cards)
    assert task["phase"] == "BUILD_QUEUE"

    assigned = []
    while task["phase"] == "BUILD_QUEUE":
        batch = store.next_batch(task["id"], cards, batch_size=4)
        assigned.extend(batch["assigned_card_ids"])
        for card_id in batch["assigned_card_ids"]:
            evidence_id = f"ev-{card_id}"
            store.submit_reading(
                task["id"], batch["id"], card_id,
                _receipt(card_topic[card_id], evidence_id, novelty="supporting"),
                allowed_evidence_ids=[evidence_id],
            )
        # Reconstruct the store repeatedly to prove that progress is in SQLite,
        # not in Python objects or the model context.
        store = ResearchTaskLedgerStore(str(db_path))
        task = store.reconcile(task["id"], cards)

    assert task["phase"] == "SYNTHESIZE"
    assert task["completed_count"] == 30
    assert len(assigned) == len(set(assigned)) == 30
    assert all(len(task["coverage"][topic]) >= 3 for topic in topics)
    assert task["gate_status"]["saturation"]["saturated"] is True
