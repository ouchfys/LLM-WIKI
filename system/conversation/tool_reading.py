"""Recoverable views of stored Markdown; offsets always refer to source characters."""
import hashlib
import json
import re


def sections(text):
    starts, cursor, fence = [], 0, None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
        elif fence is None:
            heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading:
                starts.append((cursor, heading.group(2), len(heading.group(1))))
        cursor += len(line)
    result = []
    for i, (start, title, level) in enumerate(starts):
        end = next((pos for pos, _, depth in starts[i + 1:] if depth <= level), len(text))
        result.append({"section": f"s{i + 1}", "title": title, "offset": start, "end_offset": end})
    return result


def page_metadata(item, result_id):
    body = str(item.get("content") or "")
    return {"result_id": result_id, "card_id": item.get("card_id") or item.get("id") or "",
            "title": item.get("title") or "", "total_chars": len(body),
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]}


def read_wiki_result(payload, result_id, tool, *, card_id="", section="", query="", offset=0, max_chars=4000):
    items = [item for item in payload.get("items", []) if isinstance(item, dict)]
    if card_id:
        items = [item for item in items if str(item.get("card_id") or item.get("id") or "") == card_id]
    elif query:
        # Prefer the actual card title over mentions in another paper or request metadata.
        title_matches = [item for item in items if query.lower() in str(item.get("title") or "").lower()]
        items = title_matches or [item for item in items if query.lower() in str(item.get("content") or "").lower()]
        if title_matches:
            query = ""
    if not items:
        return []
    if len(items) != 1:
        return [{**page_metadata(item, result_id), "tool": tool, "view": "directory",
                 "sections": sections(str(item.get("content") or "")),
                 "hint": "Select card_id; offsets are relative to that card's Markdown, not the JSON envelope."}
                for item in items]
    item = items[0]
    body = str(item.get("content") or "")
    index = sections(body)
    start, end = 0, len(body)
    if section:
        matches = [part for part in index if part["section"] == section or part["title"].casefold() == section.casefold()]
        if len(matches) != 1:
            return [{**page_metadata(item, result_id), "tool": tool, "view": "directory", "sections": index,
                     "error": "Section missing or ambiguous; select an exact section ID."}]
        start, end = matches[0]["offset"], matches[0]["end_offset"]
    offset = min(end, max(start, offset))
    match = body.lower().find(query.lower(), offset, end) if query else -1
    if query and match < 0:
        return [{**page_metadata(item, result_id), "tool": tool, "view": "directory", "sections": index,
                 "query": query, "match_found": False, "content": "", "next_offset": None}]
    if query:
        offset = max(offset, match - 500)
    stop = min(end, offset + max_chars)
    return [{**page_metadata(item, result_id), "tool": tool, "view": "page", "section": section,
             "content": body[offset:stop], "offset": offset, "end_offset": stop,
             "next_offset": stop if stop < end else None, "section_end": end,
             "query": query, "match_found": match >= 0 if query else None,
             "sections": index if offset == start else [],
             "hint": "Continue with the same result_id/card_id/section and next_offset; omit query when paging. This is a stored snapshot."}]


def render_page(body, metadata, counter, budget):
    """Full page if it fits; otherwise an exact prefix with an actionable cursor."""
    header = json.dumps(metadata, ensure_ascii=False) + "\n"
    if counter.count(header + body) <= budget:
        return header + body
    # A short section map is navigation, not a claim that omitted sections were read.
    directory = json.dumps(sections(body)[:24], ensure_ascii=False)
    if counter.count(directory) > budget // 3:
        directory = "[]"  # read_tool_result can return the complete directory.
    def candidate(stop):
        base = int(metadata.get("offset") or 0)
        notice = {"offset": base, "end_offset": base + stop, "next_offset": base + stop,
                  "total_chars": metadata.get("total_chars", len(body)), "sections": json.loads(directory) if not base else []}
        return (header + "[内容因上下文预算省略；read_tool_result 按下方 next_offset 续读]\n"
                + json.dumps(notice, ensure_ascii=False) + "\n" + body[:stop])
    low, high, best = 0, len(body), ""
    while low <= high:
        mid = (low + high) // 2
        value = candidate(mid)
        if counter.count(value) <= budget:
            best, low = value, mid + 1
        else:
            high = mid - 1
    if best:
        return best
    minimal = json.dumps({**metadata, "next_offset": metadata.get("offset", 0), "view": "omitted"}, ensure_ascii=False)
    return minimal if counter.count(minimal) <= budget else ""


def render_parts(parts, counter, budget):
    """Water-fill shares so a large first result cannot hide all later sources."""
    remaining = max(0, budget - 2 * len(parts))
    costs = [cost for cost, _ in parts]
    allocations, pending = [0] * len(parts), set(range(len(parts)))
    while pending and remaining:
        share = remaining // len(pending)
        small = {i for i in pending if costs[i] <= share}
        if not small:
            for i in pending:
                allocations[i] = share
            break
        for i in small:
            allocations[i] = costs[i]
            remaining -= costs[i]
        pending -= small
    return "\n".join(render(cap) for (_, render), cap in zip(parts, allocations) if cap)
