"""Save a repository Wiki card after the author has checked its draft."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from system.agent_runtime.repositories import RepositoryReader
from system.wiki.markdown_vault import MarkdownVault, readable_markdown
from system.wiki.markdown_reindexer import MarkdownWikiReindexer
from system.wiki.revision import WikiRevisionManager, _repository_draft
from system.wiki.repository_verification import author_self_check_status, repository_review_status


class RepositoryWikiWriter:
    def __init__(self, wiki_store, evidence_store, *, revision_manager=None, llm=None):
        self.wiki_store, self.evidence_store = wiki_store, evidence_store
        self.manager = revision_manager or WikiRevisionManager(
            store=evidence_store, vault=MarkdownVault(), reindexer=MarkdownWikiReindexer(db_path=wiki_store.db_path))
        # llm remains accepted for old callers. Saving never invokes a reviewer
        # and must not replace the shared manager's verifier for other workflows.

    def write(self, *, title, repository, topic, sections, observations=None, unknowns="", revision_id="", commit=""):
        title, topic = str(title).strip(), str(topic).strip()
        repository = RepositoryReader.repository(repository)
        observations = observations or []
        if not title or not topic or len(title) > 200 or len(topic) > 200:
            raise ValueError("A title and stable topic key (at most 200 characters) are required.")
        if not isinstance(sections, list) or not 1 <= len(sections) <= 16:
            raise ValueError("Provide 1..16 sections with a heading and content.")
        card_id = "repo-" + hashlib.sha256((repository.lower() + ":" + topic.casefold()).encode()).hexdigest()[:24]
        existing = self.wiki_store.get_card(card_id) or {}
        previous_revision = None
        latest_write = next((item for obs in reversed(observations) if obs.tool == "wiki_write" and obs.status == "done"
                             for item in obs.items if item.get("card_id") == card_id), None)
        if latest_write and not revision_id:
            revision_id = latest_write.get("revision_id") or ""
        if revision_id:
            previous_revision = self.evidence_store.get_revision(revision_id)
            if (not previous_revision or previous_revision["page_id"] != card_id
                    or previous_revision["review_status"] not in {"rejected", "committed"}):
                raise ValueError("revision_id must name a repository draft for this repository/topic.")
            prior = previous_revision.get("verification", {}).get("repository_draft")
            if not prior and previous_revision["review_status"] == "committed":
                from system.wiki.markdown_parser import parse_markdown_card
                prior = _repository_draft(parse_markdown_card(previous_revision["full_markdown"]))
            if not prior or prior.get("repository", "").lower() != repository.lower():
                raise ValueError("revision_id must name a repository draft for this repository/topic.")
            expected_parent = (revision_id if previous_revision["review_status"] == "committed"
                               else previous_revision.get("parent_revision_id") or "")
            if (existing.get("current_revision_id") or "") != expected_parent:
                raise ValueError("This draft is stale; the Wiki page changed after it was saved or proposed.")
            if any(s.get("section_id") for s in sections if isinstance(s, dict)):
                replacements = {s.get("section_id"): s for s in sections if isinstance(s, dict)}
                known = {s["section_id"] for s in prior["sections"]}
                if len(replacements) != len(sections) or not set(replacements) <= known:
                    raise ValueError("Use known section_id values for replacements, or omit all section_ids to supply the complete article.")
                sections = [replacements.get(s["section_id"], s) for s in prior["sections"]]
            unknowns = unknowns or prior.get("unknowns", "")
        elif any(s.get("section_id") for s in sections if isinstance(s, dict)):
            raise ValueError("Section replacements require revision_id; omit section_ids for a new article.")

        normalized = []
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                raise ValueError("Every section needs a heading and content.")
            heading = str(section.get("heading") or "").strip().lstrip("#").strip()
            content = str(section.get("content") or "").strip()
            if not heading or not content:
                raise ValueError("Every section needs a heading and content.")
            # Old evidence_ids are tolerated, but neither required nor stored.
            normalized.append({"section_id": str(section.get("section_id") or f"section-{index}"),
                               "heading": heading, "content": content})
        if len(json.dumps(normalized, ensure_ascii=False)) > 50000 or len(unknowns) > 6000:
            raise ValueError("Article exceeds the bounded writing size.")
        commit = self._commit(repository, commit, observations, previous_revision)
        draft = {"title": title, "repository": repository, "topic": topic,
                 "commit": commit, "sections": normalized, "unknowns": unknowns}
        explanation_parts = [f"### {section['heading']}\n\n{section['content']}" for section in normalized]
        if unknowns.strip():
            explanation_parts.append("尚未确认：" + unknowns.strip())
        content_json = {"reading_guide": "\n\n".join(explanation_parts),
                        "repository_research": {"repository": repository, "commit": commit, "topic": topic},
                        "repository_review": author_self_check_status(), "aliases": [title]}
        signature = hashlib.sha256(json.dumps(content_json, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = existing.get("content_json") or {}
        if isinstance(previous, str):
            previous = json.loads(previous)
        if previous.get("repository_research", {}).get("content_hash") == signature and existing.get("current_revision_id"):
            result = {"revision_id": existing["current_revision_id"], "markdown_path": existing["markdown_path"]}
        else:
            content_json["repository_research"]["content_hash"] = signature
            content_json["repository_research"]["read_date"] = datetime.now(timezone.utc).date().isoformat()
            proposal = self.manager.propose_card(card_id=card_id, title=title, page_type="ConceptPage",
                content_json=content_json, summary=normalized[0]["content"].split("。", 1)[0][:160], source_level="primary",
                source_urls=[f"https://github.com/{repository}"], related_topics=[], existing_card=existing,
                reason="user-requested repository research", verification_policy="author_self_check")
            revision = self.evidence_store.get_revision(proposal["revision_id"])
            verification = revision["verification"]
            verification.update(repository_draft=draft, repository_review=author_self_check_status())
            self.evidence_store.update_revision_verification(proposal["revision_id"], verification)
            result = self.manager.commit_proposal(proposal["revision_id"])

        saved = self.wiki_store.get_card(card_id)
        revision = self.evidence_store.get_revision(result["revision_id"])
        if not saved or not revision or revision.get("review_status") != "committed" or saved.get("current_revision_id") != result["revision_id"]:
            raise ValueError("Wiki commit could not be verified; inspect the saved revision before retrying.")
        markdown = self.manager.vault.read_reference(saved["markdown_path"])
        if not markdown or (commit and commit not in markdown) or markdown != revision.get("full_markdown"):
            raise ValueError("Wiki Markdown readback differs from the committed revision.")
        return {"kind": "execution_receipt", "status": "committed", "card_id": card_id, "title": title,
                "revision_id": result["revision_id"], "markdown_path": saved["markdown_path"], "repository": repository,
                "commit": commit, "verified_readback": True, "evidence_ids": [],
                "content": readable_markdown(markdown), "topic": topic,
                "review": repository_review_status(self.evidence_store, revision),
                "verification_scope": "Final card saved and read back; content self-check belongs to the author, with no independent model review."}

    @staticmethod
    def _commit(repository, commit, observations, previous_revision):
        """Identify the source version without requiring persisted excerpts."""
        observed = {str(item.get("commit") or "").lower()
                    for obs in observations if obs.tool == "repository" and obs.status == "done"
                    for item in obs.items if item.get("repository", "").lower() == repository.lower() and item.get("commit")}
        explicit = str(commit or "").strip().lower()
        if not explicit and len(observed) > 1:
            raise ValueError("Multiple repository versions were read; specify the commit described by this article.")
        value = explicit or next(iter(observed), "")
        if not value and previous_revision:
            value = previous_revision.get("verification", {}).get("repository_draft", {}).get("commit", "")
            if not value:
                from system.wiki.markdown_parser import parse_markdown_card
                value = parse_markdown_card(previous_revision["full_markdown"]).system_metadata.get("repository_research", {}).get("commit", "")
        if value and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            raise ValueError("Provide a full commit hash, or omit it when the source version is unknown.")
        return value
