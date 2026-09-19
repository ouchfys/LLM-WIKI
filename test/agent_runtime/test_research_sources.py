from system.agent_runtime.research_sources import ResearchSourceStore


def test_detects_and_preserves_unlabelled_provider_ai_conversation(tmp_path):
    store = ResearchSourceStore(str(tmp_path / "sources.db"))
    text = """User: 请比较两种 KV Cache 量化方案。

Assistant: 第一种方案强调逐通道量化，并报告了较低误差。

User: 它是否需要校准数据？

Assistant: 根据这段回答，需要少量校准样本，但还应回到论文原文核验。
"""

    detection = store.detect_pasted_content(text)
    assert detection["content_kind"] == "ai_conversation"
    assert detection["provider"] == "unknown"
    assert detection["confidence"] >= 0.9

    result = store.auto_capture_if_ai_conversation(project_id="project-1", raw_text=text)
    assert result is not None
    assert result["source"]["raw_text"] == text
    assert result["source"]["origin"] == "unknown"
    assert result["source"]["source_type"] == "ai_conversation"
    original_hash = result["source"]["content_hash"]
    corrected = store.correct_source(
        result["source"]["id"], source_type="ai_conversation", origin="Claude",
    )
    assert corrected["origin"] == "Claude"
    assert corrected["content_hash"] == original_hash


def test_article_with_single_question_marker_is_not_high_confidence_ai_chat(tmp_path):
    store = ResearchSourceStore(str(tmp_path / "sources.db"))
    text = "Q: Why quantize the KV cache? This article explains memory bandwidth and serving cost. " * 4

    detection = store.detect_pasted_content(text)

    assert detection["content_kind"] == "unknown"
    assert store.auto_capture_if_ai_conversation(project_id="project-1", raw_text=text) is None


def test_candidate_claims_stay_out_of_accepted_and_preserve_uncertain_dispute(tmp_path):
    store = ResearchSourceStore(str(tmp_path / "sources.db"))
    first = store.capture(
        project_id="project-1", raw_text="Assistant: KIVI uses 2-bit KV cache.",
        source_type="ai_answer",
        claims=[{"subject": "KIVI", "aspect": "quantization_bits", "scope": {}, "statement": "KIVI uses 2-bit KV cache."}],
    )
    second = store.capture(
        project_id="project-1", raw_text="Assistant: KIVI uses 4-bit KV cache.",
        source_type="ai_answer",
        claims=[{"subject": "KIVI", "aspect": "quantization_bits", "scope": {}, "statement": "KIVI uses 4-bit KV cache."}],
    )

    assert first["claims"][0]["status"] == "candidate"
    assert second["claims"][0]["status"] == "disputed"
    assert "# Disputed Claims" in second["snapshot"]["markdown"]
    assert "KIVI uses 2-bit" in second["snapshot"]["markdown"]
    assert "KIVI uses 4-bit" in second["snapshot"]["markdown"]
