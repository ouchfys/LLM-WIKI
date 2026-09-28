"""One source for native and JSON-fallback agent tool contracts.

Only active model tools are registered here. Defaults and accepted tool names
are derived from these contracts; retired services are not model tools.
"""

from typing import Any, Dict, Iterable, List
from pathlib import Path


REPOSITORY_CARD_WRITING_RULES = (Path(__file__).with_name("prompts") / "repository_card.md").read_text(encoding="utf-8")

def native_tool_specs() -> List[Dict[str, Any]]:
    def schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
        properties = {**properties,
            "gap_id": {"type": "string", "description": "Optional legacy question reference; not required for research."},
            "query_purpose": {"type": "string", "description": "Optional purpose of this lookup. Reuse already observed answers."},
        }
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    return [
        {"type": "function", "function": {
            "name": "local_shell",
            "description": "Run commands for files, public APIs, processing or verification using server permissions. Defaults to this conversation's temporary directory, created only when a command runs. Keep temporary scripts, drafts and downloads there; use projects only for user-requested deliverables. Returns stdout/stderr, exit_code and timed_out. Missing shell, bad cwd, nonzero exit or timeout require inspecting output before retrying; partial effects may remain. Verify the requested outcome separately.",
            "parameters": schema({
                "command": {"type": "string", "description": "Command text to execute."},
                "cwd": {"type": "string", "description": "Optional existing absolute path or path relative to the default shell directory shown in context."},
                "shell": {"type": "string", "enum": ["powershell", "bash"], "description": "Command language; defaults to powershell."},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "description": "Wait limit in seconds; defaults to 120."},
            }, ["command"]) }},
        {"type": "function", "function": {
            "name": "repository",
            "description": "Research public GitHub source at a pinned commit. discover finds candidate repositories (verify identity); open accepts owner/repo or its root URL and returns snapshot_id; list/search locate paths; search with paths performs literal content search in up to eight files; read returns source text and line range with legacy span metadata. Use next_offset/next_line for paging. Let the user's question and relevant conversation set the reading scope and depth; follow implementation/callers/tests to resolve relevant gaps and stop when the requested coverage is supported. Navigation and no matches are not proof of implementation or absence. You may also research with local_shell and write the final card directly; repository reads and span IDs are not prerequisites for wiki_write. checkout creates an optional size-limited shallow Git snapshot when cross-file reading or API limits justify it. Cache is bounded and expires. cache_status reports disk usage. Errors are operational, preserve successful work on other repositories.",
            "parameters": schema({
                "operation": {"type": "string", "enum": ["discover", "open", "list", "search", "read", "checkout", "cache_status"]},
                "repository": {"type": "string", "description": "Observed owner/repo or https://github.com/owner/repo for open."},
                "ref": {"type": "string", "description": "Optional observed branch/tag/commit; defaults to HEAD. Reopen at the returned commit to research a stable version."},
                "snapshot_id": {"type": "string", "description": "ID returned by open, required for list/search/read/checkout."},
                "path": {"type": "string", "description": "Repository-relative file for read or directory prefix for list/search."},
                "query": {"type": "string", "description": "Discovery query or literal path/content search text."},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 8, "description": "Observed paths to search file contents; omit to search filenames."},
                "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1},
                "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            }, ["operation"])}},
        {"type": "function", "function": {
            "name": "wiki_write",
            "description": "Save the final topic card about a repository after your own full-draft self-check against the materials actually read. Research may use repository tools or local_shell; evidence IDs are not required. Read implementation and callers/tests when needed; docs or comments alone do not prove runtime behavior. This tool saves and reads back Markdown without calling a review model. Only a committed receipt after Markdown readback completes the write; identical committed content is reused.\n\n" + REPOSITORY_CARD_WRITING_RULES,
            "parameters": schema({
                "title": {"type": "string"}, "repository": {"type": "string", "description": "Exact observed owner/repo."},
                "commit": {"type": "string", "description": "Optional observed commit for the researched version, including research via local_shell. Omit when unknown; never invent it."},
                "topic": {"type": "string", "description": "Stable topic key, e.g. memory-system; identifies this project's article across versions."},
                "revision_id": {"type": "string", "description": "Existing card revision for editing and concurrent-update protection. Send the complete final article without section_ids, or replace existing sections by section_id."},
                "sections": {"type": "array", "minItems": 1, "maxItems": 16, "description": "Final reader-facing sections answering the user's questions with supported mechanisms and explanations. Self-check the complete draft before submitting. Section limits are capacity, not a required outline.", "items": {
                    "type": "object", "properties": {
                        "heading": {"type": "string", "description": "Visible section title expressing the reader's question or the behavior explained."},
                        "content": {"type": "string", "description": "Complete explanatory prose for this section, without repeating its heading or adding source-code links. Start with the answer, then explain mechanism, conditions and relevant consequences."},
                        "section_id": {"type": "string", "description": "Optional existing section ID when editing part of a saved card."}},
                    "required": ["heading", "content"], "additionalProperties": False}},
                "unknowns": {"type": "string", "description": "Explicit unverified aspects, without inventing implementation details."},
            }, ["title", "repository", "topic", "sections"])}},
        {"type": "function", "function": {
            "name": "read_tool_result", "description": "Recover stored output by result_id without rerunning the original tool. For Wiki select card_id and optionally a section ID/title; a multi-card result without a unique selection returns a directory. Search checks card titles then actual Markdown, never request metadata. Returns the snapshot hash, character range and next_offset. Continue only missing ranges with the same card/section and next_offset, omitting query. A page or directory is not proof of full reading. Missing IDs/cards yield no items; check original status.",
            "parameters": schema({
                "result_id": {"type": "integer", "description": "result_id from a prior observation in this conversation."},
                "query": {"type": "string", "description": "Literal search text; omit for offset paging."},
                "offset": {"type": "integer", "minimum": 0, "description": "Character offset or previous next_offset; defaults to 0."},
                "card_id": {"type": "string", "description": "Card from the stored Wiki result. Wiki offsets refer to this card's Markdown."},
                "section": {"type": "string", "description": "Optional section ID (such as s3) or exact heading from the returned directory."},
                "max_chars": {"type": "integer", "minimum": 256, "maximum": 24000, "description": "Page length; defaults to 4000 characters."},
            }, ["result_id"])}},
        {
            "type": "function",
            "function": {
                "name": "wiki_open",
                "description": "Read relevant compiled Wiki pages using card_ids from the catalog or observed references. Returns reader-facing Markdown and bounded links. Answer ordinary Wiki questions from card content; state when the Wiki does not record an answer. Do not automatically return to original sources. Reuse pages already read in this run; changing query, batching or ID order does not require rereading them. Recover omitted content with read_tool_result. Set refresh=true with a concrete reason only to deliberately reread after a content update or for a specific unresolved check. Unknown or unavailable IDs fail; correct IDs or resolve a query.",
                "parameters": schema(
                    {
                        "card_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "card_id values from the catalog or observed references.",
                        },
                        "query": {"type": "string", "description": "Optional fallback query if no card_id is known."},
                        "limit": {"type": "integer", "description": "Maximum number of cards to open.", "minimum": 1, "maximum": 8},
                        "refresh": {"type": "boolean", "description": "Defaults to false. True deliberately rereads an already opened page and requires reason."},
                        "reason": {"type": "string", "description": "Concrete reason for refresh, such as a changed page or an unresolved source detail; not a generic request to read again."},
                    },
                    [],
                ),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "evidence_lookup",
                "description": "Retrieve retained original passages for opened legacy card_ids when the user explicitly requests research, source checking or a knowledge update. Ordinary Wiki answers use card content and do not need this tool; new cards may have no retained passages. Returns full text, element_id, source_packet_id, section, score and source_path for reading the original Markdown via local_shell. Large results can be recovered with read_tool_result. This tool does not perform semantic verification: matches do not prove support or full-document coverage. Search neutral facts; repeated passages mean change method. To check missing experiments, inspect source sections/appendices, not repeated searches for 'no experiment'. No matches do not prove absence.",
                "parameters": schema(
                    {
                        "query": {"type": "string", "description": "Claim or fact to verify."},
                        "card_ids": {"type": "array", "items": {"type": "string"}, "description": "Opened Wiki page IDs whose sources should be checked."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 8, "description": "Maximum number of source excerpts to return."},
                    },
                    ["query"],
                ),
            },
        },
        {"type": "function", "function": {
            "name": "arxiv",
            "description": "Search, resolve or import arXiv papers and check ingestion. Choose action: search requires query (paper name or concise topic); lookup requires arxiv_ids (batch up to 20); import requires arxiv_id from the user or observed metadata; status requires an observed job_id. Search returns candidates, not an identity guarantee: compare titles, authors and abstracts; refine an empty/ambiguous search and ask only when ambiguity remains. An explicit request to import a named paper authorizes search and import of the identified paper without another approval. Links-only requests do not authorize import. Imports are asynchronous; use status and open the resulting Wiki card. Search/lookup/status are reads; import writes. Requests are paced and retried by the service; do not duplicate failed requests with shell loops. A successful status call does not mean the ingestion job succeeded.",
            "parameters": schema({
                "action": {"type": "string", "enum": ["search", "lookup", "import", "status"]},
                "query": {"type": "string", "description": "For search: paper-title keywords or a concise topic."},
                "arxiv_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20,
                              "description": "For lookup: exact identifiers from the user or observed metadata."},
                "arxiv_id": {"type": "string", "description": "For import: the identified paper's exact arXiv ID."},
                "job_id": {"type": "string", "description": "For status: job ID returned by import or supplied by the user."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Maximum search candidates; default 5."},
                "author": {"type": "string", "description": "Optional search author filter."},
                "categories": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "year_from": {"type": "integer", "minimum": 1991, "maximum": 2100},
                "year_to": {"type": "integer", "minimum": 1991, "maximum": 2100},
                "approval_mode": {"type": "string", "enum": ["risk", "manual", "auto"], "description": "Import approval policy; default risk. Follow user policy."},
            }, ["action"])}},
    ]


REGISTERED_TOOL_NAMES = frozenset(item["function"]["name"] for item in native_tool_specs())
DEFAULT_AGENT_TOOLS = REGISTERED_TOOL_NAMES


def select_native_tool_specs(names: Iterable[str]) -> List[Dict[str, Any]]:
    """Return fresh contracts so request-specific edits cannot mutate the registry."""
    allowed = frozenset(names)
    return [item for item in native_tool_specs() if item["function"]["name"] in allowed]


def fallback_tool_specs(names: Iterable[str] | None = None) -> List[Dict[str, Any]]:
    """Expose the same descriptions and full parameter schemas to JSON routing."""
    specs = native_tool_specs() if names is None else select_native_tool_specs(names)
    return [item["function"] for item in specs]
