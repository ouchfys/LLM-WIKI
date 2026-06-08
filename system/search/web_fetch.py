"""Fetch and extract readable evidence from public web pages.

`web_search` is for source discovery. `web_fetch` is for evidence: it opens a
specific URL, extracts readable text, and returns compact passages for the Wiki
Agent to cite as temporary external context.
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse

import requests


@dataclass
class WebFetchResult:
    title: str
    url: str
    site: str = ""
    snippet: str = ""
    text_excerpt: str = ""
    passages: List[Dict[str, Any]] = field(default_factory=list)
    published_at: str = ""
    author: str = ""
    fetched_at: str = ""
    status: str = "done"
    error: str = ""


class WebFetchTool:
    def __init__(
        self,
        timeout_seconds: int = 12,
        max_bytes: int = 2_000_000,
        max_passages: int = 4,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_passages = max(1, max_passages)

    @property
    def available(self) -> bool:
        return True

    def fetch(self, url: str, query: str = "", max_passages: int | None = None) -> WebFetchResult:
        url = self._normalize_url(url)
        parsed = urlparse(url)
        site = parsed.netloc.lower()
        if not self._allowed_url(parsed):
            return WebFetchResult(
                title="",
                url=url,
                site=site,
                status="error",
                error="URL is not allowed for web_fetch.",
                fetched_at=self._now(),
            )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,text/plain,application/xhtml+xml;q=0.9,*/*;q=0.5",
        }
        try:
            response = requests.get(
                url,
                timeout=self.timeout_seconds,
                headers=headers,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not any(kind in content_type for kind in ("text/html", "text/plain", "xhtml")):
                return WebFetchResult(
                    title="",
                    url=response.url or url,
                    site=urlparse(response.url or url).netloc.lower(),
                    status="error",
                    error=f"Unsupported content type: {content_type}",
                    fetched_at=self._now(),
                )
            raw = self._read_limited(response)
        except requests.exceptions.SSLError as exc:
            try:
                response = requests.get(
                    url,
                    timeout=self.timeout_seconds,
                    headers=headers,
                    stream=True,
                    allow_redirects=True,
                    verify=False,
                )
                response.raise_for_status()
                raw = self._read_limited(response)
            except Exception as retry_exc:
                return self._error(url, site, f"SSL retry failed: {retry_exc}")
        except Exception as exc:
            return self._error(url, site, str(exc))

        encoding = response.encoding or response.apparent_encoding or "utf-8"
        text = raw.decode(encoding, errors="replace")
        final_url = response.url or url
        final_site = urlparse(final_url).netloc.lower()
        title = self._extract_title(text) or final_site
        author = self._extract_meta(text, ["author", "article:author", "byl"])
        published_at = self._extract_meta(
            text,
            ["article:published_time", "datePublished", "date", "pubdate", "publishdate"],
        )
        readable = self._extract_readable_text(text)
        excerpt = readable[:1200]
        passages = self._rank_passages(readable, query, max_passages or self.max_passages)
        return WebFetchResult(
            title=title,
            url=final_url,
            site=final_site,
            snippet=passages[0]["text"][:320] if passages else excerpt[:320],
            text_excerpt=excerpt,
            passages=passages,
            published_at=published_at,
            author=author,
            fetched_at=self._now(),
        )

    def _read_limited(self, response: requests.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            total += len(chunk)
            if total > self.max_bytes:
                chunks.append(chunk[: max(0, self.max_bytes - (total - len(chunk)))])
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def _allowed_url(self, parsed) -> bool:
        if parsed.scheme not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").strip().lower()
        if not host or host in {"localhost"} or host.endswith(".local"):
            return False
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror:
            return False
        for info in infos:
            ip_text = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_text)
            except ValueError:
                return False
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return False
        return True

    @staticmethod
    def _extract_title(text: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", text or "", flags=re.IGNORECASE | re.DOTALL)
        if match:
            return WebFetchTool._clean(match.group(1))[:220]
        meta_title = WebFetchTool._extract_meta(text, ["og:title", "twitter:title"])
        return meta_title[:220]

    @staticmethod
    def _extract_meta(text: str, names: list[str]) -> str:
        for name in names:
            patterns = [
                rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\'](.*?)["\']',
                rf'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
            ]
            for pattern in patterns:
                match = re.search(pattern, text or "", flags=re.IGNORECASE | re.DOTALL)
                if match:
                    return WebFetchTool._clean(match.group(1))[:220]
        return ""

    @staticmethod
    def _extract_readable_text(text: str) -> str:
        text = re.sub(r"<!--.*?-->", " ", text or "", flags=re.DOTALL)
        text = re.sub(r"<(script|style|noscript|svg|canvas|iframe)\b.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<(nav|header|footer|aside|form)\b.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"</(p|div|section|article|h[1-6]|li|tr)>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        lines = []
        seen = set()
        for raw_line in text.splitlines():
            line = WebFetchTool._clean(raw_line)
            if len(line) < 30:
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            lines.append(line)
        return "\n".join(lines)[:12000]

    @staticmethod
    def _rank_passages(text: str, query: str, limit: int) -> List[Dict[str, Any]]:
        paragraphs = [part.strip() for part in re.split(r"\n+", text or "") if len(part.strip()) >= 40]
        terms = WebFetchTool._terms(query)
        scored: list[tuple[float, int, str]] = []
        for index, paragraph in enumerate(paragraphs[:80]):
            lower = paragraph.lower()
            score = sum(1.0 for term in terms if term.lower() in lower)
            score += min(len(paragraph), 600) / 2000.0
            scored.append((score, index, paragraph[:900]))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {"text": passage, "score": round(score, 4), "rank": rank}
            for rank, (score, _, passage) in enumerate(scored[:limit], start=1)
        ]

    @staticmethod
    def _terms(text: str) -> List[str]:
        terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", text or "")
        return list(dict.fromkeys(terms))[:16]

    @staticmethod
    def _clean(value: str) -> str:
        value = re.sub(r"<[^>]+>", " ", value or "")
        value = html.unescape(value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _error(url: str, site: str, message: str) -> WebFetchResult:
        return WebFetchResult(
            title="",
            url=url,
            site=site,
            status="error",
            error=message[:500],
            fetched_at=WebFetchTool._now(),
        )

    @staticmethod
    def _normalize_url(url: str) -> str:
        value = (url or "").strip()
        if not value:
            return ""
        if re.match(r"^https?://", value, flags=re.IGNORECASE):
            return value
        if re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/.*)?$", value):
            return "https://" + value
        return value
