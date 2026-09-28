"""Repository writes save checked prose; historical review metadata stays readable."""
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from system.agent_runtime.control import RunControl, RunCancelled
from system.wiki.repository_verification import RepositoryEvidenceVerifier, repository_review_status
from system.wiki.repository_writer import RepositoryWikiWriter
from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation
from test_repository_research_flow import setup_service, SHA, EntailedReviewer


FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/repository_write_failure_20260926.json").read_text(encoding="utf-8"))


class FrozenEvidence:
    def get_evidence(self, ids):
        return [FIXTURE["evidence"][ref] for ref in ids if ref in FIXTURE["evidence"]]


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["revision_id"][:8] + ":" + c["claim"]["id"][-7:])
def test_legacy_verifier_reads_original_claim_and_evidence(case):
    # The verifier remains available to historical/other evidence workflows.
    claim = case["claim"]
    label = "insufficient" if case["old_missing"] == ["404"] else "entailed"
    model = EntailedReviewer({claim["statement"]: (label, "fixture verdict")})
    verifier = RepositoryEvidenceVerifier(FrozenEvidence(), model)
    result = verifier.verify_claim_payload(claim)
    assert result.semantic_result == label
    document = json.loads(model.prompts[0].split("INPUT:\n", 1)[1])
    assert document["sections"][0]["statement"] == claim["statement"]
    assert {e["id"]: e["text"] for e in document["evidence"]} == {
        ref: FIXTURE["evidence"][ref]["text"] for ref in claim["evidence_ids"]}


def source_observation():
    content = "DEFAULT_LIMIT = 20\ndef load_memory(): return read_session()\n"
    source = {"kind": "source", "repository": "fixtures/agent", "commit": SHA, "path": "memory.py",
              "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest(),
              "span_id": "repo_test", "start_line": 1, "end_line": 2, "total_lines": 50,
              "url": f"https://github.com/fixtures/agent/blob/{SHA}/memory.py#L1-L2"}
    return AgentToolObservation("repository", "", "done", "read", [source])


def section(content, section_id=None):
    return {"heading": "Mechanism", "content": content,
            **({"section_id": section_id} if section_id else {})}


def save(writer, **changes):
    args = dict(title="Memory", repository="fixtures/agent", topic="memory",
                sections=[section("默认值为 20。")], observations=[source_observation()])
    args.update(changes)
    return writer.write(**args)


def forbid_verifier(manager, monkeypatch):
    monkeypatch.setattr(manager.verifier, "verify_claim_payloads",
                        lambda *a, **k: pytest.fail("Saving must not invoke the shared verifier"))
    monkeypatch.setattr(manager.verifier, "verify_claim_payload",
                        lambda *a, **k: pytest.fail("Saving must not invoke the shared verifier"))


@pytest.mark.parametrize("content", [
    "默认阈值为 99。", "commit " + "a" * 40, "读取 L1-99。", "文件共 999 行。",
    "容量上限是 1 KiB。", "需要 2 个条件：线程数至少为 20，且摘要有效。",
])
def test_saving_does_not_attempt_another_content_review(tmp_path, monkeypatch, content):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    forbid_verifier(manager, monkeypatch)
    receipt = save(writer, sections=[section(content)])
    assert receipt["status"] == "committed" and content in receipt["content"]
    assert receipt["review"] == {"policy": "author_self_check", "status": "not_requested",
        "model_calls": 0, "independent_status": "not_run", "current_version_reviewed": False}


def test_only_card_and_basic_snapshot_metadata_are_persisted(tmp_path, monkeypatch):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    forbid_verifier(manager, monkeypatch)
    # Old callers may still supply evidence_ids; they have no persistence role.
    receipt = save(writer, sections=[{**section("会话记忆由读取函数载入。"), "evidence_ids": ["repo_test", "old-id"]}])
    card = writer.wiki_store.get_card(receipt["card_id"])
    revision = writer.evidence_store.get_revision(receipt["revision_id"])
    markdown = manager.vault.read_reference(receipt["markdown_path"])
    assert card["page_type"] == "ConceptPage"
    assert "### Mechanism\n\n会话记忆由读取函数载入。" in markdown
    assert "/blob/" not in markdown and "https://github.com/fixtures/agent" in markdown
    assert "阅读日期（UTC）：" in markdown and SHA in markdown
    assert markdown.rstrip().endswith("</small>")
    assert 'status: "saved"' in markdown
    assert not card["content_json"].get("claims") and not card["content_json"].get("source_packet_ids")
    assert revision["source_ids"] == [] and revision["verification"]["claims"] == []
    assert revision["verification"]["verification_policy"] == "author_self_check"
    assert "evidence_ids" not in revision["verification"]["repository_draft"]["sections"][0]
    with sqlite3.connect(writer.evidence_store.db_path) as db:
        for table in ("source_packets", "wiki_claims", "claim_evidence", "wiki_card_sources"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["open", "shell"])
