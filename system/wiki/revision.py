"""Verified Wiki revision commit and rollback service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from system.wiki.evidence_verifier import EvidenceVerifier
from system.wiki.hierarchical_compiler import HierarchicalWikiCompiler
from system.wiki.markdown_parser import content_json_from_sections, parse_markdown_card
from system.wiki.markdown_reindexer import MarkdownWikiReindexer
from system.wiki.markdown_vault import MarkdownVault
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore


class RevisionRejectedError(RuntimeError):
    pass


class WikiRevisionManager:
    def __init__(
        self,
        *,
        store: PaperWikiPipelineStore,
        vault: MarkdownVault,
        reindexer: MarkdownWikiReindexer,
    ):
        self.store = store
        self.vault = vault
        self.reindexer = reindexer
        self.verifier = EvidenceVerifier(store)

    def commit_card(
        self,
        *,
        card_id: str,
        title: str,
        page_type: str,
        content_json: dict[str, Any],
        summary: str,
        source_level: str,
        source_urls: list[str],
        related_topics: list[str],
        existing_card: dict[str, Any] | None = None,
        reason: str = "compiler merge",
    ) -> dict[str, Any]:
        proposal = self.propose_card(
            card_id=card_id, title=title, page_type=page_type,
            content_json=content_json, summary=summary, source_level=source_level,
            source_urls=source_urls, related_topics=related_topics,
            existing_card=existing_card, reason=reason,
        )
        if proposal.get("review_status") == "rejected":
            raise RevisionRejectedError(
                f"revision {proposal['revision_id']} rejected: unsupported claim(s)"
            )
        return self.commit_proposal(str(proposal["revision_id"]))

    def propose_card(
        self,
        *,
        card_id: str,
        title: str,
        page_type: str,
        content_json: dict[str, Any],
        summary: str,
        source_level: str,
        source_urls: list[str],
        related_topics: list[str],
        existing_card: dict[str, Any] | None = None,
        reason: str = "compiler merge",
    ) -> dict[str, Any]:
        """Freeze and verify a proposal without mutating the Markdown Wiki."""
        existing_card = existing_card or {}
        before = self.vault.read_reference(str(existing_card.get("markdown_path") or ""))
        proposed = self.vault.render_card(
            card_id=card_id,
            title=title,
            page_type=page_type,
            summary=summary,
            content_json=content_json,
            source_level=source_level,
            source_urls=source_urls,
            related_topics=related_topics,
            created=str(existing_card.get("created_at") or ""),
        )
        patch = HierarchicalWikiCompiler.unified_patch(before, proposed, card_id)
        claims = [item for item in content_json.get("claims") or [] if isinstance(item, dict)]
        affected_ids = {
            str(item.get("claim_id") or "")
            for item in content_json.get("affected_claims") or []
            if isinstance(item, dict)
        }
        to_verify = [claim for claim in claims if not affected_ids or str(claim.get("id") or "") in affected_ids]
        checks = [self.verifier.verify_claim_payload(claim).as_dict() for claim in to_verify]
        unsupported = [check for check in checks if check["result"] == "unsupported"]
        source_ids = _source_ids(claims, content_json)
        target_path = str(self.vault._resolve_path(
            card_id, title, page_type, str(existing_card.get("markdown_path") or "")
        ))
        try:
            target_path = str(Path(target_path).relative_to(self.vault.repo_root)).replace("\\", "/")
        except ValueError:
            pass
        proposal_meta = {
            "checks": checks,
            "unsupported": len(unsupported),
            "claims": claims,
            "affected_claims": [
                item for item in content_json.get("affected_claims") or []
                if isinstance(item, dict)
            ],
            "target_markdown_path": target_path,
            "title": title,
            "page_type": page_type,
        }
        revision_id = self.store.create_revision(
            page_id=card_id,
            parent_revision_id=str(existing_card.get("current_revision_id") or ""),
            patch=patch,
            before_markdown=before,
            full_markdown=proposed,
            reason=reason,
            source_ids=source_ids,
            verification=proposal_meta,
            review_status="rejected" if unsupported else "proposed",
        )
        return {
            "card_id": card_id,
            "revision_id": revision_id,
            "patch": patch,
            "verification": checks,
            "review_status": "rejected" if unsupported else "proposed",
            "target_markdown_path": target_path,
        }

    def commit_proposal(self, revision_id: str) -> dict[str, Any]:
        """Commit the exact frozen Markdown after approval and reject stale parents."""
        revision = self.store.get_revision(revision_id)
        if not revision or revision.get("review_status") != "proposed":
            raise ValueError("Only a proposed revision can be committed.")
        card_id = str(revision["page_id"])
        existing_card = self.reindexer.wiki_store.get_card(card_id) or {}
        expected_parent = str(revision.get("parent_revision_id") or "")
        actual_parent = str(existing_card.get("current_revision_id") or "")
        if actual_parent != expected_parent:
            raise ValueError(
                f"Stale proposal: expected parent {expected_parent or '(new page)'}, "
                f"current parent is {actual_parent or '(new page)'}"
            )
        verification = revision.get("verification") or {}
        target_path = str(verification.get("target_markdown_path") or existing_card.get("markdown_path") or "")
        if not target_path:
            raise ValueError("Proposal has no target Markdown path")
        claims = [item for item in verification.get("claims") or [] if isinstance(item, dict)]
        checks = [item for item in verification.get("checks") or [] if isinstance(item, dict)]

        markdown_path = ""
        try:
            markdown_path = self.vault.write_raw_reference(target_path, str(revision.get("full_markdown") or ""))
            indexed = self.reindexer.reindex_reference(markdown_path)
            self.store.finalize_revision(revision_id, card_id, "committed")
            self.store.replace_revision_claims(
                page_id=card_id,
                revision_id=revision_id,
                claims=claims,
                verification=checks,
            )
        except Exception:
            self._compensate_failed_write(
                card_id, markdown_path, str(revision.get("before_markdown") or ""), bool(existing_card)
            )
            self.store.finalize_revision(revision_id, card_id, "rejected")
            raise
        return {**indexed, "revision_id": revision_id, "patch": revision.get("patch", ""), "verification": checks}

    def reject_proposal(self, revision_id: str) -> dict[str, Any]:
        revision = self.store.get_revision(revision_id)
        if not revision or revision.get("review_status") != "proposed":
            raise ValueError("Only a proposed revision can be rejected.")
        self.store.finalize_revision(revision_id, str(revision["page_id"]), "rejected")
        return {"revision_id": revision_id, "page_id": revision["page_id"], "review_status": "rejected"}

    def edit_proposal(self, revision_id: str, full_markdown: str, *, reason: str = "human edited proposal") -> dict[str, Any]:
        """Create a verified replacement proposal; edited Markdown never bypasses guardrails."""
        revision = self.store.get_revision(revision_id)
        if not revision or revision.get("review_status") != "proposed":
            raise ValueError("Only a proposed revision can be edited.")
        parsed = parse_markdown_card(full_markdown)
        page_id = str(revision["page_id"])
        if parsed.id != page_id:
            raise ValueError("Edited Markdown must preserve the proposal page id.")
        content_json = content_json_from_sections(parsed)
        claims = [item for item in parsed.claims if isinstance(item, dict)]
        checks = [self.verifier.verify_claim_payload(claim).as_dict() for claim in claims]
        unsupported = [check for check in checks if check.get("result") == "unsupported"]
        original_verification = revision.get("verification") or {}
        verification = {
            "checks": checks,
            "unsupported": len(unsupported),
            "claims": claims,
            "target_markdown_path": original_verification.get("target_markdown_path", ""),
            "title": parsed.title,
            "page_type": parsed.page_type,
            "edited_from_revision_id": revision_id,
            "post_commit_effects": list(original_verification.get("post_commit_effects") or []),
        }
        replacement_id = self.store.create_revision(
            page_id=page_id,
            parent_revision_id=str(revision.get("parent_revision_id") or ""),
            patch=HierarchicalWikiCompiler.unified_patch(
                str(revision.get("before_markdown") or ""), full_markdown, page_id
            ),
            before_markdown=str(revision.get("before_markdown") or ""),
            full_markdown=full_markdown,
            reason=reason,
            source_ids=_source_ids(claims, content_json) or list(revision.get("source_ids") or []),
            verification=verification,
            review_status="rejected" if unsupported else "proposed",
        )
        return {
            "revision_id": replacement_id,
            "page_id": page_id,
            "review_status": "rejected" if unsupported else "proposed",
            "patch": HierarchicalWikiCompiler.unified_patch(
                str(revision.get("before_markdown") or ""), full_markdown, page_id
            ),
            "verification": checks,
        }

    def apply_post_commit_effects(self, revision_id: str) -> dict[str, Any]:
        """Apply metadata frozen with a committed proposal, once and only after commit."""
        revision = self.store.get_revision(revision_id)
        if not revision or revision.get("review_status") != "committed":
            raise ValueError("Post-commit effects require a committed revision.")
        verification = revision.get("verification") or {}
        if verification.get("post_commit_effects_applied_at"):
            return verification.get("post_commit_effect_result") or {"applied": 0, "skipped": []}
        effects = [item for item in verification.get("post_commit_effects") or [] if isinstance(item, dict)]
        applied = 0
        skipped: list[dict[str, Any]] = []
        for effect in effects:
            kind = str(effect.get("kind") or "")
            payload = effect.get("payload") if isinstance(effect.get("payload"), dict) else {}
            if kind == "aliases":
                card_id = str(payload.get("card_id") or revision["page_id"])
                if not self.reindexer.wiki_store.get_card(card_id):
                    skipped.append({"kind": kind, "reason": "card_not_committed", "card_id": card_id})
                    continue
                self.store.add_aliases(card_id, list(payload.get("aliases") or []))
            elif kind == "source":
                card_id = str(payload.get("card_id") or revision["page_id"])
                if not self.reindexer.wiki_store.get_card(card_id):
                    skipped.append({"kind": kind, "reason": "card_not_committed", "card_id": card_id})
                    continue
                source_card_id = str(payload.get("source_card_id") or "")
                if source_card_id and not self.reindexer.wiki_store.get_card(source_card_id):
                    source_card_id = ""
                self.store.add_card_source(
                    card_id=card_id,
                    source_packet_id=str(payload.get("source_packet_id") or ""),
                    source_card_id=source_card_id,
                    raw_source_path=str(payload.get("raw_source_path") or ""),
                    source_url=str(payload.get("source_url") or ""),
                    section_id=str(payload.get("section_id") or ""),
                    evidence_text=str(payload.get("evidence_text") or ""),
                    claim_text=str(payload.get("claim_text") or ""),
                    confidence=float(payload.get("confidence") or 1.0),
                )
            elif kind == "link":
                from_card_id = str(payload.get("from_card_id") or "")
                to_card_id = str(payload.get("to_card_id") or "")
                if not self.reindexer.wiki_store.get_card(from_card_id) or not self.reindexer.wiki_store.get_card(to_card_id):
                    skipped.append({
                        "kind": kind, "reason": "link_endpoint_not_committed",
                        "from_card_id": from_card_id, "to_card_id": to_card_id,
                    })
                    continue
                self.store.add_card_link(
                    from_card_id=from_card_id,
                    to_card_id=to_card_id,
                    relation_type=str(payload.get("relation_type") or "related"),
                    source_packet_id=str(payload.get("source_packet_id") or ""),
                    evidence_text=str(payload.get("evidence_text") or ""),
                )
            else:
                skipped.append({"kind": kind or "unknown", "reason": "unsupported_effect"})
                continue
            applied += 1
        result = {"applied": applied, "skipped": skipped}
        verification["post_commit_effect_result"] = result
        verification["post_commit_effects_applied_at"] = self.store.now_iso()
        self.store.update_revision_verification(revision_id, verification)
        return result

    def rollback(self, revision_id: str, *, reason: str = "manual rollback") -> dict[str, Any]:
        revision = self.store.get_revision(revision_id)
        if not revision or revision.get("review_status") != "committed":
            raise ValueError("Only a committed revision can be rolled back.")
        page_id = str(revision["page_id"])
        card = self.reindexer.wiki_store.get_card(page_id)
        if not card or not card.get("markdown_path"):
            raise FileNotFoundError(f"Wiki page not found for revision {revision_id}")
        target = str(revision.get("before_markdown") or "")
        if not target:
            raise ValueError("The first page-creation revision has no previous Markdown to restore.")
        parse_markdown_card(target)  # fail before write if the snapshot is corrupt
        current = self.vault.read_reference(card["markdown_path"])
        rollback_patch = HierarchicalWikiCompiler.unified_patch(current, target, page_id)
        rollback_id = self.store.create_revision(
            page_id=page_id,
            parent_revision_id=str(card.get("current_revision_id") or revision_id),
            patch=rollback_patch,
            before_markdown=current,
            full_markdown=target,
            reason=reason,
            source_ids=revision.get("source_ids") or [],
            verification={"rollback_of": revision_id},
            review_status="proposed",
        )
        try:
            reference = self.vault.write_raw_reference(card["markdown_path"], target)
            indexed = self.reindexer.reindex_reference(reference)
            self.store.finalize_revision(rollback_id, page_id, "committed")
            restored = parse_markdown_card(target)
            checks = [self.verifier.verify_claim_payload(claim).as_dict() for claim in restored.claims]
            self.store.replace_revision_claims(
                page_id=page_id,
                revision_id=rollback_id,
                claims=restored.claims,
                verification=checks,
            )
            self.store.mark_revision_rolled_back(revision_id)
        except Exception:
            try:
                reference = self.vault.write_raw_reference(card["markdown_path"], current)
                self.reindexer.reindex_reference(reference)
            finally:
                self.store.finalize_revision(rollback_id, page_id, "rejected")
            raise
        return {**indexed, "revision_id": rollback_id, "rolled_back_revision_id": revision_id, "patch": rollback_patch}

    def _compensate_failed_write(self, card_id: str, markdown_path: str, before: str, existed: bool) -> None:
        """Best-effort compensation for the filesystem/SQLite transaction gap."""
        try:
            if existed and before and markdown_path:
                reference = self.vault.write_raw_reference(markdown_path, before)
                self.reindexer.reindex_reference(reference)
            elif markdown_path:
                self.reindexer.wiki_store.delete_card(card_id)
                self.vault.delete_card(markdown_path)
        except Exception as exc:
            print(f"[wiki.revision] compensation failed for {card_id}: {exc}")


def _source_ids(claims: list[dict[str, Any]], content: dict[str, Any]) -> list[str]:
    values = []
    for claim in claims:
        values.extend(str(value) for value in claim.get("source_packet_ids") or [] if str(value))
    values.extend(str(value) for value in content.get("source_packet_ids") or [] if str(value))
    if content.get("source_packet_id"):
        values.append(str(content["source_packet_id"]))
    return list(dict.fromkeys(values))
