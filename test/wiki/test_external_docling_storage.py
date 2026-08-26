import sqlite3

from system.storage.object_storage import ObjectStorage
from system.storage import object_storage as object_storage_module
from system.wiki.paper_pipeline.models import SourcePacket
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore


def test_docling_json_is_externalized_and_checksum_verified(tmp_path, monkeypatch):
    storage = ObjectStorage()
    storage.backend = "local"
    storage.root_prefix = ""
    storage.repo_root = tmp_path
    monkeypatch.setattr(object_storage_module, "_STORAGE", storage)

    db_path = str(tmp_path / "evidence.sqlite")
    store = PaperWikiPipelineStore(db_path=db_path)
    packet = SourcePacket(
        source_id="packet-1",
        title="External evidence",
        parser_used="docling-remote",
        source_hash="abc123",
        docling_json={"pages": {"1": {"size": {"width": 100, "height": 200}}}},
    )

    store.upsert_source_packet(packet)

    with sqlite3.connect(db_path) as conn:
        inline, uri, digest, size = conn.execute(
            """SELECT docling_json, docling_json_uri, docling_json_hash, docling_json_size
               FROM source_documents WHERE source_packet_id = 'packet-1'"""
        ).fetchone()
    assert inline == "{}"
    assert uri.startswith("local://sources/papers/docling/")
    assert len(digest) == 64
    assert size > 0
    assert store.load_source_document_json("packet-1") == packet.docling_json