def test_snapshot_does_not_require_repository_source_spans(tmp_path, monkeypatch, mode):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    forbid_verifier(manager, monkeypatch)
    observed = [] if mode == "shell" else [
        AgentToolObservation("repository", "", "done", "opened",
            [{"kind": "navigation", "repository": "fixtures/agent", "commit": SHA, "snapshot_id": "opened"}])]
    receipt = save(writer, observations=observed, commit=SHA if mode == "shell" else "")
    assert receipt["commit"] == SHA and receipt["verified_readback"]


@pytest.mark.parametrize("commit", ["main", "abcdef", "z" * 40, "a" * 39])
def test_invalid_commit_cannot_create_a_snapshot(tmp_path, monkeypatch, commit):
    _, writer, _ = setup_service(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="full commit hash"):
        save(writer, observations=[], commit=commit)
    assert not writer.wiki_store.get_card("unknown")


def test_shell_commit_can_override_old_observations_and_failed_reads_do_not_supply_versions(tmp_path, monkeypatch):
    _, writer, _ = setup_service(tmp_path, monkeypatch)
    receipt = save(writer, commit="a" * 40)
    assert receipt["commit"] == "a" * 40
    failed = source_observation()
    failed.status = "error"
    receipt = save(writer, observations=[failed])
    assert receipt["commit"] == ""


def test_explicit_commit_selects_one_of_multiple_researched_versions(tmp_path, monkeypatch):
    _, writer, _ = setup_service(tmp_path, monkeypatch)
    other = source_observation()
    other.items[0]["commit"] = "a" * 40
    observed = [source_observation(), other]
    with pytest.raises(ValueError, match="Multiple repository versions"):
        save(writer, observations=observed)
    receipt = save(writer, observations=observed, commit=SHA)
    assert receipt["commit"] == SHA and receipt["verified_readback"]


def test_commit_can_be_restored_when_editing_an_existing_revision(tmp_path, monkeypatch):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    first = save(writer)
    restarted = RepositoryWikiWriter(writer.wiki_store, writer.evidence_store, revision_manager=manager)
    second = save(restarted, observations=[], revision_id=first["revision_id"],
                  sections=[section("修正后的结论。", "section-0")])
    assert second["commit"] == SHA and "修正后的结论。" in second["content"]


def test_old_committed_revision_without_draft_restores_sections_and_stale_guard(tmp_path, monkeypatch):
    _, writer, _ = setup_service(tmp_path, monkeypatch)
    first = save(writer, sections=[section("保留的第一节。"), section("待改的第二节。")])
    verification = writer.evidence_store.get_revision(first["revision_id"])["verification"]
    verification.pop("repository_draft")
    verification.pop("verification_policy")
    writer.evidence_store.update_revision_verification(first["revision_id"], verification)
    second = save(writer, revision_id=first["revision_id"], observations=[],
                  sections=[section("已改的第二节。", "section-1")])
    assert "保留的第一节。" in second["content"] and "已改的第二节。" in second["content"]
    assert second["commit"] == SHA
    with pytest.raises(ValueError, match="stale"):
        save(writer, revision_id=first["revision_id"], sections=[section("过时的修改。", "section-0")])


def test_section_edit_and_exact_retry_preserve_other_sections(tmp_path, monkeypatch):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    forbid_verifier(manager, monkeypatch)
    first = save(writer, sections=[section("短期记忆。"), section("待修正的长期记忆。")])
    args = dict(revision_id=first["revision_id"], sections=[section("长期记忆修正。", "section-1")])
    second = save(writer, **args)
    assert "短期记忆。" in second["content"] and "长期记忆修正。" in second["content"]
    repeat = save(writer, revision_id=second["revision_id"],
                  sections=[section("短期记忆。"), section("长期记忆修正。")])
    assert repeat["revision_id"] == second["revision_id"]
    with pytest.raises(ValueError, match="stale"):
        save(writer, **args)
    with pytest.raises(ValueError, match="known section_id"):
        save(writer, revision_id=second["revision_id"], sections=[section("错误定位。", "section-99")])


