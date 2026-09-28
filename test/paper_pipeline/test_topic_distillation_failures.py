import json

import pytest

from system.agent_runtime import AgentRunStore, TraceRecorder
from system.core.deepseek_client import OutputTruncatedError
from system.wiki.markdown_vault import MarkdownVault
from system.wiki.paper_pipeline.distiller import PaperDistiller
from system.wiki.paper_pipeline.models import DistilledCandidate


class TopicLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def invoke(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)


def paper():
    return DistilledCandidate(candidate_type="paper_page", page_type="PaperPage", title="Paper",
                              source_packet_id="source-1", content_json={"notes": "已有论文说明。"})


@pytest.mark.parametrize("failure", [
    OutputTruncatedError(max_tokens=16384, output_tokens=16384),
    '{"knowledge_cards": [{"title": "unfinished',
])
def test_truncation_and_invalid_json_retry_with_larger_topic_budget(failure, monkeypatch):
    monkeypatch.delenv("PAPERWIKI_TOPIC_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("PAPERWIKI_TOPIC_RETRY_OUTPUT_TOKENS", raising=False)
    llm = TopicLLM([failure, {"knowledge_cards": [{
        "candidate_type": "concept_card", "page_type": "TopicPage", "title": "Reusable method",
            "content_json": {
                "definition": "A source-grounded concept.",
                "reading_guide": (
                    "### 它是什么\n\n这是一种把来源中的关键事实整理成可复用解释的方法。"
                    "阅读时先确认它解决了什么问题，再看它依赖哪些证据。\n\n"
                    "### 怎样使用\n\n可以把它想成一张带出处的说明卡：先用自己的话解释概念，"
                    "遇到重要结论再返回原文核对。这个例子只是帮助理解，并非论文实验。"
                ),
            },
    }]}])
    candidate = paper()
    topics = PaperDistiller(llm)._llm_topic_candidates(candidate)
    assert [call["max_tokens"] for call in llm.calls] == [16384, 32768]
    assert "previous topic response could not be used" in llm.calls[1]["prompt"]
    assert [topic.title for topic in topics] == ["Reusable method"]
    assert candidate.content_json["notes"] == "已有论文说明。"


def test_repeated_topic_failure_is_audited_and_visible_in_paper_notes(tmp_path):
    llm = TopicLLM([OutputTruncatedError(max_tokens=16384), '{"knowledge_cards":'])
    runtime = AgentRunStore(db_path=str(tmp_path / "trace.db"))
    run = runtime.create_run(run_type="paper_ingestion")
    candidate = paper()
    with TraceRecorder(runtime, run["id"]).bind():
        topics = PaperDistiller(llm)._llm_topic_candidates(candidate)
    assert topics == []
    rendered = "\n".join(MarkdownVault._render_content_sections("PaperPage", candidate.content_json))
    assert "已有论文说明。" in rendered
    assert "可选主题卡生成未完成" in rendered
    events = [event for event in runtime.list_events(run["id"]) if event["event_type"].startswith("paper.topic.")]
    assert [event["event_type"] for event in events] == ["paper.topic.retry", "paper.topic.failed"]
    assert "finish_reason=length" in events[0]["error"]
    assert "invalid JSON" in events[1]["error"]


def test_missing_topic_array_retries_but_valid_empty_array_is_success(monkeypatch):
    monkeypatch.setenv("PAPERWIKI_TOPIC_OUTPUT_TOKENS", "24000")
    monkeypatch.setenv("PAPERWIKI_TOPIC_RETRY_OUTPUT_TOKENS", "48000")
    llm = TopicLLM([{}, {"knowledge_cards": []}])
    candidate = paper()
    assert PaperDistiller(llm)._llm_topic_candidates(candidate) == []
    assert [call["max_tokens"] for call in llm.calls] == [24000, 48000]
    assert "no knowledge_cards JSON array" in llm.calls[1]["prompt"]
    assert candidate.content_json["notes"] == "已有论文说明。"
