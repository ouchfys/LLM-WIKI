"""Lightweight web search adapter for Wiki Chat fallback.

The tool intentionally returns search-result snippets only. It does not ingest
web pages into the private Wiki and does not persist external content.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import List
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""


class WebSearchTool:
    def __init__(
        self,
        mode: str = "duckduckgo",
        timeout_seconds: int = 8,
        max_results: int = 5,
    ):
        self.mode = (mode or "off").strip().lower()
        self.timeout_seconds = timeout_seconds
        self.max_results = max(1, max_results)

    @property
    def available(self) -> bool:
        return self.mode == "duckduckgo"

    def search(self, query: str, limit: int | None = None) -> List[WebSearchResult]:
        if not self.available or not (query or "").strip():
            return []
        if self.mode == "duckduckgo":
            return self._search_duckduckgo(query, limit or self.max_results)
        return []

    def _search_duckduckgo(self, query: str, limit: int) -> List[WebSearchResult]:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        }
        try:
            response = requests.get(url, timeout=self.timeout_seconds, headers=headers)
            response.raise_for_status()
        except requests.exceptions.SSLError as exc:
            print(f"[WebSearchTool] DuckDuckGo SSL verification failed, retrying without verification: {exc}")
            try:
                response = requests.get(url, timeout=self.timeout_seconds, headers=headers, verify=False)
                response.raise_for_status()
            except Exception as retry_exc:
                print(f"[WebSearchTool] DuckDuckGo search failed after SSL fallback: {retry_exc}")
                return []
        except Exception as exc:
            print(f"[WebSearchTool] DuckDuckGo search failed: {exc}")
            return self._search_bing(query, limit, headers)

        return self._parse_duckduckgo_html(response.text, limit)

    def _search_bing(self, query: str, limit: int, headers: dict) -> List[WebSearchResult]:
        url = f"https://www.bing.com/search?q={quote_plus(query)}"
        try:
            response = requests.get(url, timeout=self.timeout_seconds, headers=headers)
            response.raise_for_status()
        except requests.exceptions.SSLError as exc:
            print(f"[WebSearchTool] Bing SSL verification failed, retrying without verification: {exc}")
            try:
                response = requests.get(url, timeout=self.timeout_seconds, headers=headers, verify=False)
                response.raise_for_status()
            except Exception as retry_exc:
                print(f"[WebSearchTool] Bing search failed after SSL fallback: {retry_exc}")
                return []
        except Exception as exc:
            print(f"[WebSearchTool] Bing search failed: {exc}")
            return []
        return self._parse_bing_html(response.text, limit)

    def _parse_duckduckgo_html(self, text: str, limit: int) -> List[WebSearchResult]:
        results: List[WebSearchResult] = []
        blocks = re.findall(
            r'<div class="result results_links.*?</div>\s*</div>',
            text or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not blocks:
            blocks = re.findall(
                r'<a[^>]+class="result__a"[^>]+href="[^"]+"[^>]*>.*?</a>.*?(?:result__snippet.*?</a>|</div>)',
                text or "",
                flags=re.IGNORECASE | re.DOTALL,
            )

        for block in blocks:
            link = re.search(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not link:
                continue

            raw_url = html.unescape(link.group(1))
            title = self._clean_html(link.group(2))
            snippet_match = re.search(
                r'class="result__snippet"[^>]*>(.*?)</a>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            snippet = self._clean_html(snippet_match.group(1)) if snippet_match else ""
            resolved = self._resolve_duckduckgo_url(raw_url)

            if not title or not resolved:
                continue
            if any(item.url == resolved for item in results):
                continue

            results.append(WebSearchResult(title=title, url=resolved, snippet=snippet))
            if len(results) >= limit:
                break

        return results

    def _parse_bing_html(self, text: str, limit: int) -> List[WebSearchResult]:
        results: List[WebSearchResult] = []
        blocks = re.findall(
            r'<li class="b_algo".*?</li>',
            text or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        for block in blocks:
            link = re.search(
                r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not link:
                continue
            url = html.unescape(link.group(1)).strip()
            title = self._clean_html(link.group(2))
            snippet_match = re.search(
                r'<p[^>]*>(.*?)</p>',
                block,
                flags=re.IGNORECASE | re.DOTALL,
            )
            snippet = self._clean_html(snippet_match.group(1)) if snippet_match else ""
            if not title or not url:
                continue
            if any(item.url == url for item in results):
                continue
            results.append(WebSearchResult(title=title, url=url, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _resolve_duckduckgo_url(raw_url: str) -> str:
        raw_url = html.unescape(raw_url or "").strip()
        if not raw_url:
            return ""
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        parsed = urlparse(raw_url)
        if parsed.path.startswith("/l/"):
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            return unquote(target)
        return raw_url

    @staticmethod
    def _clean_html(value: str) -> str:
        value = re.sub(r"<[^>]+>", " ", value or "")
        value = html.unescape(value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()
