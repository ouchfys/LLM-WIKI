from __future__ import annotations

import sqlite3
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from system.storage import object_storage as object_storage_module
from system.storage.object_storage import ObjectStorage
from system.wiki.markdown_reindexer import MarkdownWikiReindexer
from system.wiki.markdown_vault import MarkdownVault
from system.wiki.paper_pipeline.merger import PaperMergeAgent
from system.wiki.paper_pipeline.models import CandidateClaim, DistilledCandidate, ReviewReport, SourcePacket
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.wiki_store import WikiStore


class FakeMergeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, prompt: str, temperature: float = 0.0, max_tokens: int = 3600) -> str:
        self.calls += 1
        return (
            '{"action":"update_existing","target_card_id":"target-card",'
            '"field_updates":{"mechanism":{"mode":"append","text":"Incoming paper adds a second mechanism."}},'
            '"aliases_to_add":["MFM"],"links_to_add":[],'
            '"reason":"append new evidence","confidence":0.88}'
        )


def make_agent(work_dir: Path, llm=None) -> tuple[PaperMergeAgent, WikiStore]:
    storage = ObjectStorage()
    storage.backend = "local"
    storage.root_prefix = ""
    object_storage_module._STORAGE = storage

    db_path = str(work_dir / "smoke.db")
    wiki_store = WikiStore(db_path=db_path)
    pipeline_store = PaperWikiPipelineStore(db_path=db_path)
    agent = PaperMergeAgent(pipeline_store=pipeline_store, wiki_store=wiki_store, llm=llm)
    agent.markdown_vault = MarkdownVault(vault_dir=str(work_dir / "wiki"))
    agent.markdown_reindexer = MarkdownWikiReindexer(db_path=db_path)
    return agent, wiki_store


def test_topic_merge_preserves_existing_reader_guide_without_model() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        agent, _ = make_agent(Path(tmp))
        guide = "### 直观理解\n\n旧主题导读，包含已有来源的解释。"
        result = agent._merged_reading_guide(
            existing={"reading_guide": guide, "source_packet_ids": ["old-paper"]},
            incoming={"reading_guide": "### 新材料\n\n另一篇论文提供新的证据。"},
            merged={"definition": "合并后的定义"},
            packet=SourcePacket(source_id="new-paper", title="New Paper"),
        )
        assert result == guide


def test_topic_merge_refreshes_reader_guide_from_same_source() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        agent, _ = make_agent(Path(tmp))
        result = agent._merged_reading_guide(
            existing={"reading_guide": "旧导读", "source_packet_ids": ["paper"]},
            incoming={"reading_guide": "### 更新\n\n同一论文重新编写的导读。"},
            merged={},
            packet=SourcePacket(source_id="paper", title="Paper"),
        )
        assert "同一论文重新编写" in result


def test_topic_merge_synthesizes_guide_when_a_second_source_arrives() -> None:
    class GuideLLM:
        def invoke(self, prompt, **_kwargs):
            assert "existing_guide" in prompt and "incoming_guide" in prompt
            return json.dumps({"reading_guide": (
                "### 这个概念是什么\n\n它描述系统怎样保存可复用的执行经验。"
                "旧研究说明如何记录步骤，新研究补充了核验结果的办法。\n\n"
                "### 怎样理解\n\n可以把它想成附有检查记录的工作笔记："
                "笔记帮助下次继续工作，但仍需核对原始证据。这个例子只是解释概念。"
                "两项研究讨论的任务条件并不完全相同，因此不能把其中一篇报告的结果"
                "直接当作所有系统都能达到的效果。阅读时应分别查看各自的测试范围。"
            )}, ensure_ascii=False)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        agent, _ = make_agent(Path(tmp), llm=GuideLLM())
        guide = agent._merged_reading_guide(
            existing={"reading_guide": "### 原有理解\n\n记录执行步骤。", "source_packet_ids": ["old-paper"]},
            incoming={"reading_guide": "### 新证据\n\n核验执行结果。"},
            merged={"definition": "保存并核验经验"},
            packet=SourcePacket(source_id="new-paper", title="New Paper"),
        )
        assert "旧研究" in guide and "新研究" in guide


