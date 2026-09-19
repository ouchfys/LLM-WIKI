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
        AgentToolCall("arxiv_search", {"query": "KV cache", "limit": 3}), [], [], [], 3
    )

    assert result.status == "done"
    assert result.items[0]["arxiv_id"] == "2309.06180"


def test_arxiv_import_submits_async_job_without_exposing_local_cache_path():
    result = _service()._execute_agent_tool_call_impl(
        AgentToolCall("arxiv_import_paper", {"arxiv_id": "2309.06180", "approval_mode": "risk"}), [], [], [], 3
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
        AgentToolCall("arxiv_ingestion_status", {"job_id": "job-1"}), [], [], [], 3
    )

    assert result.status == "done"
    assert result.items[0]["paper_card_id"] == "paper-1"


def test_native_policy_blocks_external_tools_when_user_requires_fixed_wiki():
    service = _service()
    calls = service._apply_query_tool_policy(
        [
            AgentToolCall("web_search", {"query": "latest"}),
            AgentToolCall("arxiv_search", {"query": "latest"}),
            AgentToolCall("wiki_search", {"query": "agentic rl"}),
        ],
        message="只使用当前固定 30 篇 Wiki，不得访问 Web",
        effective_query="agentic rl",
        observations=[],
        limit=5,
    )

    assert [call.name for call in calls] == ["wiki_search"]


def test_native_policy_requires_arxiv_search_grounding_before_import():
    service = _service()
    import_call = AgentToolCall("arxiv_import_paper", {"arxiv_id": "2309.06180"})

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
    assert [call.name for call in accepted] == ["arxiv_import_paper"]


def test_native_policy_inserts_wiki_search_before_query_only_open():
    calls = _service()._apply_query_tool_policy(
        [AgentToolCall("wiki_open", {"query": "GRPO"})],
        message="解释 GRPO",
        effective_query="GRPO",
        observations=[],
        limit=4,
    )

    assert [call.name for call in calls] == ["wiki_search", "wiki_open"]


def test_native_policy_compacts_full_prompt_used_as_web_query():
    long_query = (
        "自主检索 Agent Harness 论文与官方工程资料，入库并生成面试指导书，覆盖循环终止、工具、规划、"
        "上下文、记忆、运行状态、恢复、Trace、评测与安全，并逐项结合 PaperWiki 的真实实现。"
    ) * 2

    calls = _service()._apply_query_tool_policy(
        [AgentToolCall("web_search", {"query": long_query})],
        message=long_query,
        effective_query=long_query,
        observations=[],
        limit=5,
    )

    assert calls[0].arguments["query"] == "agent harness official documentation LLM runtime"


def test_native_policy_blocks_learning_recommendations_during_paper_ingestion():
    calls = _service()._apply_query_tool_policy(
        [
            AgentToolCall("resource_recommend", {"query": "KV cache papers"}),
            AgentToolCall("arxiv_search", {"query": "KV cache quantization"}),
        ],
        message="找 KV Cache 论文并入库建立知识库",
        effective_query="KV cache",
        observations=[],
        limit=5,
    )

    assert [call.name for call in calls] == ["arxiv_search"]


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

    assert [call.name for call in calls] == ["arxiv_ingestion_status", "arxiv_ingestion_status"]
    assert [call.arguments["job_id"] for call in calls] == job_ids


def test_native_policy_does_not_recheck_job_observed_in_current_loop():
    job_id = "8044c897-4c36-40be-8135-7aabad032d7a"

    calls = _service()._apply_query_tool_policy(
        [AgentToolCall("arxiv_search", {"query": "agent harness"})],
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

    assert [call.name for call in calls] == ["arxiv_search"]


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

    assert [call.name for call in calls] == ["wiki_search"]


def test_json_tool_fallback_receives_the_same_runtime_policy_as_native_calls(monkeypatch):
    service = _service()
    monkeypatch.setattr(service, "_next_native_tool_calls", lambda **kwargs: None)
    monkeypatch.setattr(
        service,
        "_invoke_llm",
        lambda *args, **kwargs: (
            '{"finish": false, "tool_calls": ['
            '{"name": "wiki_search", "arguments": {"query": "interview guide"}}]}'
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

    assert [call.name for call in calls] == ["wiki_search"]


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

    assert [call.name for call in calls] == ["web_search"]


def test_structured_synthesis_phase_blocks_all_external_discovery(monkeypatch):
    service = _service()
    monkeypatch.setattr(service, "_active_research_task", lambda: {"phase": "SYNTHESIZE"})

    calls = service._apply_query_tool_policy(
        [
            AgentToolCall("web_search", {"query": "latest"}),
            AgentToolCall("arxiv_search", {"query": "agent harness"}),
            AgentToolCall("resource_recommend", {"query": "papers"}),
            AgentToolCall("corpus_manifest", {"page_type": "PaperPage", "cursor": 0, "limit": 50}),
        ],
        message="继续完成研究报告",
        effective_query="agent harness",
        observations=[],
        limit=8,
    )

    assert [call.name for call in calls] == ["corpus_manifest"]
