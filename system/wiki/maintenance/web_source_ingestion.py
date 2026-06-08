from __future__ import annotations

from pathlib import Path
from typing import Any

from system.storage import get_object_storage
from system.wiki.ingestion_jobs import IngestionJobStore
from system.wiki.maintenance.candidate_processor import MaintenanceCandidateProcessor
from system.wiki.maintenance.candidates import MaintenanceCandidateStore
from system.wiki.wiki_builder import sanitize_wiki_text


class WebSourceIngestionProcessor:
    """Consume queued web_source jobs into staged SourceNote candidates."""

    def __init__(self, db_path: str | Path | None = None):
        repo_root = Path(__file__).resolve().parents[3]
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self.jobs = IngestionJobStore(db_path=self.db_path)
        self.candidates = MaintenanceCandidateStore(db_path=self.db_path)
        self.storage = get_object_storage()

    def process_queued(self, *, limit: int = 10, auto_merge: bool = True) -> dict[str, Any]:
        jobs = [
            item for item in self.jobs.list_jobs(limit=max(limit * 3, limit))
            if item.get("source_type") == "web_source" and item.get("status") == "queued"
        ][:limit]
        results = []
        for job in jobs:
            results.append(self.process_job(job, auto_merge=auto_merge))
        return {"ok": True, "processed": len(results), "items": results}

    def process_job(self, job: dict[str, Any], *, auto_merge: bool = True) -> dict[str, Any]:
        job_id = str(job.get("id") or "")
        self.jobs.update_job(job_id, status="running", stage="source_note_candidate", progress=0.35)
        try:
            payload = self._candidate_payload(job)
            staged = self.candidates.add(
                source_type="web_source_ingestion",
                source_id=job_id,
                candidate_type="source_note",
                title=payload["title"],
                payload=payload,
                status="candidate_ready",
                upload=True,
            )
            merge_result = MaintenanceCandidateProcessor(db_path=self.db_path).process_pending(
                limit=20,
                auto_merge=auto_merge,
            )
            self.jobs.update_job(
                job_id,
                status="done",
                stage="done",
                progress=1.0,
                result={
                    "ok": True,
                    "candidate_id": staged["id"],
                    "candidate_artifact_uri": staged["artifact_uri"],
                    "maintenance_merge": merge_result,
                },
            )
            return {"job_id": job_id, "status": "done", "candidate_id": staged["id"]}
        except Exception as exc:
            self.jobs.update_job(job_id, status="failed", stage="failed", progress=1.0, error=str(exc))
            return {"job_id": job_id, "status": "failed", "error": str(exc)}

    def _candidate_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        source_uri = str(job.get("source_uri") or "")
        markdown = self.storage.read_text(source_uri) if source_uri else ""
        title = sanitize_wiki_text(str(metadata.get("source_title") or _title_from_markdown(markdown) or source_uri or "Web source"))
        source_url = str(metadata.get("source_url") or "")
        main_points = sanitize_wiki_text(markdown)[:1600]
        return {
            "status": "candidate_ready",
            "candidate_type": "source_note",
            "title": title[:180],
            "summary": sanitize_wiki_text(str(metadata.get("source_title") or title or "Web source candidate"))[:500],
            "content_json": {
                "schema_version": "web-source-note-v1",
                "source_type": metadata.get("source_type") or "web",
                "source_url": source_url,
                "source_candidate_uri": source_uri,
                "main_points": main_points,
                "useful_for": "Follow-up paper/source ingestion and wiki maintenance.",
                "notes": markdown[:3000],
                "expected_wiki_impact": metadata.get("expected_wiki_impact", ""),
                "confidence": metadata.get("confidence", ""),
                "topic": metadata.get("topic", ""),
            },
            "related_topics": [metadata.get("topic", "")] if metadata.get("topic") else [],
        }


def _title_from_markdown(markdown: str) -> str:
    for line in (markdown or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line.startswith("title:"):
            return line.split(":", 1)[1].strip().strip('"')
    return ""
