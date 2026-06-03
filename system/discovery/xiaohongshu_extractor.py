"""Public metadata extraction for Xiaohongshu share links.

This only reads publicly returned HTML metadata. It does not bypass login,
call private APIs, or scrape authenticated content.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Optional

import requests


XHS_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:xiaohongshu\.com|xhslink\.com)/[^\s<>()\[\]{}\"']+",
    re.I,
)


@dataclass
class ExtractedXhsNote:
    title: str
    content: str
    source_url: str
    final_url: str
    image_urls: list[str]


class XiaohongshuExtractor:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def extract_from_text_or_url(self, text_or_url: str) -> Optional[ExtractedXhsNote]:
        text_or_url = (text_or_url or "").strip()
        if not text_or_url:
            return None

        url = self.find_url(text_or_url)
        if not url:
            return None

        fetched = self.fetch_public_meta(url)
        if not fetched:
            return None

        share_text = self._strip_share_url(text_or_url, url).strip()
        unavailable = self._is_unavailable_page(fetched)
        share_title = self._title_from_share_text(share_text)
        # Share text is usually just a title, emoji token, and copy instruction.
        # Prefer the page metadata when it is publicly available so query strings
        # and lossy clipboard text never pollute the knowledge card body.
        content = share_text if unavailable else (str(fetched["description"] or "").strip() or share_text)

        return ExtractedXhsNote(
            title=share_title or ("" if unavailable else fetched["title"]) or self._fallback_title(content),
            content=content,
            source_url=url,
            final_url=url if unavailable else fetched["final_url"],
            image_urls=[] if unavailable else fetched["image_urls"],
        )

    def fetch_public_meta(self, url: str) -> Optional[Dict[str, str | list[str]]]:
        try:
            response = requests.get(
                url,
                headers=self.headers,
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
        except Exception as exc:
            print(f"[XiaohongshuExtractor] fetch failed: {exc}")
            return None

        response.encoding = self._detect_encoding(response)
        html_text = response.text or ""

        meta = self._parse_meta(html_text)
        title = meta.get("og:title") or meta.get("title") or self._parse_title(html_text)
        description = meta.get("description") or meta.get("og:description") or ""
        image_urls = self._parse_image_urls(html_text, meta)

        return {
            "title": self._clean_title(title),
            "description": self._clean_text(description),
            "final_url": response.url,
            "image_urls": image_urls,
        }

    @staticmethod
    def find_url(text: str) -> str:
        match = XHS_URL_RE.search(text or "")
        if not match:
            return ""
        url = match.group(0).strip()
        trailing = ".,;:!?)]}" + "\u3002\uff0c\uff1b\uff1a\uff01\uff1f\uff09\u3011"
        return url.rstrip(trailing)

    @staticmethod
    def _parse_meta(html_text: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for tag in re.findall(r"<meta\b[^>]*>", html_text or "", flags=re.I):
            attrs = {
                key.lower(): html.unescape(value)
                for key, value in re.findall(r'(\w+)=["\']([^"\']*)["\']', tag)
            }
            name = attrs.get("name") or attrs.get("property")
            content = attrs.get("content")
            if name and content:
                result[name.lower()] = content
        return result

    @staticmethod
    def _parse_image_urls(html_text: str, meta: Dict[str, str]) -> list[str]:
        candidates: list[str] = []
        for key, value in meta.items():
            if key in {"og:image", "twitter:image"} and value:
                candidates.append(value)
        candidates.extend(
            re.findall(r"https?://[^\"'\\\s<>)]+?xhscdn\.com/[^\"'\\\s<>)]+", html_text or "")
        )

        result: list[str] = []
        seen = set()
        for url in candidates:
            url = html.unescape(url)
            url = "/".join(url.split("\\u002F"))
            url = str(url).strip().rstrip("\\")
            url = url.split(");", 1)[0]
            url = url.split(")", 1)[0]
            url = re.sub(r"[,;]+$", "", url)
            if url and XiaohongshuExtractor._looks_like_image_url(url) and url not in seen:
                seen.add(url)
                result.append(url)
        return result[:12]

    @staticmethod
    def _looks_like_image_url(url: str) -> bool:
        lowered = (url or "").lower()
        if any(lowered.endswith(ext) for ext in [".js", ".css", ".json", ".html"]):
            return False
        if any(ext in lowered for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
            return True
        return any(domain in lowered for domain in ["sns-webpic", "sns-img", "sns-avatar"])

    @staticmethod
    def _parse_title(html_text: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html_text or "", flags=re.I | re.S)
        if not match:
            return ""
        return html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()

    @staticmethod
    def _clean_title(title: str) -> str:
        title = XiaohongshuExtractor._clean_text(title)
        title = re.sub(r"\s*-\s*\u5c0f\u7ea2\u4e66\s*$", "", title).strip()
        return title

    @staticmethod
    def _is_unavailable_page(fetched: Dict[str, str | list[str]]) -> bool:
        text = " ".join(str(fetched.get(key, "")) for key in ("title", "description", "final_url"))
        return any(
            marker in text
            for marker in (
                "404",
                "\u9875\u9762\u4e0d\u89c1\u4e86",
                "\u65e0\u6cd5\u6d4f\u89c8",
                "error_code=300031",
            )
        )

    @staticmethod
    def _title_from_share_text(text: str) -> str:
        text = XiaohongshuExtractor._clean_text(text)
        match = re.search(r"\u3010(.+?)\u3011", text)
        if match:
            title = match.group(1)
            title = title.split("|", 1)[0].strip()
            title = re.sub(r"\s*-\s*\u5c0f\u7ea2\u4e66\s*$", "", title).strip()
            return title[:80]
        return ""

    @staticmethod
    def _clean_text(text: str) -> str:
        text = html.unescape(text or "")
        text = unicodedata.normalize("NFKC", text)
        text = text.replace("\u200b", "").replace("\ufeff", "")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _strip_share_url(text: str, url: str) -> str:
        text = (text or "").replace(url, "")
        text = re.sub(
            r"\u5148\u590d\u5236\u8fd9\u6bb5.*?\u3010\u5c0f\u7ea2\u4e66\u3011.*$",
            "",
            text,
            flags=re.S,
        )
        text = re.sub(
            r"\u590d\u5236\u8fd9\u6bb5.*?\u6253\u5f00\u3010\u5c0f\u7ea2\u4e66\u3011.*$",
            "",
            text,
            flags=re.S,
        )
        return XiaohongshuExtractor._clean_text(text)

    @staticmethod
    def _detect_encoding(response) -> str:
        try:
            response.content.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            pass

        apparent = response.apparent_encoding
        if apparent:
            return apparent
        return response.encoding or "utf-8"

    @staticmethod
    def _fallback_title(content: str) -> str:
        content = XiaohongshuExtractor._clean_text(content)
        return content[:40] or "\u5c0f\u7ea2\u4e66\u7b14\u8bb0"
