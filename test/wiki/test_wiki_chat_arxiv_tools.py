from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService


class _Page:
    def to_dict(self):
        return {
            "papers": [
                {
                    "arxiv_id": "2309.06180",
                    "title": "Efficient Memory Management for Large Language Model Serving with PagedAttention",
                    "abs_url": "https://arxiv.org/abs/2309.06180",
                }
            ]
        }


class _ArxivClient:
    def search(self, query, **kwargs):
        assert query == "KV cache"
        assert kwargs["max_results"] == 3
        return _Page()

    def get_papers(self, arxiv_ids):
        assert arxiv_ids == ["2309.06180", "2401.00001"]
        return [{
            "arxiv_id": "2309.06180",
            "title": "Efficient Memory Management for Large Language Model Serving with PagedAttention",
            "abs_url": "https://arxiv.org/abs/2309.06180",
        }]


class _IngestionClient:
    def get_job(self, job_id):
        assert job_id == "job-1"
        return {"id": job_id, "status": "done", "stage": "done", "progress": 1.0, "paper_card_id": "paper-1"}


class _ArxivService:
    arxiv = _ArxivClient()
    ingestion = _IngestionClient()

    def import_paper(self, arxiv_id, *, approval_mode):
        assert arxiv_id == "2309.06180"
        assert approval_mode == "risk"
        return {
            "ok": True,
            "arxiv_id": arxiv_id,
            "download": {"path": "C:/secret/cache/paper.pdf"},
            "ingestion": {"job_id": "job-1", "agent_run_id": "run-1"},
            "next": "poll status",
        }


def _service():
    return WikiChatService(object(), wiki_resolver=object(), arxiv_service=_ArxivService())


def test_arxiv_search_returns_structured_paper_metadata():
    result = _service()._execute_agent_tool_call_impl(
        AgentToolCall("arxiv", {"action": "search", "query": "KV cache", "limit": 3}), [], [], [], 3
    )

    assert result.status == "done"
    assert result.items[0]["arxiv_id"] == "2309.06180"


def test_arxiv_lookup_resolves_explicit_ids_in_one_call_and_marks_missing_ids():
    result = _service()._execute_agent_tool_call_impl(
        AgentToolCall(
            "arxiv",
            {"action": "lookup", "arxiv_ids": ["2309.06180", "2401.00001"]},
        ),
        [], [], [], 8,
    )

    assert result.status == "done"
    assert result.summary == "resolved 1/2 explicit arXiv identifiers"
    assert result.items[0]["found"] is True
    assert result.items[1] == {"arxiv_id": "2401.00001", "found": False}


def test_arxiv_import_submits_async_job_without_exposing_local_cache_path():
    result = _service()._execute_agent_tool_call_impl(
        AgentToolCall("arxiv", {"action": "import", "arxiv_id": "2309.06180", "approval_mode": "risk"}), [], [], [], 3
    )

    assert result.status == "done"
    assert result.items == [{
        "arxiv_id": "2309.06180",
        "job_id": "job-1",
        "agent_run_id": "run-1",
        "already_exists": False,
        "paper_card_id": None,
        "next": "poll status",
    }]
    assert "secret" not in str(result.items)


def test_arxiv_ingestion_status_returns_bounded_job_state():
    result = _service()._execute_agent_tool_call_impl(
        AgentToolCall("arxiv", {"action": "status", "job_id": "job-1"}), [], [], [], 3
    )

    assert result.status == "done"
    assert result.items[0]["paper_card_id"] == "paper-1"


def test_native_policy_blocks_arxiv_when_user_requires_fixed_wiki():
    service = _service()
    calls = service._apply_query_tool_policy([
        AgentToolCall("arxiv", {"action": "search", "query": "latest"}),
        AgentToolCall("wiki_open", {"card_ids": ["paper-1"]})],
        message="\u53ea\u4f7f\u7528\u5f53\u524d Wiki", effective_query="agents", observations=[], limit=5)
    assert [c.name for c in calls] == ["wiki_open"]


