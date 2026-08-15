import sqlite3

from system.wiki.wiki_chat import AgentToolCall, WikiChatService
from system.wiki.wiki_resolver import WikiResolver, lookup_terms, normalize_lookup
from system.wiki.wiki_store import WikiStore


def _build_store(tmp_path):
    store = WikiStore(str(tmp_path / "wiki.sqlite"))
    grpo_id = store.create_card(
        title="Group Relative Policy Optimization (GRPO)",
        page_type="MethodPage",
        summary="A policy optimization method using relative rewards within a sampled group.",
        content_json={"description": "GRPO removes the separate value model."},
        related_topics=["reinforcement learning", "policy optimization"],
    )
    attention_id = store.create_card(
        title="Self-Attention",
        page_type="ConceptPage",
        summary="Token representations attend to other positions in the same sequence.",
        content_json={"definition": "Scaled dot-product attention over one sequence."},
        related_topics=["Transformer"],
    )
    unrelated_id = store.create_card(
        title="Random Forest",
        page_type="MethodPage",
        summary="An ensemble of decision trees.",
        content_json={"description": "Bagging and feature subsampling."},
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """CREATE TABLE wiki_aliases (
                   card_id TEXT NOT NULL,
                   alias TEXT NOT NULL,
                   normalized_alias TEXT NOT NULL
               )"""
        )
        conn.executemany(
            "INSERT INTO wiki_aliases(card_id, alias, normalized_alias) VALUES (?, ?, ?)",
            [
                (grpo_id, "GRPO", normalize_lookup("GRPO")),
                (attention_id, "自注意力", normalize_lookup("自注意力")),
            ],
        )
    return store, grpo_id, attention_id, unrelated_id


def test_resolver_prefers_exact_alias_and_explains_match(tmp_path):
    store, grpo_id, _, _ = _build_store(tmp_path)

    results = WikiResolver(store).resolve("GRPO", limit=3)

    assert results[0]["card_id"] == grpo_id
    assert results[0]["matched_alias"] == "GRPO"
    assert "alias_exact" in results[0]["match_reasons"]
    assert results[0]["score"] >= 100


def test_resolver_handles_cjk_alias_inside_natural_question(tmp_path):
    store, _, attention_id, unrelated_id = _build_store(tmp_path)

    results = WikiResolver(store).resolve("请解释一下自注意力是如何工作的", limit=2)

    assert results[0]["card_id"] == attention_id
    assert "alias_mentioned" in results[0]["match_reasons"]
    assert unrelated_id not in {item["card_id"] for item in results}


def test_lookup_terms_create_cjk_bigrams_without_extra_dependency():
    terms = lookup_terms("强化学习中的策略优化")

    assert "强化" in terms
    assert "策略" in terms
    assert "优化" in terms


def test_wiki_search_returns_bounded_resolutions_not_full_catalog(tmp_path):
    store, grpo_id, _, _ = _build_store(tmp_path)
    service = WikiChatService(store, wiki_resolver=WikiResolver(store))
    cards = []

    observation = service._execute_agent_tool_call(
        AgentToolCall(name="wiki_search", arguments={"query": "GRPO", "limit": 1}),
        cards=cards,
        web_results=[],
        resources=[],
        limit=6,
    )

    assert observation.status == "done"
    assert observation.summary == "resolved 1 compiled Wiki pages"
    assert len(observation.items) == 1
    assert observation.items[0]["card_id"] == grpo_id
    assert observation.items[0]["match_reason"]
    assert cards == []


def test_wiki_card_query_fallback_uses_resolver_and_opens_page(tmp_path):
    store, grpo_id, _, _ = _build_store(tmp_path)
    service = WikiChatService(store, wiki_resolver=WikiResolver(store))

    opened = service._open_cards(
        AgentToolCall(name="wiki_card", arguments={"query": "GRPO", "limit": 1}),
        query="GRPO",
        call_limit=1,
    )

    assert [card["id"] for card in opened] == [grpo_id]
    assert opened[0]["_resolution"]["matched_alias"] == "GRPO"
    assert "value model" in opened[0]["_full_text"]


def test_opened_page_exposes_bounded_bidirectional_wiki_links(tmp_path):
    store, grpo_id, attention_id, _ = _build_store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """CREATE TABLE wiki_card_links (
                   id TEXT PRIMARY KEY,
                   from_card_id TEXT NOT NULL,
                   to_card_id TEXT NOT NULL,
                   relation_type TEXT NOT NULL,
                   source_packet_id TEXT DEFAULT '',
                   evidence_text TEXT DEFAULT '',
                   created_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO wiki_card_links
               (id, from_card_id, to_card_id, relation_type, created_at)
               VALUES ('link-1', ?, ?, 'compares_with', '2026-08-13T00:00:00Z')""",
            (grpo_id, attention_id),
        )
    service = WikiChatService(store, wiki_resolver=WikiResolver(store))

    opened = service._open_cards(
        AgentToolCall(name="wiki_card", arguments={"card_ids": [attention_id], "limit": 1}),
        query="",
        call_limit=1,
    )

    assert opened[0]["_linked_pages"] == [{
        "card_id": grpo_id,
        "title": "Group Relative Policy Optimization (GRPO)",
        "page_type": "MethodPage",
        "relation_type": "compares_with",
        "direction": "incoming",
    }]
