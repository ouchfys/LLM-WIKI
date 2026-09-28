"""User's acceptance query through real reading, card saving and stopping.

Network responses and model decisions are fixtures. This verifies orchestration,
not the quality of a live model's research of the four real projects.
"""
import json

import pytest

from system.agent_runtime.control import RunControl
from system.agent_runtime.repositories import RepositoryReader
from system.wiki.markdown_vault import MarkdownVault
from system.wiki.markdown_reindexer import MarkdownWikiReindexer
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.repository_writer import RepositoryWikiWriter
from system.wiki.revision import WikiRevisionManager
from system.wiki.wiki_store import WikiStore
from system.wiki.wiki_chat import WikiChatService, AgentToolCall


QUERY = r"研究deepseek harness\codex cli\pi agent\hermes这种开源agent架构的记忆系统，并且分别写入wiki"
PROJECTS = ["deepseek-harness", "codex-cli", "pi-agent", "hermes"]
FILES = ["config.py", "memory.py", "compact.py", "test_memory.py"]
SHA = "b" * 40


class EntailedReviewer:
    """Scripted model responses; this fixture does not infer numerical meaning."""
    def __init__(self, verdicts=None):
        self.prompts = []
        self.verdicts = {
            "默认值是 99。": ("contradicted", "草稿写 99，源码 DEFAULT_LIMIT 的值为 20。"),
            "记忆由会话读取函数载入，历史通过摘要函数压缩。容量是 99999。":
                ("insufficient", "来源没有提供容量上限的依据。"),
            **(verdicts or {}),
        }

    def invoke(self, prompt, **kwargs):
        self.prompts.append(prompt)
        entries = json.loads(prompt.split("INPUT:\n", 1)[1])["sections"]
        checks = []
        for entry in entries:
            label, reason = self.verdicts.get(entry["statement"], ("entailed", "fixture source supports section"))
            checks.append({"id": entry["id"], "label": label, "reason": reason})
        return json.dumps({"checks": checks})


class Response:
    status_code = 200
    headers = {}
    def __init__(self, data, status=200):
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.status_code = status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def iter_content(self, _):
        yield self.raw


class FixtureHTTP:
    blocked = ""
    def get(self, url, **kwargs):
        if self.blocked and self.blocked in url:
            return Response({}, 404)
        if "/commits/" in url:
            return Response({"sha": SHA})
        filename = url.rsplit("/", 1)[-1]
        return Response((f"# {filename}: memory behavior fixture\n" + {
            "config.py": "MEMORY_PATH = 'session.json'",
            "memory.py": "def load_memory(): return read_session()",
            "compact.py": "def compact(): return summarize_history()",
            "test_memory.py": "def test_memory(): assert load_memory()",
        }[filename]).encode())


def setup_service(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    db = str(tmp_path / "wiki.db")
    wiki, evidence = WikiStore(db_path=db), PaperWikiPipelineStore(db_path=db)
    manager = WikiRevisionManager(store=evidence, vault=MarkdownVault(str(tmp_path / "vault")),
                                  reindexer=MarkdownWikiReindexer(db_path=db))
    reader = RepositoryReader(tmp_path / "repos", http=FixtureHTTP())
    def unexpected_review(*args, **kwargs):
        pytest.fail("wiki_write must not make a model call")
    writer = RepositoryWikiWriter(wiki, evidence, revision_manager=manager,
                                  llm=type("UnusedReviewer", (), {"invoke": staticmethod(unexpected_review)})())
    model = type("Model", (), {"invoke": lambda *args, **kwargs: ""})()
    service = WikiChatService(wiki, llm=model, evidence_store=evidence, repositories=reader, repository_writer=writer)
    return service, writer, manager


@pytest.mark.parametrize("research_mode", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
def test_four_project_query_writes_pages_before_finishing(tmp_path, monkeypatch, research_mode, blocked):
    service, writer, manager = setup_service(tmp_path, monkeypatch)
    if blocked:
        service.repositories.http.blocked = PROJECTS[0]
    control = RunControl(None, "", QUERY)
    control.loop_state["research_mode"] = research_mode

    def decide(**kwargs):
        observations = control.loop_state["observations"]
        for index, name in enumerate(PROJECTS):
            repo = "fixtures/" + name
            if any(obs.tool == "repository" and obs.status == "error" and any(i.get("repository") == repo for i in obs.items) for obs in observations):
                continue
            items = [item for obs in observations if obs.status == "done" for item in obs.items if item.get("repository") == repo]
            if any(item.get("status") == "committed" for item in items):
                continue
            snapshots = [item for item in items if item.get("snapshot_id")]
            common = {"gap_id": f"research{index}", "query_purpose": "定位和核对该项目的记忆实现"}
            if not snapshots:
                return [AgentToolCall("repository", {"operation": "open", "repository": repo, **common})]
            read = [item for item in items if item.get("kind") == "source"]
            for path in FILES:
                if not any(item.get("path") == path for item in read):
                    return [AgentToolCall("repository", {"operation": "read", "snapshot_id": snapshots[0]["snapshot_id"], "path": path, **common})]
            content = "记忆由会话读取函数载入，历史通过摘要函数压缩。"
            return [AgentToolCall("wiki_write", {"repository": repo, "title": name + " 记忆系统", "topic": "memory-system",
                "sections": [{"heading": "实现与测试", "content": content}]})]
        return []

    monkeypatch.setattr(service, "_next_agent_tool_calls", decide)
    monkeypatch.setattr(service, "_assess_research_state", lambda *a: pytest.fail("Unexpected per-read assessment"))
    with control.bind():
        result = service._run_tool_loop(QUERY, QUERY, [], max_steps=40)
    assert result["trace"]["stop_reason"] == "model_finished"
    assert "research_state" not in result["trace"]
    receipts = [item for obs in control.loop_state["observations"] if obs.tool == "wiki_write" and obs.status == "done" for item in obs.items]
    assert len(receipts) == (3 if blocked else 4), [(o.tool, o.summary) for o in control.loop_state["observations"]]
    for receipt in receipts:
        assert receipt["status"] == "committed" and receipt["verified_readback"]
        card = service.wiki_store.get_card(receipt["card_id"])
        assert card["current_revision_id"] == receipt["revision_id"]
        assert SHA in manager.vault.read_reference(card["markdown_path"])
        assert receipt["evidence_ids"] == []
        assert receipt["review"]["model_calls"] == 0
    assert len({r["card_id"] for r in receipts}) == len(receipts)
    # Replaying the exact write reuses the committed revision and doesn't need cached source files.
    last = receipts[-1]
    obs = control.loop_state["observations"]
    sections = [{"heading": "实现与测试", "content": "记忆由会话读取函数载入，历史通过摘要函数压缩。", "evidence_ids": last["evidence_ids"]}]
    repeated = writer.write(title=last["title"], repository=last["repository"], topic="memory-system", sections=sections, observations=obs)
    assert repeated["revision_id"] == last["revision_id"]


def test_shell_write_can_omit_unknown_source_version_and_span_ids(tmp_path, monkeypatch):
    service, writer, _ = setup_service(tmp_path, monkeypatch)
    receipt = writer.write(title="Memory", repository="fixtures/agent", topic="memory", observations=[],
                           sections=[{"heading": "Mechanism", "content": "Claim"}])
    assert receipt["commit"] == "" and receipt["verified_readback"]
    assert "https://github.com/fixtures/agent" in receipt["content"]
    assert "阅读日期（UTC）：" in receipt["content"] and "版本：未记录" in receipt["content"]