def test_native_policy_requires_arxiv_search_grounding_before_import():
    service = _service()
    import_call = AgentToolCall("arxiv", {"action": "import", "arxiv_id": "2309.06180"})

    rejected = service._apply_query_tool_policy(
        [import_call],
        message="研究 KV Cache 并建立论文库",
        effective_query="KV Cache",
        observations=[],
        limit=5,
    )
    accepted = service._apply_query_tool_policy(
        [import_call],
        message="研究 KV Cache 并建立论文库",
        effective_query="KV Cache",
        observations=[
            AgentToolObservation(
                "arxiv_search",
                "KV Cache",
                "done",
                items=[{"arxiv_id": "2309.06180"}],
            )
        ],
        limit=5,
    )

    assert rejected == []
    assert [call.operation_name for call in accepted] == ["arxiv_import_paper"]


def test_native_policy_allows_shell_to_resolve_user_supplied_arxiv_ids():
    service = _service()
    calls = service._apply_query_tool_policy(
        [
            AgentToolCall("task_plan_write", {"markdown": "# Goal\nRead the papers"}),
            AgentToolCall("local_shell", {
                "command": "$ids='2309.06180,2401.00001'; Invoke-WebRequest ('https://export.arxiv.org/api/query?id_list=' + $ids)",
                "shell": "powershell",
            }),
        ],
        message=(
            "请整理这些论文：arXiv:2309.06180、2401.00001，"
            "比较后告诉我这个方向的发展路线。"
        ),
        effective_query="recursive self improvement",
        observations=[],
        limit=8,
    )

    assert [call.operation_name for call in calls] == ["task_plan_write", "local_shell"]
    assert "Invoke-WebRequest" in calls[1].arguments["command"]


def test_native_policy_treats_versioned_lookup_result_as_the_same_explicit_source():
    calls = _service()._apply_query_tool_policy(
        [AgentToolCall("task_plan_write", {"markdown": "# Goal\nContinue"})],
        message="请阅读并整理 arXiv:2309.06180",
        effective_query="paper summary",
        observations=[AgentToolObservation(
            "arxiv_lookup", "2309.06180", "done",
            items=[{"arxiv_id": "2309.06180v2", "found": True}],
        )],
        limit=5,
    )

    assert [call.operation_name for call in calls] == ["task_plan_write"]


def test_native_policy_allows_import_after_exact_id_lookup():
    calls = _service()._apply_query_tool_policy(
        [AgentToolCall("arxiv", {"action": "import", "arxiv_id": "2309.06180"})],
        message="请阅读并整理 arXiv:2309.06180",
        effective_query="paper summary",
        observations=[AgentToolObservation(
            "arxiv_lookup", "2309.06180", "done",
            items=[{"arxiv_id": "2309.06180", "found": True}],
        )],
        limit=5,
    )

    assert [call.operation_name for call in calls] == ["arxiv_import_paper"]


def test_ingestion_status_is_repeatable_while_other_tool_calls_remain_deduplicated(monkeypatch):
    service = _service()
    service.llm = object()
    job_call = AgentToolCall("arxiv", {"action": "status", "job_id": "job-1"})
    planned = [[job_call], []]
    executed = []
    monkeypatch.setattr(service, "_next_agent_tool_calls", lambda **kwargs: planned.pop(0))
    monkeypatch.setattr(service, "_wait_for_ingestion_interval", lambda seconds: None)

    def execute(call, cards, web_results, resources, limit):
        executed.append(call.operation_name)
        status = "running" if len(executed) == 1 else "done"
        return AgentToolObservation(
            call.name, "job-1", "done", f"ingestion job status: {status}",
            [{"id": "job-1", "status": status}], arguments=dict(call.arguments),
        )

    monkeypatch.setattr(service, "_execute_agent_tool_call", execute)
    service._run_tool_loop("继续等待论文入库", "论文入库", [], max_steps=3)

    assert executed == ["arxiv_ingestion_status", "arxiv_ingestion_status"]


def test_read_result_does_not_force_the_agent_loop_to_finish(monkeypatch):
    service = _service()
    service.llm = object()
    planned = [
        [AgentToolCall("read_tool_result", {"result_id": 29})],
        [AgentToolCall("wiki_search", {"query": "recursive self improvement"})],
        [],
    ]
    executed = []
    monkeypatch.setattr(service, "_next_agent_tool_calls", lambda **kwargs: planned.pop(0))

    def execute(call, cards, web_results, resources, limit):
        executed.append(call.operation_name)
        return AgentToolObservation(call.name, "", "done", "ok", [])

    monkeypatch.setattr(service, "_execute_agent_tool_call", execute)
    service._run_tool_loop("整理多篇论文", "RSI", [], max_steps=3)

    assert executed == ["read_tool_result", "wiki_search"]


