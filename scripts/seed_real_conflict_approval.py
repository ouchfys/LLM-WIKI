from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from system.agent_runtime.store import AgentRunStore
from system.wiki.markdown_reindexer import MarkdownWikiReindexer
from system.wiki.markdown_vault import MarkdownVault
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.revision import WikiRevisionManager
from system.wiki.wiki_store import WikiStore


SCENARIO_KEY = "real-grpo-exploration-conflict-v1"
TARGET_CARD_TITLE = "Group Relative Policy Optimization (GRPO)"
INCOMING_PAPER_TITLE = (
    "Unveiling Implicit Advantage Symmetry: "
    "Why GRPO Struggles with Exploration and Difficulty Adaptation"
)
EXISTING_CLAIM_MARKER = "增强数学推理能力"
EVIDENCE_QUERY = (
    "GRPO lacks an intrinsic active exploration mechanism for unsampled "
    "correct trajectories leading to local optima entrapment"
)
INCOMING_CLAIM_ID = "clm-real-grpo-exploration-conflict"
INCOMING_STATEMENT = (
    "GRPO 并不能稳定扩展模型的推理能力边界：其隐式优势对称性会限制"
    "对未采样正确轨迹的主动探索。"
)
COMPARISON_REASON = (
    "后续研究发现 GRPO 的隐式优势对称性会限制未采样正确轨迹的探索，"
    "并可能造成能力边界收缩；这挑战了早期论文将 GRPO 概括为直接增强数学推理能力的结论。"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a reproducible approval from two real GRPO papers already in the Wiki."
    )
    parser.add_argument(
        "--db-path",
        default=str(REPO_ROOT / "sessions.db"),
        help="SQLite database path.",
    )
    return parser.parse_args()


def _card_by_title(wiki: WikiStore, title: str) -> dict[str, Any]:
    for card in wiki.list_cards(limit=1000):
        if str(card.get("title") or "").strip() == title:
            return card
    raise RuntimeError(f"Required Wiki card not found: {title}")


def _active_scenario(runtime: AgentRunStore) -> dict[str, Any] | None:
    for run in runtime.list_runs(limit=500):
        if (run.get("context") or {}).get("scenario_key") != SCENARIO_KEY:
            continue
        for approval in runtime.list_approvals(status="action_required", run_id=str(run["id"]), limit=20):
            return {"run": run, "approval": approval}
    return None


def _existing_claim(content: dict[str, Any]) -> dict[str, Any]:
    for claim in content.get("claims") or []:
        if isinstance(claim, dict) and EXISTING_CLAIM_MARKER in str(claim.get("statement") or ""):
            return dict(claim)
    raise RuntimeError(
        f"The target card no longer contains the expected claim marker: {EXISTING_CLAIM_MARKER}"
    )


def _incoming_claim(
    *,
    source_packet_id: str,
    existing_claim_id: str,
    evidence_id: str,
    evidence_text: str,
) -> dict[str, Any]:
    return {
        "id": INCOMING_CLAIM_ID,
        "statement": INCOMING_STATEMENT,
        "status": "conflicting",
        "relation": "challenges",
        "evidence_ids": [evidence_id],
        "evidence_excerpt": evidence_text[:1000],
        "section_id": "implicit-advantage-symmetry-in-grpo",
        "page_start": 5,
        "confidence": 0.98,
        "verifier_result": "entailed",
        "verifier_reason": "The persisted source span explicitly states the exploration limitation.",
        "entailment_score": 1.0,
        "semantic_verification_required": True,
        "source_packet_ids": [source_packet_id],
        "candidate_id": "real-grpo-conflict-backfill",
        "supersedes_claim_id": "",
        "conflicts_with_claim_id": existing_claim_id,
        "comparison_reason": COMPARISON_REASON,
    }


