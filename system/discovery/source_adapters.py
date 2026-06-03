"""
Source Adapters — fetch and normalize paper/article metadata from various sources.

Defines the shared SourceItem structure and adapter interface.
Adapters fetch metadata only (not full text) for lightweight discovery.
"""

import hashlib
import html
import json
import re
import urllib.request
import urllib.error
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET


@dataclass
class SourceItem:
    """Normalized representation of a discovered source."""
    id: str
    title: str
    source_type: str = "article"       # "paper" | "article" | "manual" | "repo"
    summary: str = ""
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: Optional[str] = None
    url: Optional[str] = None
    citation_count: Optional[int] = None
    source_level: str = ""             # set by SourceClassifier
    relevance_score: float = 0.0       # set by PaperDiscovery ranking
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


class SourceAdapter(ABC):
    """Base class for source adapters."""

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> List[SourceItem]:
        """Search for sources matching the query."""

    @abstractmethod
    def fetch_detail(self, source_id: str) -> Optional[SourceItem]:
        """Fetch detailed metadata for a specific source."""


class ManualSourceAdapter(SourceAdapter):
    """Adapter for user-provided URLs or text. Parses basic metadata."""

    def search(self, query: str, limit: int = 10) -> List[SourceItem]:
        return []

    def fetch_detail(self, source_id: str) -> Optional[SourceItem]:
        return None

    @staticmethod
    def from_url(url: str) -> Optional[SourceItem]:
        """Create a SourceItem from a user-provided URL by fetching metadata."""
        url = (url or "").strip()
        if not url:
            return None

        item_id = hashlib.md5(url.encode()).hexdigest()[:12]
        item = SourceItem(
            id=item_id,
            title=url.split("/")[-1] or url,
            source_type="article",
            url=url,
        )

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PersonalResearchAgent/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")[:50000]
                item.raw_metadata["html"] = content[:5000]
                title_match = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE)
                if title_match:
                    item.title = title_match.group(1).strip()
                desc_match = re.search(
                    r'<meta\s+name="description"\s+content="(.*?)"',
                    content,
                    re.IGNORECASE,
                )
                if desc_match:
                    item.summary = desc_match.group(1).strip()[:500]
                elif not item.summary:
                    item.summary = item.title
        except Exception as e:
            print(f"[ManualSourceAdapter] Failed to fetch {url}: {e}")
            item.summary = item.title

        return item

    @staticmethod
    def from_text(text: str, title: str = "") -> SourceItem:
        """Create a SourceItem from manually pasted text."""
        text = (text or "").strip()
        item_id = hashlib.md5(text.encode()).hexdigest()[:12]
        return SourceItem(
            id=item_id,
            title=title or "Manual Entry",
            source_type="manual",
            summary=text[:500],
            url=None,
        )


class ArxivAdapter(SourceAdapter):
    """Search arXiv API for paper metadata. No full-text download."""

    BASE_URL = "http://export.arxiv.org/api/query"

    def __init__(self, sort_by: str = "relevance"):
        self.sort_by = sort_by

    def search(self, query: str, limit: int = 10) -> List[SourceItem]:
        query = (query or "").strip()
        if not query:
            return []

        params = urllib.parse.urlencode({
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": limit,
            "sortBy": self.sort_by,
            "sortOrder": "descending",
        })
        url = f"{self.BASE_URL}?{params}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PersonalResearchAgent/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"[ArxivAdapter] Search failed: {e}")
            return []

        return self._parse_response(content)

    def fetch_detail(self, source_id: str) -> Optional[SourceItem]:
        arxiv_id = source_id
        if arxiv_id.startswith("arxiv:"):
            arxiv_id = arxiv_id[6:]
        params = urllib.parse.urlencode({"id_list": arxiv_id, "max_results": 1})
        url = f"{self.BASE_URL}?{params}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PersonalResearchAgent/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"[ArxivAdapter] Fetch detail failed: {e}")
            return None

        items = self._parse_response(content)
        return items[0] if items else None

    def _parse_response(self, xml_content: str) -> List[SourceItem]:
        items = []
        try:
            root = ET.fromstring(xml_content)
            ns = {"atom": "http://www.w3.org/2005/Atom",
                   "arxiv": "http://arxiv.org/schemas/atom"}
            for entry in root.findall("atom:entry", ns):
                title_el = entry.find("atom:title", ns)
                summary_el = entry.find("atom:summary", ns)
                id_el = entry.find("atom:id", ns)

                title = self._clean_text(title_el.text) if title_el is not None else "Untitled"
                summary = self._clean_text(summary_el.text) if summary_el is not None else ""
                arxiv_url = id_el.text.strip() if id_el is not None else ""
                arxiv_id = arxiv_url.split("/abs/")[-1] if "/abs/" in arxiv_url else arxiv_url

                authors = []
                for author_el in entry.findall("atom:author", ns):
                    name_el = author_el.find("atom:name", ns)
                    if name_el is not None and name_el.text:
                        authors.append(name_el.text.strip())

                published_el = entry.find("atom:published", ns)
                year = None
                if published_el is not None and published_el.text:
                    try:
                        year = int(published_el.text[:4])
                    except ValueError:
                        pass

                items.append(SourceItem(
                    id=f"arxiv:{arxiv_id}",
                    title=title,
                    source_type="paper",
                    summary=summary[:800],
                    authors=authors,
                    year=year,
                    venue="arXiv",
                    url=arxiv_url,
                    citation_count=None,
                    source_level="primary",
                    raw_metadata={"arxiv_id": arxiv_id},
                ))
        except ET.ParseError as e:
            print(f"[ArxivAdapter] XML parse error: {e}")

        return items

    @staticmethod
    def _clean_text(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"\s+", " ", text)
        return text


