from backend import deps


def test_summary_llm_uses_deepseek_official_client(monkeypatch):
    created = {}

    class FakeDeepSeekChat:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setattr(deps, "DeepSeekChat", FakeDeepSeekChat)
    deps.get_summary_llm.cache_clear()
    try:
        llm = deps.get_summary_llm()
    finally:
        deps.get_summary_llm.cache_clear()

    assert isinstance(llm, FakeDeepSeekChat)
    assert created == {
        "model": deps.DEEPSEEK_CHAT_MODEL,
        "temperature": 0.0,
        "max_tokens": 4096,
    }