def test_legacy_rejected_draft_can_be_saved_without_reviewer(tmp_path, monkeypatch):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    first = save(writer)
    prior = writer.evidence_store.get_revision(first["revision_id"])
    verification = prior["verification"]
    verification["repository_review"] = {"policy": "single_pass", "status": "incomplete", "model_calls": 1}
    verification.pop("verification_policy")
    rejected = writer.evidence_store.create_revision(page_id=first["card_id"], parent_revision_id=first["revision_id"],
        patch="", before_markdown=prior["full_markdown"], full_markdown=prior["full_markdown"],
        reason="legacy fixture", source_ids=[], verification=verification, review_status="rejected")
    forbid_verifier(manager, monkeypatch)
    receipt = save(writer, revision_id=rejected, sections=[section("作者修正后的内容。", "section-0")])
    assert receipt["review"]["policy"] == "author_self_check" and "作者修正" in receipt["content"]


def test_whole_card_full_text_survives_saving_and_followup(tmp_path, monkeypatch):
    service, _, manager = setup_service(tmp_path, monkeypatch)
    forbid_verifier(manager, monkeypatch)
    control = RunControl(None, "", "write and explain")
    observed = [source_observation()]
    control.loop_state["observations"] = observed
    cards = []
    args = dict(title="Memory", repository="fixtures/agent", topic="memory",
                sections=[section("短期记忆。" * 500), section("长期记忆在会话文件中。")])
    with control.bind():
        first = service._execute_agent_tool_call(AgentToolCall("wiki_write", args), cards, [], [], 6)
        observed.append(first)
        assert first.status == "done"
        assert "长期记忆在会话文件中。" in cards[0]["_full_text"]
        from system.wiki.citation_context import WikiCitationContext
        context = WikiCitationContext(cards, service.context_budget.counter, service._compact_content)
        assert "长期记忆在会话文件中。" in context.render(10000)
        args["sections"][-1] = section("长期记忆示例：重新加载会话文件。")
        second = service._execute_agent_tool_call(AgentToolCall("wiki_write", args), cards, [], [], 6)
        assert second.status == "done" and len(cards) == 1
        assert "长期记忆示例" in cards[0]["_full_text"]


@pytest.mark.parametrize("native", [False, True])
def test_both_tool_routes_preserve_edit_revision_and_shell_commit(tmp_path, monkeypatch, native):
    service, _, _ = setup_service(tmp_path, monkeypatch)
    args = dict(title="Memory", repository="fixtures/agent", topic="memory", revision_id="existing",
                commit=SHA, sections=[section("默认值是 20。", "section-0")])
    raw = {"name": "wiki_write", "arguments": args}
    if native:
        calls = service._normalize_native_tool_calls({"tool_calls": [{"function": raw}]}, "query", 6)
    else:
        calls = service._normalize_agent_tool_calls({"tool_calls": [raw]}, "query", 6)
    assert calls[0].arguments["revision_id"] == "existing"
    assert calls[0].arguments["sections"][0]["section_id"] == "section-0"
    assert calls[0].arguments["commit"] == SHA


@pytest.mark.parametrize("sections", [[section("读取会话。") for _ in range(17)], [section("x" * 50001)]])
def test_malformed_or_oversized_card_fails_before_saving(tmp_path, monkeypatch, sections):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    forbid_verifier(manager, monkeypatch)
    with pytest.raises(ValueError, match="1..16|bounded writing size"):
        save(writer, sections=sections)


def test_reindex_failure_restores_previous_card_and_revision(tmp_path, monkeypatch):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    first = save(writer)
    before = manager.vault.read_reference(first["markdown_path"])
    original = manager.reindexer.reindex_reference
    attempts = []
    def fail_once(reference):
        attempts.append(reference)
        if len(attempts) == 1:
            raise OSError("fixture index failure")
        return original(reference)
    monkeypatch.setattr(manager.reindexer, "reindex_reference", fail_once)
    with pytest.raises(OSError, match="fixture index failure"):
        save(writer, sections=[section("这次提交应该回滚。")])
    card = writer.wiki_store.get_card(first["card_id"])
    assert card["current_revision_id"] == first["revision_id"]
    assert manager.vault.read_reference(first["markdown_path"]) == before


def test_cancellation_stops_before_commit(tmp_path, monkeypatch):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    from system.agent_runtime import control
    def cancel():
        raise RunCancelled("cancelled")
    monkeypatch.setattr(control, "check_run_control", cancel)
    with pytest.raises(RunCancelled):
        save(writer)


