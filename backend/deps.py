from functools import lru_cache

from system.memory.learning_profile import LearningProfileStore
from system.conversation.session_store import SessionStore
from system.recommender.monthly_reads import MonthlyReadingStore
from system.recommender.profile_builder import ProfileBuilder
from system.recommender.item_scorer import ProfileAwareRecommender
from system.paper_index.store import PaperIndexStore
from system.wiki.wiki_store import WikiStore
from system.wiki.wiki_chat import WikiChatService
from system.wiki.wiki_resolver import WikiResolver
from system.wiki.chunk_index import WikiChunkIndex
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.table_qa import TableQuestionAnswerer
from system.agent_runtime import AgentRunStore
from system.search.resource_recommender import LearningResourceRecommender
from system.search.web_fetch import WebFetchTool
from system.search.web_search import WebSearchTool
from system.core.config import (
    DEEPSEEK_CHAT_MODEL,
    SILICONFLOW_FAST_MODEL,
    SILICONFLOW_MAINTENANCE_FAST_MODEL,
    SILICONFLOW_MAINTENANCE_MODEL,
    SILICONFLOW_MERGE_MODEL,
    SILICONFLOW_REVIEW_MODEL,
    SILICONFLOW_SUMMARY_MODEL,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_MODE,
    WEB_SEARCH_TIMEOUT_SECONDS,
    WIKI_EMBEDDING_BATCH_SIZE,
    WIKI_EMBEDDING_MODEL,
    WIKI_RRF_K,
    WIKI_VECTOR_SEARCH_ENABLED,
)

try:
    from system.core.siliconflow_client import DeepSeekChat, SiliconFlowChat, SiliconFlowEmbeddings
except Exception:
    DeepSeekChat = None
    SiliconFlowChat = None
    SiliconFlowEmbeddings = None


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
    """DeepSeek official model for Wiki tool use, table QA, and final answers."""
    if DeepSeekChat is None:
        return None
    try:
        return DeepSeekChat(model=DEEPSEEK_CHAT_MODEL)
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
def get_maintenance_llm():
    """Stronger maintenance model for semantic repair and web-update judgment."""
    if SiliconFlowChat is None:
        return None
    try:
        return SiliconFlowChat(model=SILICONFLOW_MAINTENANCE_MODEL, temperature=0.0, max_tokens=3200)
    except Exception as exc:
        print(f"[deps] Maintenance LLM unavailable: {exc}")
        return get_review_llm()


@lru_cache(maxsize=1)
def get_maintenance_fast_llm():
    """Cheap maintenance model for planning and controlled-vocabulary routing."""
    if SiliconFlowChat is None:
        return None
    try:
        return SiliconFlowChat(model=SILICONFLOW_MAINTENANCE_FAST_MODEL, temperature=0.0, max_tokens=1800)
    except Exception as exc:
        print(f"[deps] Maintenance fast LLM unavailable: {exc}")
        return get_fast_llm()


@lru_cache(maxsize=1)
def get_chunk_index() -> WikiChunkIndex:
    return WikiChunkIndex(db_path=get_session_store().db_path)


@lru_cache(maxsize=1)
def get_wiki_embeddings():
    if not WIKI_VECTOR_SEARCH_ENABLED or SiliconFlowEmbeddings is None:
        return None
    try:
        return SiliconFlowEmbeddings(
            model=WIKI_EMBEDDING_MODEL,
            batch_size=WIKI_EMBEDDING_BATCH_SIZE,
        )
    except Exception as exc:
        print(f"[deps] Wiki embeddings unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_wiki_resolver() -> WikiResolver:
    return WikiResolver(
        get_wiki_store(),
        embedder=get_wiki_embeddings(),
        rrf_k=WIKI_RRF_K,
    )


@lru_cache(maxsize=1)
def get_web_search() -> WebSearchTool:
    return WebSearchTool(
        mode=WEB_SEARCH_MODE,
        timeout_seconds=WEB_SEARCH_TIMEOUT_SECONDS,
        max_results=WEB_SEARCH_MAX_RESULTS,
    )


@lru_cache(maxsize=1)
def get_web_fetch() -> WebFetchTool:
    return WebFetchTool(timeout_seconds=max(WEB_SEARCH_TIMEOUT_SECONDS, 8))


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
    pipeline_store = PaperWikiPipelineStore(db_path=get_wiki_store().db_path)
    runtime = AgentRunStore(db_path=get_wiki_store().db_path)
    return WikiChatService(
        wiki_store=get_wiki_store(),
        learning_profile=get_learning_profile(),
        session_store=get_session_store(),
        llm=get_chat_llm(),
        chunk_index=get_chunk_index(),
        web_search=get_web_search(),
        web_fetch=get_web_fetch(),
        resource_recommender=get_resource_recommender(),
        wiki_resolver=get_wiki_resolver(),
        evidence_store=pipeline_store,
        runtime=runtime,
        table_qa=TableQuestionAnswerer(
            pipeline_store,
            llm=get_chat_llm(),
        ),
    )
