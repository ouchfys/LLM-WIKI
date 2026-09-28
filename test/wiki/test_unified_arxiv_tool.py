import json
from types import SimpleNamespace

import pytest

from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService


class Papers:
    def __init__(self):
        self.searches = []
        self.lookups = []

    def search(self, query, **kwargs):
        self.searches.append((query, kwargs))
        return {"papers": [{"arxiv_id": "2501.12345", "title": "RSIAgent",
                            "abstract": "Candidate for identity verification", "authors": ["Example Author"]}]}

    def get_papers(self, ids):
        self.lookups.append(ids)
        return [{"arxiv_id": value, "title": "Example"} for value in ids]


def make_service():
    papers, imports, polls = Papers(), [], []

    def submit(arxiv_id, **kwargs):
        imports.append((arxiv_id, kwargs))
        return {"ok": True, "arxiv_id": arxiv_id,
                "ingestion": {"job_id": "job-1", "agent_run_id": "run-1"}}

    def status(job_id):
        polls.append(job_id)
        return {"id": job_id, "status": "done", "paper_card_id": "paper-1"}

    service = WikiChatService(object(), wiki_resolver=object(),
        arxiv_service=SimpleNamespace(arxiv=papers, import_paper=submit,
                                     ingestion=SimpleNamespace(get_job=status)))
    return service, papers, imports, polls


def normalize(service, native, args):
    if native:
        return service._normalize_native_tool_calls({"tool_calls": [{"id": "call-1", "function": {
            "name": "arxiv", "arguments": json.dumps(args)}}]}, "Import RSIAgent", 5)
    return service._normalize_agent_tool_calls({"tool_calls": [{"name": "arxiv", "arguments": args}]},
                                                "Import RSIAgent", 5)


@pytest.mark.parametrize("native", [False, True])
def test_named_paper_search_import_status_flow_with_observed_identity(native):
    service, papers, imports, polls = make_service()
    observations = []
    for args in ({"action": "search", "query": "RSIAgent", "limit": 15},
                 {"action": "import", "arxiv_id": "2501.12345"},
                 {"action": "status", "job_id": "job-1"}):
        calls = service._apply_query_tool_policy(normalize(service, native, args),
            message="Import RSIAgent", effective_query="Import RSIAgent", observations=observations, limit=5)
        assert len(calls) == 1
        result = service._execute_agent_tool_call(calls[0], [], [], [], 5)
        assert result.tool == "arxiv"
        assert result.status == "done"
        assert result.arguments["action"] == args["action"]
        observations.append(result)
    assert papers.searches[0][0] == "RSIAgent"
    assert papers.searches[0][1]["max_results"] == 15
    assert imports == [("2501.12345", {"approval_mode": "risk"})]
    assert polls == ["job-1"]
    assert service._pending_ingestion_jobs(observations) == {}
    state = service._execution_state([service._observation_payload(o) for o in observations])
    assert state["latest_ingestion_jobs"]["job-1"]["paper_card_id"] == "paper-1"


def test_lookup_keeps_batch_and_search_filters():
    service, papers, _, _ = make_service()
    result = service._execute_agent_tool_call(AgentToolCall("arxiv", {
        "action": "lookup", "arxiv_ids": ["1706.03762", "2501.12345"]}), [], [], [], 5)
    assert result.status == "done"
    assert papers.lookups == [["1706.03762", "2501.12345"]]
    service._execute_agent_tool_call(AgentToolCall("arxiv", {"action": "search", "query": "agent memory",
        "author": "Example", "categories": ["cs.AI"], "year_from": 2024}), [], [], [], 5)
    assert papers.searches[0][1]["author"] == "Example"
    assert papers.searches[0][1]["year_from"] == 2024


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("args", [{}, {"action": "download"}, {"action": "search"},
    {"action": "lookup", "arxiv_ids": []}, {"action": "import"}, {"action": "status"},
    {"action": "import", "arxiv_id": "2501.12345", "approval_mode": "skip"}])
def test_invalid_action_arguments_return_feedback_without_io(native, args):
    service, papers, imports, polls = make_service()
    calls = service._apply_query_tool_policy(normalize(service, native, args),
        message="Import RSIAgent", effective_query="", observations=[], limit=5)
    assert len(calls) == 1
    result = service._execute_agent_tool_call(calls[0], [], [], [], 5)
    assert result.status == "error"
    assert not papers.searches and not papers.lookups and not imports and not polls


def test_guessed_id_cannot_import_until_observed():
    service, _, imports, _ = make_service()
    call = AgentToolCall("arxiv", {"action": "import", "arxiv_id": "2501.12345"})
    assert service._apply_query_tool_policy([call], message="Import RSIAgent", effective_query="",
                                            observations=[], limit=5) == []
    assert not imports


def test_import_is_a_write_barrier_and_independent_reads_stay_parallel():
    search = AgentToolCall("arxiv", {"action": "search", "query": "agents"})
    lookup = AgentToolCall("arxiv", {"action": "lookup", "arxiv_ids": ["1706.03762"]})
    submit = AgentToolCall("arxiv", {"action": "import", "arxiv_id": "1706.03762"})
    status = AgentToolCall("arxiv", {"action": "status", "job_id": "j"})
    assert WikiChatService._tool_execution_batches([search, lookup, submit, status]) == [
        [search, lookup], [submit], [status]]


def test_failed_job_is_distinct_from_successful_status_read_and_keeps_action():
    service, _, _, _ = make_service()
    service.arxiv_service.ingestion.get_job = lambda job: {"id": job, "status": "failed", "error": "parse failed"}
    result = service._execute_agent_tool_call(AgentToolCall("arxiv", {"action": "status", "job_id": "j"}), [], [], [], 5)
    assert result.status == "done"
    assert result.items[0]["status"] == "failed"
    assert service._execution_state([result])["latest_ingestion_jobs"]["j"]["error"] == "parse failed"


def test_status_deduplication_and_pending_jobs_accept_old_and_new_receipts():
    service, _, _, _ = make_service()
    old = AgentToolObservation("arxiv_import_paper", "", "done", items=[{"job_id": "j"}])
    running = AgentToolObservation("arxiv", "j", "done", items=[{"id": "j", "status": "running"}],
                                   arguments={"action": "status", "job_id": "j"})
    done = AgentToolObservation("arxiv", "j", "done", items=[{"id": "j", "status": "done"}],
                                arguments={"action": "status", "job_id": "j"})
    assert "j" in service._pending_ingestion_jobs([old, running])
    assert not service._pending_ingestion_jobs([old, running, done])
    assert service._latest_status_observations([old, running, done]) == [old, done]
