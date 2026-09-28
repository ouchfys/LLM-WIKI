"""One citation namespace and one budgeted body per opened Wiki page."""
from __future__ import annotations

import json
import re
from typing import Callable
from system.conversation.tool_reading import render_page


class WikiCitationContext:
    """Keep the UI's card order; budget bodies independently of source identity.

    A source index is navigation metadata, never evidence by itself. Keeping it
    outside optional body sections prevents a long first paper from removing the
    citation identities of papers later in the same answer.
    """

    def __init__(self, cards, counter, compact_content: Callable, result_ids=None):
        self.counter = counter
        self.sources = []
        self.numbers = {}
        for number, card in enumerate(cards, start=1):
            card_id = str(card.get("id") or card.get("card_id") or "")
            if card_id and card_id in self.numbers:
                continue
            if card_id:
                self.numbers[card_id] = number
            full_text = card.get("_full_text")
            matched = card.get("_matched_chunks")
            if full_text:
                body = str(full_text)
            elif matched:
                body = "\n\n".join(str(part) for part in matched)
            else:
                body = compact_content(card.get("content_json") or {})
            content = card.get("content_json") if isinstance(card.get("content_json"), dict) else {}
            if content.get("repository_research"):
                body = re.sub(r"<!--\s*wiki-system\s+\{.*?\}\s*-->", "", body, flags=re.DOTALL).strip()
            source_packets = content.get("source_packet_ids") or []
            if not isinstance(source_packets, list):
                source_packets = []
            source_packets = list(dict.fromkeys(str(value) for value in [
                content.get("source_packet_id"), *source_packets,
            ] if value))
            self.sources.append({
                "number": number,
                "card_id": card_id,
                "title": str(card.get("title") or ""),
                "path": str(card.get("markdown_path") or ""),
                "source_packet_ids": source_packets,
                "body": body,
                "result_id": (result_ids or {}).get(card_id),
            })

    def index(self):
        return "\n".join(
            f"[{source['number']}] " + json.dumps({
                "card_id": source["card_id"],
                "title": source["title"],
                "path": source["path"],
                "source_packet_ids": source["source_packet_ids"],
                "result_id": source["result_id"],
            }, ensure_ascii=False)
            for source in self.sources
        )

    def token_cost(self):
        return sum(
            self.counter.count(f"[{source['number']}] card_id={source['card_id']}\n" + source["body"]
                               + json.dumps({"card_id": source["card_id"], "result_id": source["result_id"]})) + 16
            for source in self.sources
        )

    def render(self, budget):
        """Include full bodies when they fit, otherwise share the actual budget.

        No fixed per-paper or total token window is imposed here. Small pages
        keep their full body; unused shares flow to longer pages. Truncation is
        explicit and never changes the citation map.
        """
        if not self.sources:
            return ""
        headers = [
            f"[{source['number']}] card_id={source['card_id']}\n"
            for source in self.sources
        ]
        overhead = self.counter.count("\n\n".join(headers)) + 8 * len(headers)
        remaining = max(0, budget - overhead)
        costs = [self.counter.count(source["body"] + "\n" + json.dumps(
            {"card_id": source["card_id"], "result_id": source["result_id"]}, ensure_ascii=False)) for source in self.sources]
        allocations = [0] * len(costs)
        pending = set(range(len(costs)))
        while pending and remaining:
            share = remaining // len(pending)
            if not share:
                break
            finished = {i for i in pending if costs[i] <= share}
            if not finished:
                for i in pending:
                    allocations[i] = share
                break
            for i in finished:
                allocations[i] = costs[i]
                remaining -= costs[i]
            pending -= finished
        parts = []
        for header, source, allowance in zip(headers, self.sources, allocations):
            body = render_page(source["body"], {"card_id": source["card_id"], "result_id": source["result_id"]},
                               self.counter, allowance)
            if body:
                parts.append(header + body)
        return "\n\n".join(parts)