def test_native_policy_keeps_query_only_open_without_retired_search():
    calls = _service()._apply_query_tool_policy(
        [AgentToolCall("wiki_open", {"query": "GRPO"})],
        message="解释 GRPO",
        effective_query="GRPO",
        observations=[],
        limit=4,
    )

    assert [call.operation_name for call in calls] == ["wiki_open"]
    assert calls[0].arguments["query"] == "GRPO"


def test_search_preserves_model_selected_keywords():
    query = "agent harness context management"
    calls = _service()._apply_query_tool_policy([
        AgentToolCall("arxiv", {"action": "search", "query": query})],
        message="Research agent architectures", effective_query="agents", observations=[], limit=5)
    assert calls[0].arguments["query"] == query


def test_retired_recommendations_are_not_accepted_alongside_arxiv_search():
    calls = _service()._normalize_agent_tool_calls({"tool_calls": [
        {"name": "resource_recommend", "arguments": {"query": "papers"}},
        {"name": "arxiv", "arguments": {"action": "search", "query": "KV cache"}}]}, "papers", 5)
    assert [c.name for c in calls] == ["arxiv"]


def test_native_policy_checks_explicit_ingestion_jobs_even_when_model_returns_no_calls():
    job_ids = [
        "8044c897-4c36-40be-8135-7aabad032d7a",
        "8da12058-d2ad-49ef-94e0-32695dd330db",
    ]

    calls = _service()._apply_query_tool_policy(
        [],
        message=(
            "检查已完成的异步入库结果，必须调用 arxiv_ingestion_status 核验每个 job："
            + "、".join(job_ids)
        ),
        effective_query="Agent Harness",
        observations=[],
        limit=5,
    )

    assert [call.operation_name for call in calls] == ["arxiv_ingestion_status", "arxiv_ingestion_status"]
    assert [call.arguments["job_id"] for call in calls] == job_ids


def test_native_policy_does_not_recheck_job_observed_in_current_loop():
    job_id = "8044c897-4c36-40be-8135-7aabad032d7a"

    calls = _service()._apply_query_tool_policy(
        [AgentToolCall("arxiv", {"action": "search", "query": "agent harness"})],
        message=f"调用 arxiv_ingestion_status 核验 job {job_id} 的状态",
        effective_query="Agent Harness",
        observations=[
            AgentToolObservation(
                "arxiv_ingestion_status",
                job_id,
                "done",
                items=[{"job_id": job_id, "status": "done"}],
            )
        ],
        limit=5,
    )

    assert [call.operation_name for call in calls] == ["arxiv_search"]


def test_native_policy_does_not_replace_model_choice_from_prose_progress_claims():
    calls = _service()._apply_query_tool_policy(
        [AgentToolCall("wiki_search", {"query": "interview guide"})],
        message=(
            "当前只有 3/6 张 PaperPage，尚未达到持久语料硬门槛。"
            "本轮优先补库：先用一次 arxiv_search。原任务是 Agent Harness 研究。"
        ),
        effective_query="Agent Harness research and interview guide",
        observations=[],
        limit=8,
    )

    assert [call.operation_name for call in calls] == ["wiki_search"]


def test_json_tool_fallback_receives_the_same_runtime_policy_as_native_calls(monkeypatch):
    service = _service()
    monkeypatch.setattr(service, "_next_native_tool_calls", lambda **kwargs: None)
    monkeypatch.setattr(
        service,
        "_invoke_llm",
        lambda *args, **kwargs: (
            '{"finish": false, "tool_calls": ['
            '{"name": "wiki_open", "arguments": {"card_ids": ["paper-1"]}}]}'
        ),
    )

    calls = service._next_agent_tool_calls(
        message=(
            "当前只有 3/6 张 PaperPage，尚未达到持久语料硬门槛。"
            "本轮优先补库。原任务是 Agent Harness 研究。"
        ),
        effective_query="Agent Harness research",
        history=[],
        observations=[],
        step_index=0,
        limit=8,
    )

    assert [call.operation_name for call in calls] == ["wiki_open"]