def create_scenario(db_path: str) -> dict[str, Any]:
    runtime = AgentRunStore(db_path=db_path)
    active = _active_scenario(runtime)
    if active:
        return {
            "ok": True,
            "created": False,
            "message": "The real GRPO conflict approval is already waiting for review.",
            "run_id": active["run"]["id"],
            "approval_id": active["approval"]["id"],
            "revision_id": active["approval"]["revision_id"],
        }

    wiki = WikiStore(db_path=db_path)
    pipeline = PaperWikiPipelineStore(db_path=db_path)
    target_card = _card_by_title(wiki, TARGET_CARD_TITLE)
    incoming_paper = _card_by_title(wiki, INCOMING_PAPER_TITLE)
    incoming_content = incoming_paper.get("content_json") or {}
    source_packet_id = str(
        incoming_content.get("source_packet_id")
        or next(iter(incoming_content.get("source_packet_ids") or []), "")
    )
    packet = pipeline.get_source_packet(source_packet_id)
    if not packet:
        raise RuntimeError(f"SourcePacket missing for {INCOMING_PAPER_TITLE}")

    evidence = pipeline.find_evidence(source_packet_id, text=EVIDENCE_QUERY, limit=1)
    if not evidence:
        raise RuntimeError("The persisted evidence for the GRPO limitation was not found.")
    evidence_id = str(evidence[0].get("id") or "")
    evidence_text = str(evidence[0].get("text") or evidence[0].get("caption") or "").strip()
    if not evidence_id or "unsampled" not in evidence_text.lower():
        raise RuntimeError("The matched evidence is not the expected GRPO exploration passage.")

    content = dict(target_card.get("content_json") or {})
    existing_claim = _existing_claim(content)
    claims = [
        dict(claim)
        for claim in content.get("claims") or []
        if isinstance(claim, dict) and str(claim.get("id") or "") != INCOMING_CLAIM_ID
    ]
    incoming_claim = _incoming_claim(
        source_packet_id=source_packet_id,
        existing_claim_id=str(existing_claim["id"]),
        evidence_id=evidence_id,
        evidence_text=evidence_text,
    )
    claims.append(incoming_claim)
    content["claims"] = claims
    content["affected_claims"] = [{
        "action": "challenge_claim",
        "claim_id": INCOMING_CLAIM_ID,
        "statement": INCOMING_STATEMENT,
        "compared_claim_id": str(existing_claim["id"]),
    }]
    content["source_packet_ids"] = list(dict.fromkeys([
        *[str(value) for value in content.get("source_packet_ids") or [] if str(value)],
        source_packet_id,
    ]))
    content["sources"] = [
        *[item for item in content.get("sources") or [] if isinstance(item, dict)],
        {
            "url": packet.source_urls[0] if packet.source_urls else "",
            "level": "primary",
            "source_packet_id": source_packet_id,
        },
    ]
    content["limitations"] = (
        "后续研究补充了 GRPO 的适用边界：GRAE 的隐式优势对称性对未采样"
        "轨迹产生零梯度，因此缺少主动探索新的正确路径的内在机制，且对样本难度的"
        "动态变化不敏感。"
    )

    source_urls = list(dict.fromkeys([
        *[str(value) for value in target_card.get("source_urls") or [] if str(value)],
        *[str(value) for value in packet.source_urls if str(value)],
    ]))
    manager = WikiRevisionManager(
        store=pipeline,
        vault=MarkdownVault(),
        reindexer=MarkdownWikiReindexer(db_path=db_path),
    )
    proposal = manager.propose_card(
        card_id=str(target_card["id"]),
        title=str(target_card["title"]),
        page_type=str(target_card["page_type"]),
        content_json=content,
        summary=str(target_card.get("summary") or ""),
        source_level=str(target_card.get("source_level") or "primary"),
        source_urls=source_urls,
        related_topics=[str(value) for value in target_card.get("related_topics") or []],
        existing_card=target_card,
        reason="real cross-paper conflict: later GRPO evidence challenges the existing capability claim",
    )
    if proposal.get("review_status") != "proposed":
        raise RuntimeError(f"Conflict proposal failed evidence verification: {proposal}")

    run = runtime.create_run(
        run_type="paper_conflict_backfill",
        source_uri=packet.source_urls[0] if packet.source_urls else packet.pdf_storage_uri,
        approval_mode="risk",
        context={
            "scenario_key": SCENARIO_KEY,
            "scenario_kind": "real_cross_paper_conflict",
            "source_packet_id": source_packet_id,
            "source_title": packet.title,
            "target_card_id": target_card["id"],
            "existing_claim_id": existing_claim["id"],
            "incoming_claim_id": INCOMING_CLAIM_ID,
            "evidence_id": evidence_id,
        },
    )
    run_id = str(run["id"])
    for state, reason in (
        ("EXTRACTING", "reused persisted source evidence"),
        ("DISTILLING", "distilled the GRPO exploration limitation"),
        ("VERIFYING", "re-read the persisted source span"),
        ("COMPILING_PROPOSAL", "compiled a claim-aware Markdown patch"),
    ):
        runtime.transition(run_id, state, reason=reason)
    approval = runtime.create_approval(
        run_id=run_id,
        revision_id=str(proposal["revision_id"]),
        page_id=str(target_card["id"]),
        title=str(target_card["title"]),
    )
    runtime.append_event(
        run_id,
        event_type="approval.risk_escalated",
        node_name="AWAITING_APPROVAL",
        status="pending",
        input_data={
            "approval_id": approval["id"],
            "reasons": ["changes_claim_history"],
            "scenario_key": SCENARIO_KEY,
        },
    )
    runtime.transition(
        run_id,
        "AWAITING_APPROVAL",
        context_updates={
            "approval_ids": [approval["id"]],
            "revision_ids": [proposal["revision_id"]],
        },
        result={
            "ok": True,
            "source_packet_id": source_packet_id,
            "paper_card_id": incoming_paper["id"],
            "approval_ids": [approval["id"]],
        },
        reason="real cross-paper claim conflict requires a knowledge policy decision",
    )
    return {
        "ok": True,
        "created": True,
        "message": "Created a real cross-paper GRPO conflict approval.",
        "run_id": run_id,
        "approval_id": approval["id"],
        "revision_id": proposal["revision_id"],
        "target_card_id": target_card["id"],
        "incoming_source_packet_id": source_packet_id,
        "existing_source_packet_id": (existing_claim.get("source_packet_ids") or [""])[0],
        "evidence_id": evidence_id,
    }


def main() -> int:
    args = parse_args()
    print(json.dumps(create_scenario(args.db_path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
