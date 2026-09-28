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


def test_detects_flattened_tabular_ai_answer_without_inferring_provider(tmp_path):
    store = ResearchSourceStore(str(tmp_path / "sources.db"))
    text = """截至今天，可以把两种方案概括为不同的 KV cache 量化路线。以下结论仍需回到论文核验，不能直接进入知识库。

维度\t方案 A\t方案 B
定位\t端到端 serving 系统，包含权重与缓存量化。\t只处理 KV cache 的低比特插件。
量化粒度\t按通道缩放并配合系统内核。\tKey 按通道，Value 按 token。
收益口径\t报告端到端吞吐与服务成本。\t报告显存占用与可支持 batch。
适用场景\t大批量云端推理。\t长上下文且显存受限的推理。

关键限制是两者实验硬件、模型族和指标口径并不相同，因此不能直接横向比较。""" * 4

    detection = store.detect_pasted_content(text)

    assert detection["content_kind"] == "ai_answer"
    assert detection["provider"] == "unknown"
    assert detection["confidence"] >= 0.9
    result = store.auto_capture_if_ai_material(project_id="project-1", raw_text=text)
    assert result is not None
    assert result["source"]["source_type"] == "ai_answer"
    assert result["source"]["origin"] == "unknown"


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


def test_recapture_preserves_user_confirmed_provider(tmp_path):
    store = ResearchSourceStore(str(tmp_path / "sources.db"))
    raw = "Assistant: This is a sufficiently long answer about KV cache quantization. " * 4
    first = store.capture(
        project_id="project-1", raw_text=raw, source_type="ai_answer", origin="Kimi",
    )

    repeated = store.capture(
        project_id="project-1", raw_text=raw, source_type="unknown", origin="unknown",
    )

    assert first["source"]["origin"] == "Kimi"
    assert repeated["source"]["origin"] == "Kimi"
    assert repeated["source"]["source_type"] == "ai_answer"
    assert repeated["source"]["metadata"]["provider_confirmed_by_user"] is True
