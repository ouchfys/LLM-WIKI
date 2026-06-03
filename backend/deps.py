from functools import lru_cache

from system.memory.learning_profile import LearningProfileStore
from system.memory.session_store import SessionStore
from system.recommender.monthly_reads import MonthlyReadingStore
from system.recommender.profile_builder import ProfileBuilder
from system.recommender.item_scorer import ProfileAwareRecommender
from system.paper_index.store import PaperIndexStore
from system.wiki.wiki_store import WikiStore
from system.wiki.wiki_chat import WikiChatService
from system.wiki.chunk_index import WikiChunkIndex
from system.search.resource_recommender import LearningResourceRecommender
from system.search.web_search import WebSearchTool
from system.core.config import (
    SILICONFLOW_FAST_MODEL,
    SILICONFLOW_MERGE_MODEL,
    SILICONFLOW_REVIEW_MODEL,
    SILICONFLOW_SUMMARY_MODEL,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_MODE,
    WEB_SEARCH_TIMEOUT_SECONDS,
)

try:
    from system.core.siliconflow_client import SiliconFlowChat
except Exception:
    SiliconFlowChat = None


@lru_cache(maxsize=1)
def get_session_store() -> SessionStore:
    return SessionStore()


@lru_cache(maxsize=1)
def get_learning_profile() -> LearningProfileStore:
    store = get_session_store()
    return LearningProfileStore(store, db_path=store.db_path)


@lru_cache(maxsize=1)
def get_monthly_reads() -> MonthlyReadingStore:
    store = get_session_store()
    return MonthlyReadingStore(db_path=store.db_path)


@lru_cache(maxsize=1)
def get_wiki_store() -> WikiStore:
    store = get_session_store()
    return WikiStore(db_path=store.db_path)


@lru_cache(maxsize=1)
def get_paper_index() -> PaperIndexStore:
    store = get_session_store()
    return PaperIndexStore(db_path=store.db_path)


@lru_cache(maxsize=1)
def get_chat_llm():
    """主力模型: Wiki Chat / 面试评估 / 论文发现排序"""
    if SiliconFlowChat is None:
        return None
    try:
        return SiliconFlowChat()
    except Exception as exc:
        print(f"[deps] Chat LLM unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_fast_llm():
    """轻量模型: 意图路由 / 偏好抽取 / 简单分类"""
    if SiliconFlowChat is None:
        return None
    try:
        return SiliconFlowChat(model=SILICONFLOW_FAST_MODEL)
    except Exception as exc:
        print(f"[deps] Fast LLM unavailable: {exc}")
        # 回退到主力模型
        return get_chat_llm()


@lru_cache(maxsize=1)
def get_summary_llm():
    """Dedicated model for source-to-Wiki summarization and paper compilation."""
    if SiliconFlowChat is None:
        return None
    try:
        return SiliconFlowChat(model=SILICONFLOW_SUMMARY_MODEL, temperature=0.0, max_tokens=4096)
    except Exception as exc:
        print(f"[deps] Summary LLM unavailable: {exc}")
        return get_fast_llm()


@lru_cache(maxsize=1)
def get_review_llm():
    """Dedicated deterministic reviewer model for paper candidates."""
    if SiliconFlowChat is None:
        return None
    try:
        return SiliconFlowChat(model=SILICONFLOW_REVIEW_MODEL, temperature=0.0, max_tokens=2200)
    except Exception as exc:
        print(f"[deps] Review LLM unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_merge_llm():
    """Dedicated deterministic merge-planning model for paper cards."""
    if SiliconFlowChat is None:
        return None
    try:
        return SiliconFlowChat(model=SILICONFLOW_MERGE_MODEL, temperature=0.0, max_tokens=3600)
    except Exception as exc:
        print(f"[deps] Merge LLM unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_chunk_index() -> WikiChunkIndex:
    return WikiChunkIndex(db_path=get_session_store().db_path)


@lru_cache(maxsize=1)
def get_web_search() -> WebSearchTool:
    return WebSearchTool(
        mode=WEB_SEARCH_MODE,
        timeout_seconds=WEB_SEARCH_TIMEOUT_SECONDS,
        max_results=WEB_SEARCH_MAX_RESULTS,
    )


@lru_cache(maxsize=1)
def get_resource_recommender() -> LearningResourceRecommender:
    return LearningResourceRecommender(web_search=get_web_search())


@lru_cache(maxsize=1)
def get_profile_builder() -> ProfileBuilder:
    return ProfileBuilder(
        learning_profile=get_learning_profile(),
        session_store=get_session_store(),
    )


@lru_cache(maxsize=1)
def get_recommender() -> ProfileAwareRecommender:
    return ProfileAwareRecommender(
        learning_profile=get_learning_profile(),
        profile_builder=get_profile_builder(),
    )


@lru_cache(maxsize=1)
def get_wiki_chat() -> WikiChatService:
    return WikiChatService(
        wiki_store=get_wiki_store(),
        learning_profile=get_learning_profile(),
        session_store=get_session_store(),
        llm=get_chat_llm(),
        chunk_index=get_chunk_index(),
        web_search=get_web_search(),
        resource_recommender=get_resource_recommender(),
    )