def test_policy_never_turns_a_read_call_into_an_automatic_import_side_effect():
    calls = _service()._apply_query_tool_policy(
        [AgentToolCall("web_search", {"query": "more papers"})],
        message=(
            "当前只有 3/8 张 PaperPage，尚未达到持久语料硬门槛。"
            "本轮以量化为重点优先补库。"
        ),
        effective_query="KV Cache quantization",
        observations=[
            AgentToolObservation(
                "arxiv_search",
                "KV cache quantization",
                "done",
                items=[{"arxiv_id": "2401.00001"}, {"arxiv_id": "2401.00002"}],
            )
        ],
        limit=8,
    )

    assert [call.operation_name for call in calls] == ["web_search"]


def test_legacy_synthesis_phase_blocks_unified_search(monkeypatch):
    service = _service()
    monkeypatch.setattr(service, "_active_research_task", lambda: {"phase": "SYNTHESIZE"})
    calls = service._apply_query_tool_policy([
        AgentToolCall("arxiv", {"action": "search", "query": "agents"}),
        AgentToolCall("wiki_open", {"card_ids": ["paper-1"]})],
        message="Finish report", effective_query="agents", observations=[], limit=5)
    assert [c.name for c in calls] == ["wiki_open"]


def test_default_registry_routes_discovery_to_unified_arxiv():
    calls = _service()._normalize_agent_tool_calls({"tool_calls": [
        {"name": "web_search", "arguments": {"query": "KV cache"}},
        {"name": "web_fetch", "arguments": {"url": "https://example.com"}},
        {"name": "arxiv", "arguments": {"action": "search", "query": "KV cache"}}]}, "papers", 5)
    assert [c.name for c in calls] == ["arxiv"]


def test_research_discovery_query_targets_first_coverage_gap():
    query = _service()._research_discovery_query(
        {
            "topic_minimum": 3,
            "required_topics": {
                "quantization": ["quantization"],
                "eviction": ["eviction"],
            },
            "coverage": {
                "quantization": ["paper-1", "paper-2", "paper-3"],
                "eviction": [],
            },
        },
        "从当前论文库自主研究 KV Cache",
    )

    assert query == "large language model KV cache eviction pruning"


def test_discover_phase_injects_arxiv_search_when_planner_stalls(monkeypatch):
    service = _service()
    monkeypatch.setattr(service, "_active_research_task", lambda: {
        "phase": "DISCOVER",
        "topic_minimum": 3,
        "required_topics": {"offloading": ["offload"]},
        "coverage": {"offloading": []},
        "budget": {"max_discovery_calls": 12},
        "usage": {},
    })

    call = service._research_discovery_fallback_call(
        message="研究 KV Cache",
        observations=[],
        limit=6,
    )

    assert call is not None
    assert call.operation_name == "arxiv_search"
    assert call.arguments["query"] == "large language model KV cache offloading"


def test_legacy_research_task_does_not_switch_the_unified_loop(monkeypatch):
    service = _service()
    service.llm = object()
    active_task = {
        "phase": "DISCOVER",
        "topic_minimum": 3,
        "required_topics": {"eviction": ["eviction"]},
        "coverage": {"eviction": []},
        "budget": {"max_cycles": 1, "max_discovery_calls": 12},
        "usage": {},
    }
    executed = []
    monkeypatch.setattr(service, "_active_research_task", lambda: active_task)
    monkeypatch.setattr(service, "_next_agent_tool_calls", lambda **kwargs: [])

    def execute(call, cards, web_results, resources, limit):
        executed.append(call.operation_name)
        return AgentToolObservation(call.name, str(call.arguments.get("query") or ""), "done", items=[])

    monkeypatch.setattr(service, "_execute_agent_tool_call", execute)

    result = service._run_tool_loop(
        "从当前论文库自主研究 KV Cache，生成技术图谱和发展路线图。",
        "KV Cache",
        [],
        limit=5,
    )

    assert executed == []
    assert result["plan"].tools == []
