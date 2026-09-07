from __future__ import annotations

import pytest

from system.core import config
from system.storage.object_storage import ObjectStorage


def test_default_storage_uses_configured_tenant_prefix(monkeypatch) -> None:
    monkeypatch.setattr(config, "STORAGE_BACKEND", "oss")
    monkeypatch.setattr(config, "STORAGE_TENANT_ID", "fys")
    monkeypatch.setattr(config, "STORAGE_ROOT_PREFIX", "users/fys")
    monkeypatch.setattr(config, "OSS_BUCKET", "papersfys")

    storage = ObjectStorage()

    assert storage.tenant_id == "fys"
    assert storage.uri_for_key("sources/papers/example.pdf") == (
        "oss://papersfys/users/fys/sources/papers/example.pdf"
    )


def test_explicit_tenant_gets_an_independent_prefix(monkeypatch) -> None:
    monkeypatch.setattr(config, "STORAGE_BACKEND", "oss")
    monkeypatch.setattr(config, "STORAGE_TENANT_ID", "fys")
    monkeypatch.setattr(config, "STORAGE_ROOT_PREFIX", "users/fys")
    monkeypatch.setattr(config, "OSS_BUCKET", "papersfys")

    storage = ObjectStorage(tenant_id="Research-Team_2")

    assert storage.tenant_id == "research-team_2"
    assert storage.root_prefix == "users/research-team_2"
    assert storage.uri_for_key("wiki/topic.md").startswith(
        "oss://papersfys/users/research-team_2/"
    )
    assert storage.local_cache_path_for_key("wiki/topic.md") == (
        storage.repo_root / "tenants" / "research-team_2" / "wiki" / "topic.md"
    )


def test_tenant_storage_rejects_foreign_prefix_bucket_and_traversal(monkeypatch) -> None:
    monkeypatch.setattr(config, "STORAGE_BACKEND", "oss")
    monkeypatch.setattr(config, "STORAGE_TENANT_ID", "fys")
    monkeypatch.setattr(config, "STORAGE_ROOT_PREFIX", "users/fys")
    monkeypatch.setattr(config, "OSS_BUCKET", "papersfys")
    storage = ObjectStorage()

    with pytest.raises(ValueError, match="outside tenant"):
        storage.uri_for_key("users/another-user/wiki/private.md")
    with pytest.raises(ValueError, match="does not match"):
        storage.key_from_uri("oss://another-bucket/users/fys/wiki/page.md")
    with pytest.raises(ValueError, match="relative path"):
        storage.uri_for_key("wiki/../another-user/private.md")


@pytest.mark.parametrize("tenant_id", ["", "../admin", "a/b", "有空格"])
def test_invalid_tenant_ids_are_rejected(tenant_id: str) -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        ObjectStorage(tenant_id=tenant_id)


def test_mismatched_configured_prefix_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(config, "STORAGE_TENANT_ID", "fys")
    monkeypatch.setattr(config, "STORAGE_ROOT_PREFIX", "users/admin")
    with pytest.raises(ValueError, match="does not match tenant"):
        ObjectStorage()