class RssFeedAdapter(SourceAdapter):
    """Fetch public RSS/Atom feeds and normalize entries as article sources."""

    def __init__(self, feeds: List[Dict[str, str]] = None):
        self.feeds = feeds or []

    def search(self, query: str, limit: int = 10) -> List[SourceItem]:
        query = (query or "").strip()
        tokens = self._query_tokens(query)
        items: List[SourceItem] = []
        seen = set()

        for feed in self.feeds:
            for item in self.fetch_feed(feed):
                if item.id in seen:
                    continue
                if tokens and not self._matches(item, tokens):
                    continue
                seen.add(item.id)
                items.append(item)
                if len(items) >= limit:
                    return items
        return items[:limit]

    def fetch_detail(self, source_id: str) -> Optional[SourceItem]:
        return None

    def fetch_feed(self, feed: Dict[str, str]) -> List[SourceItem]:
        url = (feed.get("url") or "").strip()
        if not url:
            return []
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PrivateJarvis/0.1"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            print(f"[RssFeedAdapter] Failed to fetch {url}: {exc}")
            return []
        return self._parse_feed(content, feed)

    def _parse_feed(self, content: str, feed: Dict[str, str]) -> List[SourceItem]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            print(f"[RssFeedAdapter] XML parse error for {feed.get('url')}: {exc}")
            return []

        tag = self._local_name(root.tag)
        if tag == "rss":
            return self._parse_rss(root, feed)
        if tag == "feed":
            return self._parse_atom(root, feed)
        return []

    def _parse_rss(self, root: ET.Element, feed: Dict[str, str]) -> List[SourceItem]:
        channel = root.find("channel")
        feed_title = self._text(channel.find("title")) if channel is not None else feed.get("name", "")
        items = []
        for entry in root.findall(".//item"):
            title = self._clean_text(self._text(entry.find("title"))) or "Untitled"
            link = self._text(entry.find("link"))
            summary = self._clean_html(self._text(entry.find("description")))
            guid = self._text(entry.find("guid")) or link or title
            categories = [self._clean_text(self._text(cat)) for cat in entry.findall("category")]
            pub_date = self._text(entry.find("pubDate"))
            items.append(self._make_item(
                identity=guid,
                title=title,
                url=link,
                summary=summary,
                feed=feed,
                feed_title=feed_title,
                raw_metadata={"published": pub_date, "categories": categories},
            ))
        return items

    def _parse_atom(self, root: ET.Element, feed: Dict[str, str]) -> List[SourceItem]:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        feed_title = self._text(root.find("atom:title", ns)) or feed.get("name", "")
        items = []
        for entry in root.findall("atom:entry", ns):
            title = self._clean_text(self._text(entry.find("atom:title", ns))) or "Untitled"
            link = self._atom_link(entry, ns)
            summary = self._clean_html(
                self._text(entry.find("atom:summary", ns))
                or self._text(entry.find("atom:content", ns))
            )
            identity = self._text(entry.find("atom:id", ns)) or link or title
            updated = self._text(entry.find("atom:updated", ns)) or self._text(entry.find("atom:published", ns))
            categories = [
                cat.attrib.get("term", "")
                for cat in entry.findall("atom:category", ns)
                if cat.attrib.get("term")
            ]
            items.append(self._make_item(
                identity=identity,
                title=title,
                url=link,
                summary=summary,
                feed=feed,
                feed_title=feed_title,
                raw_metadata={"published": updated, "categories": categories},
            ))
        return items

    def _make_item(
        self,
        identity: str,
        title: str,
        url: str,
        summary: str,
        feed: Dict[str, str],
        feed_title: str,
        raw_metadata: Dict[str, Any],
    ) -> SourceItem:
        item_id = hashlib.md5((identity or title).encode("utf-8")).hexdigest()[:16]
        return SourceItem(
            id=f"feed:{item_id}",
            title=title,
            source_type="article",
            summary=summary[:800],
            authors=[],
            year=None,
            venue=feed.get("name") or feed_title,
            url=url,
            source_level=feed.get("source_level") or "secondary",
            raw_metadata={
                "feed_url": feed.get("url", ""),
                "feed_name": feed.get("name") or feed_title,
                **raw_metadata,
            },
        )

    @staticmethod
    def _atom_link(entry: ET.Element, ns: Dict[str, str]) -> str:
        for link in entry.findall("atom:link", ns):
            rel = link.attrib.get("rel", "alternate")
            href = link.attrib.get("href", "")
            if href and rel == "alternate":
                return href
        first = entry.find("atom:link", ns)
        return first.attrib.get("href", "") if first is not None else ""

    @staticmethod
    def _text(element: Optional[ET.Element]) -> str:
        if element is None or element.text is None:
            return ""
        return element.text.strip()

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    @staticmethod
    def _clean_text(text: str) -> str:
        text = html.unescape(text or "")
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _clean_html(text: str) -> str:
        text = html.unescape(text or "")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _query_tokens(query: str) -> List[str]:
        return [
            token.lower()
            for token in re.findall(r"[a-z0-9][a-z0-9_\-]{1,}|[\u4e00-\u9fff]{2,}", query or "")
        ]

    @staticmethod
    def _matches(item: SourceItem, tokens: List[str]) -> bool:
        text = f"{item.title} {item.summary} {item.venue}".lower()
        return any(token in text for token in tokens)
