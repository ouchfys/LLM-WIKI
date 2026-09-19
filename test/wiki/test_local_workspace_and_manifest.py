import pytest

from system.wiki.local_workspace import LocalWikiWorkspace


def test_local_workspace_lists_searches_and_reads_only_markdown(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "a.md").write_text("# PagedAttention\nKV cache scheduling", encoding="utf-8")
    (root / "b.md").write_text("# KIVI\nKV cache quantization", encoding="utf-8")
    (root / "secret.txt").write_text("not exposed", encoding="utf-8")
    workspace = LocalWikiWorkspace(root)

    page = workspace.list_markdown(cursor=0, limit=1)
    assert page["total"] == 2
    assert page["next_cursor"] == 1
    assert workspace.search_markdown("quantization")[0]["path"] == "b.md"
    assert "PagedAttention" in workspace.read_markdown("a.md")["content"]
    with pytest.raises(ValueError):
        workspace.read_markdown("../outside.md")
    with pytest.raises(ValueError):
        workspace.read_markdown("secret.txt")


def test_corpus_manifest_is_stable_paginated_and_not_ranked(tmp_path, monkeypatch):
    from system.storage.object_storage import ObjectStorage
    from system.wiki import markdown_vault, wiki_store
    from system.wiki.wiki_store import WikiStore

    storage = ObjectStorage()
    storage.backend = "local"
    storage.repo_root = tmp_path
    monkeypatch.setattr(markdown_vault, "get_object_storage", lambda: storage)
    monkeypatch.setattr(wiki_store, "MarkdownVault", lambda: markdown_vault.MarkdownVault(str(tmp_path / "wiki")))
    store = WikiStore(str(tmp_path / "wiki.db"))
    for title in ("Zulu", "Alpha", "Middle"):
        store.create_card(title=title, page_type="PaperPage", summary=title, content_json={})

    first = store.corpus_manifest(cursor=0, limit=2)
    second = store.corpus_manifest(cursor=first["next_cursor"], limit=2)

    assert first["total"] == 3
    assert [item["title"] for item in first["items"]] == ["Alpha", "Middle"]
    assert [item["title"] for item in second["items"]] == ["Zulu"]
    assert second["next_cursor"] is None
