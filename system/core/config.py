"""
统一配置模块

从 .env 文件加载所有配置项，为整个项目提供统一的配置入口。
所有模块都应该从这里导入配置，而不是硬编码。

使用方式:
    from system.core.config import (
        SILICONFLOW_API_KEY,
        SILICONFLOW_CHAT_MODEL,
        SILICONFLOW_FAST_MODEL,
        ...
    )
"""

import os
import sys
from pathlib import Path


def _load_dotenv(env_path: str = None):
    """手动解析 .env 文件 (避免引入额外依赖 python-dotenv)"""
    if env_path is None:
        current_dir = Path(__file__).resolve().parent
        candidates = [
            current_dir / ".env",
            current_dir.parent / ".env",
            current_dir.parent.parent / ".env",
        ]
        for candidate in candidates:
            if candidate.exists():
                env_path = str(candidate)
                break

    if not env_path or not os.path.exists(env_path):
        print("[Config] 未找到 .env 文件，使用环境变量或默认值", file=sys.stderr)
        return

    print(f"[Config] 加载配置文件: {env_path}", file=sys.stderr)

    parsed = {}
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                parsed[key] = value

    for key, value in parsed.items():
        if key not in os.environ:
            os.environ[key] = value


# ========== 加载 .env ==========
_load_dotenv()

# ========== 硅基流动 API 配置 ==========

SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")

SILICONFLOW_BASE_URL = os.environ.get(
    "SILICONFLOW_BASE_URL",
    "https://api.siliconflow.cn/v1",
)

SILICONFLOW_CHAT_URL = f"{SILICONFLOW_BASE_URL}/chat/completions"

# ========== DeepSeek official API configuration ==========

# ``deepseek_api`` is retained as a compatibility alias for the existing local
# .env. New deployments should use the conventional uppercase variable name.
DEEPSEEK_API_KEY = (
    os.environ.get("DEEPSEEK_API_KEY", "")
    or os.environ.get("deepseek_api", "")
)
DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL",
    "https://api.deepseek.com",
).rstrip("/")
DEEPSEEK_CHAT_URL = f"{DEEPSEEK_BASE_URL}/chat/completions"
DEEPSEEK_CHAT_MODEL = os.environ.get(
    "DEEPSEEK_CHAT_MODEL",
    "deepseek-v4-flash",
)

# ========== 模型分級配置 ==========

# 主力模型: Wiki Chat / 面试评估 / 论文发现排序
SILICONFLOW_CHAT_MODEL = os.environ.get(
    "SILICONFLOW_CHAT_MODEL",
    "deepseek-ai/DeepSeek-V3",
)

# 轻量模型: 意图路由 / 偏好抽取 / 简单分类
SILICONFLOW_FAST_MODEL = os.environ.get(
    "SILICONFLOW_FAST_MODEL",
    "Qwen/Qwen3.5-9B",
)

# Wiki / paper / source summarization model. Kept separate from the fast router
# model so ingestion quality can be raised without changing chat/routing.
SILICONFLOW_SUMMARY_MODEL = os.environ.get(
    "SILICONFLOW_SUMMARY_MODEL",
    "deepseek-ai/DeepSeek-V4-Flash",
)

SILICONFLOW_REVIEW_MODEL = os.environ.get(
    "SILICONFLOW_REVIEW_MODEL",
    "Qwen/Qwen3.6-27B",
)

SILICONFLOW_MERGE_MODEL = os.environ.get(
    "SILICONFLOW_MERGE_MODEL",
    "Qwen/Qwen3.6-27B",
)

SILICONFLOW_MAINTENANCE_MODEL = os.environ.get(
    "SILICONFLOW_MAINTENANCE_MODEL",
    SILICONFLOW_REVIEW_MODEL,
)

SILICONFLOW_MAINTENANCE_FAST_MODEL = os.environ.get(
    "SILICONFLOW_MAINTENANCE_FAST_MODEL",
    SILICONFLOW_FAST_MODEL,
)

# Section-level Wiki retrieval. The 0.6B embedding model is intentionally used
# for recall rather than generation: it is inexpensive, multilingual, and the
# main chat model still makes the final page selection.
WIKI_VECTOR_SEARCH_ENABLED = os.environ.get("WIKI_VECTOR_SEARCH_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on",
}
WIKI_EMBEDDING_MODEL = os.environ.get(
    "WIKI_EMBEDDING_MODEL",
    "Qwen/Qwen3-Embedding-0.6B",
)
WIKI_EMBEDDING_BATCH_SIZE = int(os.environ.get("WIKI_EMBEDDING_BATCH_SIZE", "16"))
WIKI_RRF_K = int(os.environ.get("WIKI_RRF_K", "60"))

# ========== Paper parsing ==========

# auto: arXiv HTML first, MinerU precision fallback, then a clearly marked
# PyMuPDF degradation. Explicit modes are useful for diagnosis and benchmarks.
PAPER_PARSER_MODE = os.environ.get("PAPER_PARSER_MODE", "auto").strip().lower()
if PAPER_PARSER_MODE not in {"auto", "arxiv_html", "mineru", "pymupdf"}:
    PAPER_PARSER_MODE = "auto"
