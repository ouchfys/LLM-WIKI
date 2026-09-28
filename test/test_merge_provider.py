import pytest
from backend import deps


@pytest.mark.parametrize("name", ["get_chat_llm", "get_summary_llm", "get_fast_llm", "get_review_llm", "get_merge_llm", "get_maintenance_llm", "get_maintenance_fast_llm"])
def test_all_generation_roles_use_official_provider(monkeypatch, name):
    calls = []
    monkeypatch.setattr(deps, "DeepSeekChat", lambda **kwargs: calls.append(kwargs) or kwargs)
    factory = getattr(deps, name)
    factory.cache_clear()
    try:
        result = factory()
        assert result["model"] == deps.DEEPSEEK_CHAT_MODEL
        assert len(calls) == 1
    finally:
        factory.cache_clear()


@pytest.mark.parametrize("enabled,key,expected", [(False, "test", False), (True, "", False), (True, "test", True)])
def test_embedding_factory_configuration(monkeypatch, enabled, key, expected):
    calls = []
    monkeypatch.setattr(deps, "WIKI_VECTOR_SEARCH_ENABLED", enabled)
    monkeypatch.setattr(deps, "BAILIAN_API_KEY", key)
    monkeypatch.setattr(deps, "BailianEmbeddings", lambda **kwargs: calls.append(kwargs) or object())
    deps.get_wiki_embeddings.cache_clear()
    try:
        assert (deps.get_wiki_embeddings() is not None) == expected
        assert len(calls) == int(expected)
    finally:
        deps.get_wiki_embeddings.cache_clear()
