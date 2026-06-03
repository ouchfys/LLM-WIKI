from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.deps import (
    get_learning_profile, get_monthly_reads, get_profile_builder,
    get_recommender, get_wiki_store,
)
from system.memory.learning_profile import LearningProfileStore
from system.discovery.agent_crawler import AgentCrawler
from system.recommender.monthly_reads import MonthlyReadingStore
from system.recommender.profile_builder import ProfileBuilder
from system.recommender.item_scorer import ProfileAwareRecommender
from system.wiki.raw_source_vault import RawSourceVault
from system.wiki.wiki_store import WikiStore


router = APIRouter()


class NotesPayload(BaseModel):
    note_summary: str = ""
    takeaways: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)
    interview_points: List[str] = Field(default_factory=list)
    deep_read_worthy: bool = False


class StatusPayload(BaseModel):
    status: str


class CrawlPayload(BaseModel):
    topics: List[str] = Field(default_factory=list)
    include_arxiv: bool = True
    include_feeds: bool = True
    per_query_limit: int = 6
    max_items: int = 16


DEMO_READING_ITEMS = [
    {
        "id": "demo-attention-is-all-you-need",
        "title": "Attention Is All You Need",
        "url": "https://arxiv.org/abs/1706.03762",
        "summary": "Transformer 架构的基础论文，提出完全基于 self-attention 的序列建模方式，是 LLM、RAG 和 Agent 系统的重要底座。",
        "source_type": "paper",
        "source_level": "primary",
        "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"],
        "year": 2017,
        "venue": "NeurIPS",
    },
    {
        "id": "demo-rag-neurips-2020",
        "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
        "url": "https://arxiv.org/abs/2005.11401",
        "summary": "RAG 经典论文，把参数化生成模型和非参数化检索记忆结合起来，适合作为项目答辩中的核心背景材料。",
        "source_type": "paper",
        "source_level": "primary",
        "authors": ["Patrick Lewis", "Ethan Perez", "Aleksandra Piktus"],
        "year": 2020,
        "venue": "NeurIPS",
    },
    {
        "id": "demo-graphrag",
        "title": "From Local to Global: A Graph RAG Approach to Query-Focused Summarization",
        "url": "https://arxiv.org/abs/2404.16130",
        "summary": "GraphRAG 代表性工作，强调用图结构组织实体、社区和证据，适合解释本项目为什么保留 GraphRAG 精读能力。",
        "source_type": "paper",
        "source_level": "primary",
        "authors": ["Darren Edge", "Ha Trinh", "Newman Cheng"],
        "year": 2024,
        "venue": "arXiv",
    },
    {
        "id": "demo-agent-memory",
        "title": "A Survey on the Memory Mechanism of Large Language Model based Agents",
        "url": "https://arxiv.org/abs/2404.13501",
        "summary": "Agent 记忆机制综述，可用于完善用户画像、会话记忆、偏好记忆和长期学习轨迹的设计说明。",
        "source_type": "paper",
        "source_level": "primary",
        "authors": ["LLM Agent Memory Survey Authors"],
        "year": 2024,
        "venue": "arXiv",
    },
    {
        "id": "demo-ragas",
        "title": "RAGAS: Automated Evaluation of Retrieval Augmented Generation",
        "url": "https://arxiv.org/abs/2309.15217",
        "summary": "RAG 评估框架，覆盖 faithfulness、answer relevancy、context precision 等指标，适合补齐面试中的评估话题。",
        "source_type": "paper",
        "source_level": "primary",
        "authors": ["Shahul Es", "Jithin James", "Luis Espinosa-Anke"],
        "year": 2023,
        "venue": "arXiv",
    },
]


def _content_for_wiki(item: dict) -> dict:
    metadata = item.get("metadata", {})
    notes = []
    if item.get("note_summary"):
        notes.append(f"Summary: {item['note_summary']}")
    if item.get("takeaways"):
        notes.append("Takeaways:\n" + "\n".join(f"- {x}" for x in item["takeaways"]))
    if item.get("open_questions"):
        notes.append("Open questions:\n" + "\n".join(f"- {x}" for x in item["open_questions"]))
    if item.get("interview_points"):
        notes.append("Interview points:\n" + "\n".join(f"- {x}" for x in item["interview_points"]))
    note_text = "\n\n".join(notes) or "Saved from Monthly Reads."

    if item.get("source_type") == "paper":
        return {
            "authors": ", ".join(metadata.get("authors", [])),
            "year": str(metadata.get("year") or ""),
            "venue": metadata.get("venue") or "",
            "key_contributions": "",
            "methods": "",
            "results": item.get("summary", ""),
            "notes": note_text,
        }
    return {
        "source_type": item.get("source_type", ""),
        "main_points": item.get("summary", ""),
        "useful_for": "",
        "notes": note_text,
    }


def _write_recommendation_raw_source(item: dict) -> str:
    lines = [f"# {item.get('title', '')}", "", "## Source", ""]
    if item.get("url"):
        lines.append(f"- URL: {item['url']}")
    lines.extend(["", "## Summary", "", item.get("summary", "") or "(empty)", ""])
    if item.get("note_summary"):
        lines.extend(["## My note", "", item["note_summary"], ""])
    for key, label in [
        ("takeaways", "Takeaways"),
        ("open_questions", "Open questions"),
        ("interview_points", "Interview points"),
    ]:
        values = item.get(key) or []
        if values:
            lines.extend([f"## {label}", ""])
            lines.extend(f"- {value}" for value in values)
            lines.append("")
    return RawSourceVault().write_source(
        source_kind="recommendation",
        title=item.get("title", "") or "recommendation",
        body_markdown="\n".join(lines),
        metadata={
            "source_type": item.get("source_type", ""),
            "source_level": item.get("source_level", ""),
            "score": item.get("score", 0),
        },
        source_urls=[item["url"]] if item.get("url") else [],
        slug_hint=item.get("source_id") or item.get("id") or item.get("url", ""),
    )


