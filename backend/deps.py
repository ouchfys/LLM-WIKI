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
from system.agent_runtime import AgentRunStore, ResearchSourceStore, ResearchTaskLedgerStore, TaskPlanStore
from system.search.resource_recommender import LearningResourceRecommender
from system.search.web_fetch import WebFetchTool
from system.search.web_search import WebSearchTool
from system.discovery.arxiv_service import ArxivService
from system.storage import get_storage_layout
from system.wiki.local_workspace import LocalWikiWorkspace
from system.core.config import (
    BAILIAN_API_KEY,
    WIKI_VECTOR_SEARCH_ENABLED,
    DEEPSEEK_CHAT_MODEL,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_MODE,
    WEB_SEARCH_TIMEOUT_SECONDS,
    WIKI_RRF_K,
)
from system.core.bailian_embeddings import BailianEmbeddings

try:
    from system.core.deepseek_client import DeepSeekChat
except Exception:
    DeepSeekChat = None


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
    """DeepSeek official model for Wiki tool use and final answers."""
    if DeepSeekChat is None:
        return None
    try:
        return DeepSeekChat(model=DEEPSEEK_CHAT_MODEL)
    except Exception as exc:
        print(f"[deps] Chat LLM unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_fast_llm():
    """Routing and lightweight extraction, using the official DeepSeek endpoint."""
    try:
        return DeepSeekChat(model=DEEPSEEK_CHAT_MODEL, temperature=0.0,
                            max_tokens=1800, max_retries=1)
    except Exception as exc:
        print(f"[deps] get_fast_llm unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_summary_llm():
    """DeepSeek official model for source-to-Wiki summarization and compilation."""
    if DeepSeekChat is None:
        return None
    try:
        return DeepSeekChat(model=DEEPSEEK_CHAT_MODEL, temperature=0.0, max_tokens=4096)
    except Exception as exc:
        print(f"[deps] Summary LLM unavailable: {exc}")
        return get_chat_llm()


@lru_cache(maxsize=1)
def get_review_llm():
    """Source evidence verification, using the official DeepSeek endpoint."""
    try:
        return DeepSeekChat(model=DEEPSEEK_CHAT_MODEL, temperature=0.0,
                            max_retries=1)
    except Exception as exc:
        print(f"[deps] get_review_llm unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_merge_llm():
    """Claim comparison and merge planning, using the official DeepSeek endpoint."""
    try:
        return DeepSeekChat(model=DEEPSEEK_CHAT_MODEL, temperature=0.0,
                            max_tokens=4096, max_retries=1)
    except Exception as exc:
        print(f"[deps] get_merge_llm unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_maintenance_llm():
    """Knowledge maintenance, using the official DeepSeek endpoint."""
    try:
        return DeepSeekChat(model=DEEPSEEK_CHAT_MODEL, temperature=0.0,
                            max_tokens=4096, max_retries=1)
    except Exception as exc:
        print(f"[deps] get_maintenance_llm unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_maintenance_fast_llm():
    """Maintenance planning, using the official DeepSeek endpoint."""
    try:
        return DeepSeekChat(model=DEEPSEEK_CHAT_MODEL, temperature=0.0,
                            max_tokens=1800, max_retries=1)
    except Exception as exc:
        print(f"[deps] get_maintenance_fast_llm unavailable: {exc}")
        return None


@lru_cache(maxsize=1)
def get_chunk_index() -> WikiChunkIndex:
    return WikiChunkIndex(db_path=get_session_store().db_path)


@lru_cache(maxsize=1)
def get_wiki_embeddings():
    """One shared, bounded client for Wiki and Claim semantic retrieval."""
    if not WIKI_VECTOR_SEARCH_ENABLED or not BAILIAN_API_KEY:
        return None
    return BailianEmbeddings(api_key=BAILIAN_API_KEY)


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
def get_arxiv_service() -> ArxivService:
    return ArxivService()


@lru_cache(maxsize=1)
def get_research_ledger() -> ResearchTaskLedgerStore:
    return ResearchTaskLedgerStore(db_path=get_wiki_store().db_path)


@lru_cache(maxsize=1)
def get_research_sources() -> ResearchSourceStore:
    return ResearchSourceStore(db_path=get_wiki_store().db_path)


@lru_cache(maxsize=1)
def get_task_plans() -> TaskPlanStore:
    return get_session_store().task_plans


@lru_cache(maxsize=1)
def get_local_wiki_workspace() -> LocalWikiWorkspace:
    return LocalWikiWorkspace(get_storage_layout().wiki_dir)


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
        memory_llm=get_chat_llm(),
        title_llm=get_chat_llm(),
        chunk_index=get_chunk_index(),
        wiki_resolver=get_wiki_resolver(),
        evidence_store=pipeline_store,
        runtime=runtime,
        arxiv_service=get_arxiv_service(),
        research_ledger=get_research_ledger(),
        research_sources=get_research_sources(),
        task_plans=get_task_plans(),
        local_workspace=get_local_wiki_workspace(),
    )