def test_proposal_edit_and_rollback_keep_self_check_policy(tmp_path, monkeypatch):
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    forbid_verifier(manager, monkeypatch)
    first = save(writer)
    card = writer.wiki_store.get_card(first["card_id"])
    content = dict(card["content_json"], reading_guide="### Mechanism\n\n第二个版本。")
    proposal = manager.propose_card(card_id=card["id"], title="Memory", page_type="ConceptPage",
        content_json=content, summary="", source_level="primary", source_urls=[], related_topics=[],
        existing_card=card, verification_policy="author_self_check")
    draft = writer.evidence_store.get_revision(proposal["revision_id"])
    edited = manager.edit_proposal(proposal["revision_id"], draft["full_markdown"].replace("第二个版本。", "编辑后的第二版。"))
    final = manager.commit_proposal(edited["revision_id"])
    assert writer.evidence_store.get_revision(final["revision_id"])["verification"]["verification_policy"] == "author_self_check"
    assert "编辑后的第二版。" in manager.vault.read_reference(first["markdown_path"])
    rolled = manager.rollback(final["revision_id"])
    restored = writer.evidence_store.get_revision(rolled["revision_id"])
    assert restored["verification"]["verification_policy"] == "author_self_check"
    assert restored["verification"]["repository_review"]["independent_status"] == "not_run"
    assert "默认值为 20。" in manager.vault.read_reference(first["markdown_path"])
    third = save(writer, revision_id=rolled["revision_id"], observations=[],
                 sections=[section("回滚后也可编辑。", "section-0")])
    assert third["verified_readback"]


def test_shared_revision_manager_still_verifies_other_workflows(tmp_path, monkeypatch):
    _, _, manager = setup_service(tmp_path, monkeypatch)
    calls = []
    original = manager.verifier.verify_claim_payloads
    def record(claims):
        calls.append(claims)
        return original(claims)
    monkeypatch.setattr(manager.verifier, "verify_claim_payloads", record)
    result = manager.propose_card(card_id="paper-test", title="Paper", page_type="ConceptPage",
        content_json={"claims": [{"id": "claim-1", "statement": "Unsubstantiated assertion", "evidence_ids": ["missing"]}]},
        summary="", source_level="primary", source_urls=[], related_topics=[])
    assert len(calls) == 1 and result["review_status"] == "rejected"


def test_review_metadata_survives_reindex_without_claiming_independent_approval(tmp_path, monkeypatch):
    from backend.api.wiki import get_card
    _, writer, manager = setup_service(tmp_path, monkeypatch)
    monkeypatch.setattr(writer.wiki_store, "vault", manager.vault)
    receipt = save(writer)
    before = manager.vault.read_reference(receipt["markdown_path"])
    manager.reindexer.reindex_reference(receipt["markdown_path"])
    card = get_card(receipt["card_id"], store=writer.wiki_store)
    assert card["repository_review"]["independent_status"] == "not_run"
    assert card["repository_review"]["current_version_reviewed"] is False
    assert card["content_json"]["repository_review"]["policy"] == "author_self_check"
    assert manager.vault.read_reference(receipt["markdown_path"]) == before


@pytest.mark.parametrize("initial", ["complete", "incomplete"])
def test_legacy_review_chain_still_reports_actual_historical_verdict(initial):
    old = {"id": "old", "page_id": "card", "verification": {
        "repository_review": {"status": initial}, "checks": [{"semantic_result": "contradicted"}]}}
    current = {"id": "new", "page_id": "card", "verification": {"repository_review": {
        "status": "author_revised", "source_revision_id": "old"}}}
    review = repository_review_status(SimpleNamespace(get_revision=lambda rid: old), current)
    assert review["independent_status"] == ("incomplete" if initial == "incomplete" else "changes_requested")
    assert review["current_version_reviewed"] is False


@pytest.mark.parametrize("problem", ["missing", "cycle", "other_card", "no_verdict"])
def test_legacy_review_cannot_infer_approval_from_broken_provenance(problem):
    current = {"id": "new", "page_id": "card", "verification": {"repository_review": {
        "status": "author_revised", "source_revision_id": "old"}}}
    source = {"id": "old", "page_id": "card", "verification": {
        "repository_review": {"status": "complete"}, "checks": []}}
    if problem == "missing":
        source = None
    elif problem == "cycle":
        source = current
    elif problem == "other_card":
        source.update(page_id="unrelated")
        source["verification"]["checks"] = [{"semantic_result": "entailed"}]
    review = repository_review_status(SimpleNamespace(get_revision=lambda rid: source), current)
    assert review["independent_status"] == "unknown"
    assert review["current_version_reviewed"] is False