ARXIV_HTML_BASE_URL = os.environ.get("ARXIV_HTML_BASE_URL", "https://arxiv.org/html")
ARXIV_HTML_TIMEOUT_SECONDS = max(10, int(os.environ.get("ARXIV_HTML_TIMEOUT_SECONDS", "60")))
MINERU_API_TOKEN = os.environ.get("MINERU_API_TOKEN", "").strip()
MINERU_API_BASE_URL = os.environ.get("MINERU_API_BASE_URL", "https://mineru.net/api/v4")
MINERU_MODEL_VERSION = os.environ.get("MINERU_MODEL_VERSION", "vlm")
MINERU_TIMEOUT_SECONDS = max(60, int(os.environ.get("MINERU_TIMEOUT_SECONDS", "600")))
MINERU_POLL_INTERVAL_SECONDS = max(1.0, float(os.environ.get("MINERU_POLL_INTERVAL_SECONDS", "4")))

# ========== Web Search Tool 配置 ==========

# off | duckduckgo
WEB_SEARCH_MODE = os.environ.get("WEB_SEARCH_MODE", "duckduckgo")
WEB_SEARCH_TIMEOUT_SECONDS = int(os.environ.get("WEB_SEARCH_TIMEOUT_SECONDS", "5"))
WEB_SEARCH_MAX_RESULTS = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "5"))

# ========== arXiv MCP / literature gateway ==========

ARXIV_API_URL = os.environ.get(
    "ARXIV_API_URL", "https://export.arxiv.org/api/query"
)
ARXIV_PDF_BASE_URL = os.environ.get(
    "ARXIV_PDF_BASE_URL", "https://arxiv.org/pdf"
)
ARXIV_USER_AGENT = os.environ.get(
    "ARXIV_USER_AGENT",
    "LLM-WIKI/1.0 (+https://github.com/ouchfys/LLM-WIKI)",
)
# arXiv asks API clients to leave three seconds between repeated requests.
ARXIV_REQUEST_INTERVAL_SECONDS = max(
    0.0, float(os.environ.get("ARXIV_REQUEST_INTERVAL_SECONDS", "3.1"))
)
ARXIV_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("ARXIV_TIMEOUT_SECONDS", "30"))
)
ARXIV_MAX_PDF_MB = max(1, int(os.environ.get("ARXIV_MAX_PDF_MB", "50")))
ARXIV_MCP_CACHE_DIR = os.environ.get("ARXIV_MCP_CACHE_DIR", "")
LLM_WIKI_API_URL = os.environ.get(
    "LLM_WIKI_API_URL", "http://127.0.0.1:8000"
)

# ========== Bounded background task execution ==========

# Long-running ingestion work stays in-process for the local deployment, but
# concurrency and pending work are bounded to prevent unbounded thread growth.
AGENT_TASK_MAX_WORKERS = max(1, int(os.environ.get("AGENT_TASK_MAX_WORKERS", "2")))
AGENT_TASK_QUEUE_CAPACITY = max(0, int(os.environ.get("AGENT_TASK_QUEUE_CAPACITY", "8")))

# ========== Object storage configuration ==========

# local | oss. Durable object keys are scoped as users/{tenant_id}/....
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
STORAGE_TENANT_ID = os.environ.get("STORAGE_TENANT_ID", "admin").strip() or "admin"
STORAGE_ROOT_PREFIX = (
    os.environ.get("STORAGE_ROOT_PREFIX", "").strip("/")
    or f"users/{STORAGE_TENANT_ID}"
)

OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "https://oss-cn-shenzhen.aliyuncs.com")
OSS_BUCKET = os.environ.get("OSS_BUCKET", "")

# Preferred names plus compatibility with the ACCESS_KEY_ID / ACCESS_KEY names
# that may already exist in local .env files.
OSS_ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID") or os.environ.get("ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET") or os.environ.get("ACCESS_KEY", "")


def get_model_runtime_summary():
    """返回当前运行时的模型配置摘要"""
    return {
        "chat_provider": "deepseek-official",
        "chat_model": DEEPSEEK_CHAT_MODEL,
        "fast_model": SILICONFLOW_FAST_MODEL,
        "summary_model": SILICONFLOW_SUMMARY_MODEL,
        "review_model": SILICONFLOW_REVIEW_MODEL,
        "merge_model": SILICONFLOW_MERGE_MODEL,
        "maintenance_model": SILICONFLOW_MAINTENANCE_MODEL,
        "maintenance_fast_model": SILICONFLOW_MAINTENANCE_FAST_MODEL,
        "embedding_model": WIKI_EMBEDDING_MODEL if WIKI_VECTOR_SEARCH_ENABLED else "disabled",
        "paper_parser_mode": PAPER_PARSER_MODE,
        "mineru_configured": bool(MINERU_API_TOKEN),
        "web_search_mode": WEB_SEARCH_MODE,
        "storage_backend": STORAGE_BACKEND,
        "storage_tenant_id": STORAGE_TENANT_ID,
        "oss_bucket": OSS_BUCKET if STORAGE_BACKEND == "oss" else "n/a",
    }


def print_config():
    """打印当前配置 (隐藏敏感信息)"""
    def mask(key: str) -> str:
        if not key:
            return "(未设置)"
        if len(key) <= 12:
            return key[:4] + "****"
        return key[:8] + "..." + key[-4:]

    print("\n" + "=" * 50)
    print("  当前配置")
    print("=" * 50)
    print(f"  SiliconFlow Key: {mask(SILICONFLOW_API_KEY)}")
    print(f"  DeepSeek Key:    {mask(DEEPSEEK_API_KEY)}")
    print(f"  Chat Base URL:   {DEEPSEEK_BASE_URL}")
    print(f"  Chat 模型:       {DEEPSEEK_CHAT_MODEL}")
    print(f"  Fast 模型:    {SILICONFLOW_FAST_MODEL}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    print_config()
