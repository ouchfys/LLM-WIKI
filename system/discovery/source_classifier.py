"""
Source Classifier — classify sources as primary/secondary/tertiary.

Uses domain-based heuristics. No LLM required for most classifications.
"""

from typing import List

from system.discovery.source_adapters import SourceItem

# Domain-based classification rules
PRIMARY_DOMAINS = {
    "arxiv.org",
    "aclanthology.org",
    "openreview.net",
    "proceedings.mlr.press",
    "papers.nips.cc",
    "jmlr.org",
    "dl.acm.org",
    "ieeexplore.ieee.org",
    "scholar.google.com",      # links to primary but itself is an index
    "semanticscholar.org",     # same
    "github.com",
    "gitlab.com",
    "bitbucket.org",
}

SECONDARY_DOMAINS = {
    "zhuanlan.zhihu.com",
    "jiqizhixin.com",
    "mp.weixin.qq.com",
    "medium.com",
    "towardsdatascience.com",
    "blog.csdn.net",
    "juejin.cn",
    "sspai.com",
    "bilibili.com",
    "youtube.com",
    "reddit.com",
    "hackernews.com",
    "news.ycombinator.com",
}

PRIMARY_VENUES = {
    "ACL", "EMNLP", "NAACL", "COLING", "ICLR", "ICML", "NeurIPS",
    "CVPR", "ICCV", "ECCV", "AAAI", "IJCAI", "SIGIR", "WWW",
    "KDD", "SIGMOD", "VLDB", "OSDI", "SOSP", "PLDI", "POPL",
    "arXiv", "Nature", "Science", "Cell", "PNAS",
}

SECONDARY_VENUE_PATTERNS = {
    "workshop", "symposium", "tutorial", "survey", "review",
}


class SourceClassifier:
    """Classify sources as primary / secondary / tertiary."""

    def classify(self, item: SourceItem) -> str:
        """Classify a single item. Returns 'primary', 'secondary', or 'tertiary'."""
        # Rule 1: explicit source_level already set
        if item.source_level in ("primary", "secondary", "tertiary"):
            return item.source_level

        # Rule 2: arXiv preprints are primary
        if item.id.startswith("arxiv:"):
            return "primary"

        # Rule 3: venue-based classification
        if item.venue:
            venue_upper = item.venue.upper()
            if any(v.upper() in venue_upper for v in PRIMARY_VENUES):
                return "primary"
            if any(p in venue_upper.lower() for p in SECONDARY_VENUE_PATTERNS):
                return "secondary"

        # Rule 4: URL domain-based classification
        if item.url:
            from urllib.parse import urlparse
            try:
                domain = urlparse(item.url).netloc.lower()
                domain = domain.replace("www.", "")

                if any(d in domain for d in PRIMARY_DOMAINS):
                    return "primary"
                if any(d in domain for d in SECONDARY_DOMAINS):
                    return "secondary"
            except Exception:
                pass

        # Rule 5: source_type heuristic
        if item.source_type == "paper":
            return "primary"
        if item.source_type == "manual":
            return "primary"

        return "secondary"

    def classify_batch(self, items: List[SourceItem]) -> List[SourceItem]:
        """Classify all items in-place and return them."""
        for item in items:
            item.source_level = self.classify(item)
        return items
