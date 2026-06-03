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
        print("[Config] 未找到 .env 文件，使用环境变量或默认值")
        return

    print(f"[Config] 加载配置文件: {env_path}")

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

# ========== 模型分級配置 ==========

# 主力模型: Wiki Chat / 面试评估 / 论文发现排序
SILICONFLOW_CHAT_MODEL = os.environ.get(
    "SILICONFLOW_CHAT_MODEL",
    "deepseek-ai/DeepSeek-V3",
)

# 轻量模型: 意图路由 / 偏好抽取 / 简单分类
SILICONFLOW_FAST_MODEL = os.environ.get(
    "SILICONFLOW_FAST_MODEL",
    "Qwen/Qwen2.5-7B-Instruct",
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

# ========== Docling 远程解析服务配置 ==========

DOCLING_MODE = os.environ.get("DOCLING_MODE", "off")  # remote | local | off
DOCLING_BASE_URL = os.environ.get(
    "DOCLING_BASE_URL", "http://127.0.0.1:5001"
)
DOCLING_TIMEOUT_SECONDS = int(os.environ.get("DOCLING_TIMEOUT_SECONDS", "120"))

# ========== Web Search Tool 配置 ==========

# off | duckduckgo
WEB_SEARCH_MODE = os.environ.get("WEB_SEARCH_MODE", "duckduckgo")
WEB_SEARCH_TIMEOUT_SECONDS = int(os.environ.get("WEB_SEARCH_TIMEOUT_SECONDS", "5"))
WEB_SEARCH_MAX_RESULTS = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "5"))

# ========== Object storage configuration ==========

# local | oss. OSS keys are scoped as users/{user_id}/..., currently users/admin.
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
STORAGE_ROOT_PREFIX = os.environ.get("STORAGE_ROOT_PREFIX", "users/admin").strip("/")

OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "https://oss-cn-shenzhen.aliyuncs.com")
OSS_BUCKET = os.environ.get("OSS_BUCKET", "")

# Preferred names plus compatibility with the ACCESS_KEY_ID / ACCESS_KEY names
# that may already exist in local .env files.
OSS_ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID") or os.environ.get("ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET") or os.environ.get("ACCESS_KEY", "")


def get_model_runtime_summary():
    """返回当前运行时的模型配置摘要"""
    return {
        "chat_model": SILICONFLOW_CHAT_MODEL,
        "fast_model": SILICONFLOW_FAST_MODEL,
        "summary_model": SILICONFLOW_SUMMARY_MODEL,
        "review_model": SILICONFLOW_REVIEW_MODEL,
        "merge_model": SILICONFLOW_MERGE_MODEL,
        "docling_mode": DOCLING_MODE,
        "docling_base_url": DOCLING_BASE_URL if DOCLING_MODE == "remote" else "n/a",
        "web_search_mode": WEB_SEARCH_MODE,
        "storage_backend": STORAGE_BACKEND,
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
    print(f"  API Key:      {mask(SILICONFLOW_API_KEY)}")
    print(f"  Base URL:     {SILICONFLOW_BASE_URL}")
    print(f"  Chat 模型:    {SILICONFLOW_CHAT_MODEL}")
    print(f"  Fast 模型:    {SILICONFLOW_FAST_MODEL}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    print_config()