@router.get("")
def list_monthly_reads(
    month: Optional[str] = None,
    status: str = "all",
    store: MonthlyReadingStore = Depends(get_monthly_reads),
):
    active_month = month or store.current_month()
    return {
        "month": active_month,
        "counts": store.count_by_status(active_month),
        "items": store.list_items(month=active_month, status=status, limit=200),
    }


@router.post("/seed-demo")
def seed_demo_reads(
    store: MonthlyReadingStore = Depends(get_monthly_reads),
):
    month = store.current_month()
    created = 0
    for index, item in enumerate(DEMO_READING_ITEMS):
        before = store.find_existing(source_id=item["id"], url=item["url"], month=month)
        store.add_source_item(
            item,
            month=month,
            score=9.5 - index * 0.4,
            reasons=[
                "适合项目展示",
                "适合面试追问",
                "和 RAG / Agent / LLM Wiki 主线相关",
            ],
        )
        if not before:
            created += 1
    return {"ok": True, "created": created, "month": month}


@router.post("/crawl")
def crawl_sources(
    payload: CrawlPayload,
    store: MonthlyReadingStore = Depends(get_monthly_reads),
    profile: LearningProfileStore = Depends(get_learning_profile),
    profile_builder: ProfileBuilder = Depends(get_profile_builder),
    recommender: ProfileAwareRecommender = Depends(get_recommender),
):
    crawler = AgentCrawler(
        reading_store=store,
        learning_profile=profile,
        profile_builder=profile_builder,
        recommender=recommender,
    )
    report = crawler.crawl_to_monthly_reads(
        topics=payload.topics,
        include_arxiv=payload.include_arxiv,
        include_feeds=payload.include_feeds,
        per_query_limit=max(1, min(payload.per_query_limit, 10)),
        max_items=max(1, min(payload.max_items, 50)),
    )
    return {
        "ok": True,
        "created": report.created,
        "discovered": report.discovered,
        "ranked": report.ranked,
        "queries": report.queries,
        "sources": report.sources,
        "month": store.current_month(),
    }


@router.patch("/{item_id}/status")
def update_status(
    item_id: str,
    payload: StatusPayload,
    store: MonthlyReadingStore = Depends(get_monthly_reads),
    profile: LearningProfileStore = Depends(get_learning_profile),
):
    item = store.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Reading item not found")
    store.update_status(item_id, payload.status)
    profile.log_recommendation_feedback(
        item_id=item_id,
        item_type=item.get("source_type", ""),
        action=payload.status,
        reason="Status updated from web UI",
        metadata={"title": item.get("title", ""), "summary": item.get("summary", "")},
    )
    return {"ok": True}


@router.patch("/{item_id}/notes")
def update_notes(
    item_id: str,
    payload: NotesPayload,
    store: MonthlyReadingStore = Depends(get_monthly_reads),
):
    if not store.get_item(item_id):
        raise HTTPException(status_code=404, detail="Reading item not found")
    store.update_notes(
        item_id,
        note_summary=payload.note_summary,
        takeaways=payload.takeaways,
        open_questions=payload.open_questions,
        interview_points=payload.interview_points,
        deep_read_worthy=payload.deep_read_worthy,
    )
    return {"ok": True}


@router.post("/{item_id}/save-wiki")
def save_to_wiki(
    item_id: str,
    store: MonthlyReadingStore = Depends(get_monthly_reads),
    wiki_store: WikiStore = Depends(get_wiki_store),
    profile: LearningProfileStore = Depends(get_learning_profile),
):
    item = store.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Reading item not found")
    existing = wiki_store.find_duplicate(
        title=item["title"],
        page_type="PaperPage" if item.get("source_type") == "paper" else "SourceNote",
        source_urls=[item["url"]] if item.get("url") else [],
    )
    raw_source_path = _write_recommendation_raw_source(item)
    content_json = _content_for_wiki(item)
    content_json["raw_source_path"] = raw_source_path
    card_id = wiki_store.create_card(
        title=item["title"],
        page_type="PaperPage" if item.get("source_type") == "paper" else "SourceNote",
        content_json=content_json,
        summary=item.get("summary", "")[:240],
        source_level=item.get("source_level", ""),
        source_urls=[item["url"]] if item.get("url") else [],
        related_topics=[],
    )
    from backend.deps import get_chunk_index
    get_chunk_index().reindex_card(
        card_id=card_id,
        raw_source_path=raw_source_path,
        source_kind=item.get("source_type", "recommendation"),
    )
    store.update_status(item_id, "saved")
    profile.log_recommendation_feedback(
        item_id=item_id,
        item_type=item.get("source_type", ""),
        action="saved_to_wiki",
        reason="Saved from web UI",
        metadata={"title": item.get("title", ""), "summary": item.get("summary", "")},
    )
    return {"ok": True, "card_id": card_id, "deduped": bool(existing)}
