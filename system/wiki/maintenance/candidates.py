from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.storage import get_object_storage, get_storage_layout
from system.wiki.maintenance.store import WikiMaintenanceStore


class MaintenanceCandidateStore:
    """Stage maintenance candidates without mutating official wiki pages."""

    def __init__(self, db_path: str | Path | None = None):
        self.store = WikiMaintenanceStore(db_path=db_path)
        self.layout = get_storage_layout()
        self.storage = get_object_storage()

    def add(
        self,
        *,
        source_type: str,
        candidate_type: str,
        title: str,
        payload: dict[str, Any],
        source_id: str = "",
        status: str = "candidate_ready",
        upload: bool = True,
    ) -> dict[str, Any]:
        candidate_id = str(payload.get("candidate_id") or "")
        if not candidate_id:
            candidate_id = self.store.add_candidate(
                source_type=source_type,
                source_id=source_id,
                candidate_type=candidate_type,
                title=title,
                payload=payload,
                status=status,
                artifact_uri="",
            )
        else:
            self.store.add_candidate(
                source_type=source_type,
                source_id=source_id,
                candidate_type=candidate_type,
                title=title,
                payload=payload,
                status=status,
                artifact_uri="",
            )
        payload["candidate_id"] = candidate_id
        artifact_uri = self.write_artifact(candidate_id, payload, upload=upload)
        self.store.update_candidate(candidate_id, artifact_uri=artifact_uri, payload=payload)
        return {
            "id": candidate_id,
            "status": status,
            "source_type": source_type,
            "source_id": source_id,
            "candidate_type": candidate_type,
            "title": title,
            "artifact_uri": artifact_uri,
            "payload": payload,
        }

    def write_artifact(self, candidate_id: str, payload: dict[str, Any], *, upload: bool = True) -> str:
        path = self.layout.maintenance_artifact_path("candidates", candidate_id, ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        path.write_text(text, encoding="utf-8")
        key = self.storage.key_for_local_path(path)
        if not upload:
            return f"local://{key}"
        return self.storage.upload_text(
            key,
            text,
            content_type="application/json; charset=utf-8",
        )
