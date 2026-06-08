from __future__ import annotations

import argparse
import json

from system.core.config import (
    SILICONFLOW_MAINTENANCE_MODEL,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_MODE,
    WEB_SEARCH_TIMEOUT_SECONDS,
)
from system.core.siliconflow_client import SiliconFlowChat
from system.search.web_fetch import WebFetchTool
from system.search.web_search import WebSearchTool
from system.wiki.maintenance.web_update_agent import WebUpdateAgent


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover web source candidates for wiki maintenance.")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--fetch-top", type=int, default=2)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()

    llm = None if args.no_llm else SiliconFlowChat(
        model=SILICONFLOW_MAINTENANCE_MODEL,
        temperature=0.0,
        max_tokens=3200,
    )
    result = WebUpdateAgent(
        llm=llm,
        web_search=WebSearchTool(
            mode=WEB_SEARCH_MODE,
            timeout_seconds=WEB_SEARCH_TIMEOUT_SECONDS,
            max_results=WEB_SEARCH_MAX_RESULTS,
        ),
        web_fetch=WebFetchTool(timeout_seconds=max(WEB_SEARCH_TIMEOUT_SECONDS, 8)),
    ).discover(
        topic=args.topic,
        limit=max(1, args.limit),
        fetch_top=max(0, args.fetch_top),
        upload=not args.no_upload,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