def test_create_new_merge_writes_markdown_then_reindexes_sqlite() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work_dir = Path(tmp)
        agent, wiki_store = make_agent(work_dir)
        class RelationModel:
            def invoke(self, prompt, **kwargs):
                batch = json.loads(prompt.split("Comparison batch:\n", 1)[1])
                return json.dumps({"decisions": [{**pair, "relation": "complements",
                    "confidence": .9, "same_entity": True, "same_aspect": False,
                    "scope_overlap": True} for pair in batch["pairs"]]})
        # A model-free, unresolved comparison now correctly awaits approval.
        # This test exercises the successful Markdown commit path instead.
        agent.relation_resolver.llm = RelationModel()

        packet = SourcePacket(
            source_id="packet-smoke",
            source_type="paper_pdf",
            title="Smoke Paper",
            source_urls=["https://example.com/smoke-paper"],
            raw_source_path="local://sources/papers/raw/smoke.md",
            pdf_storage_uri="local://sources/papers/originals/smoke.pdf",
            parser_used="smoke",
        )
        paper = DistilledCandidate(
            id="cand-paper",
            source_packet_id="packet-smoke",
            candidate_type="paper_page",
            page_type="PaperPage",
            title="Smoke Paper",
            aliases=["Smoke Test Paper"],
            summary="This paper exists to test markdown-first merge.",
            content_json={
                "problem": "Verify canonical Markdown writes.",
                "key_idea": "SQLite is rebuilt after Markdown is written.",
            },
            claims=[
                CandidateClaim(
                    claim="Markdown is canonical.",
                    evidence="The merge writes Markdown before reindexing.",
                    section_id="s1",
                )
            ],
            related_topics=["Markdown-first"],
        )
        concept = DistilledCandidate(
            id="cand-concept",
            source_packet_id="packet-smoke",
            candidate_type="concept_card",
            page_type="ConceptPage",
            title="Markdown-first Merge",
            aliases=["Canonical Markdown Merge"],
            summary="A merge strategy where the wiki Markdown file is the authority layer.",
            content_json={"definition": "Markdown is written before SQLite cache rows are rebuilt."},
            claims=[
                CandidateClaim(
                    claim="Merge writes Markdown first.",
                    evidence="A canonical Markdown file is created and reindexed.",
                    section_id="s2",
                )
            ],
            related_topics=["Smoke Paper"],
        )
        reports = {
            "cand-paper": ReviewReport(candidate_id="cand-paper", status="approved"),
            "cand-concept": ReviewReport(
                candidate_id="cand-concept",
                status="approved",
                merge_recommendation={"action": "create_new", "reason": "new concept", "confidence": 0.91},
            ),
        }

        result = agent.merge(packet=packet, candidates=[paper, concept], reports=reports)
        paper_card = wiki_store.get_card(result.paper_card_id)
        concept_card = wiki_store.get_card(result.created_cards[0]["id"])

        assert paper_card and paper_card.get("markdown_path")
        assert concept_card and concept_card.get("markdown_path")
        assert isinstance(paper_card["content_json"].get("import_impact"), dict)
        assert paper_card["content_json"].get("source_packet_id") == "packet-smoke"
        assert concept_card["content_json"].get("definition")

        with sqlite3.connect(wiki_store.db_path) as conn:
            pages = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM wiki_chunks").fetchone()[0]
            sources = conn.execute("SELECT COUNT(*) FROM wiki_card_sources").fetchone()[0]
        assert pages == 2
        assert chunks >= 2
        assert sources >= 2


def test_update_existing_merge_can_use_llm_and_still_reindex_markdown() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work_dir = Path(tmp)
        fake_llm = FakeMergeLLM()
        agent, wiki_store = make_agent(work_dir, llm=fake_llm)

        agent._write_canonical_card(
            card_id="target-card",
            title="Markdown-first Merge",
            page_type="ConceptPage",
            summary="Existing concept.",
            content_json={
                "definition": "Existing definition.",
                "mechanism": "Existing mechanism.",
                "aliases": ["Markdown-first Merge"],
            },
            source_level="primary",
            source_urls=["https://example.com/old"],
            related_topics=[],
        )

        packet = SourcePacket(
            source_id="packet-update",
            title="Update Paper",
            source_urls=["https://example.com/update"],
            raw_source_path="local://sources/update.md",
        )
        paper = DistilledCandidate(
            id="paper-update",
            source_packet_id="packet-update",
            candidate_type="paper_page",
            page_type="PaperPage",
            title="Update Paper",
            summary="Paper for update.",
            claims=[CandidateClaim(claim="paper", evidence="paper evidence")],
        )
        concept = DistilledCandidate(
            id="concept-update",
            source_packet_id="packet-update",
            candidate_type="concept_card",
            page_type="ConceptPage",
            title="Markdown-first Merge",
            aliases=["MFM"],
            summary="New evidence extends the mechanism.",
            content_json={"mechanism": "Incoming deterministic mechanism."},
            claims=[CandidateClaim(claim="new mechanism", evidence="evidence", section_id="s1")],
        )
        reports = {
            "paper-update": ReviewReport(candidate_id="paper-update", status="approved"),
            "concept-update": ReviewReport(
                candidate_id="concept-update",
                status="approved",
                merge_recommendation={
                    "action": "update_existing",
                    "target_card_id": "target-card",
                    "reason": "possible duplicate with new evidence",
                    "confidence": 0.82,
                },
            ),
        }

        result = agent.merge(packet=packet, candidates=[paper, concept], reports=reports)
        updated = wiki_store.get_card("target-card")

        assert fake_llm.calls == 1
        assert agent.llm_calls == 1
        assert result.updated_cards and result.updated_cards[0]["id"] == "target-card"
        assert updated and updated.get("markdown_path")
        assert "Incoming paper adds a second mechanism." in updated["content_json"].get("mechanism", "")

        with sqlite3.connect(wiki_store.db_path) as conn:
            chunks = conn.execute("SELECT COUNT(*) FROM wiki_chunks WHERE card_id = ?", ("target-card",)).fetchone()[0]
        assert chunks >= 1


def test_manual_markdown_edit_can_reindex_sqlite_cache() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work_dir = Path(tmp)
        agent, wiki_store = make_agent(work_dir)
        markdown_path = agent.markdown_vault.write_card(
            card_id="manual-card",
            title="Manual Markdown Card",
            page_type="ConceptPage",
            summary="Old summary.",
            content_json={"definition": "Original definition."},
            source_level="primary",
            source_urls=["https://example.com/manual"],
            related_topics=[],
        )
        agent.markdown_reindexer.reindex_reference(markdown_path)
        path = Path(markdown_path)
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("Old summary.", "Edited summary."), encoding="utf-8")

        agent.markdown_reindexer.reindex_reference(markdown_path)
        card = wiki_store.get_card("manual-card")

        assert card
        assert card["summary"] == "Edited summary."


if __name__ == "__main__":
    test_create_new_merge_writes_markdown_then_reindexes_sqlite()
    test_update_existing_merge_can_use_llm_and_still_reindex_markdown()
    test_manual_markdown_edit_can_reindex_sqlite_cache()
    print("markdown-first merge smoke tests passed")
