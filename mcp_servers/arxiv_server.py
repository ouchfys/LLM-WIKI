"""arXiv MCP server for discovery, managed download, and Wiki ingestion."""

from __future__ import annotations

import argparse
import threading
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from system.discovery.arxiv_service import ArxivMcpService, normalize_arxiv_id


mcp = MCPServer(
    name="llm-wiki-arxiv",
    title="LLM-WIKI arXiv Gateway",
    version="1.0.0",
    description=(
        "Search arXiv, inspect paper metadata, cache a PDF, and submit it to "
        "the LLM-WIKI paper-ingestion workflow."
    ),
    instructions=(
        "Search and inspect metadata before importing. Call arxiv_import_paper only "
        "when the user asks to add a paper to the Wiki. Imports are asynchronous; "
        "poll arxiv_ingestion_status with the returned job_id."
    ),
)

_service: ArxivMcpService | None = None
_service_lock = threading.Lock()


def get_service() -> ArxivMcpService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = ArxivMcpService()
    return _service


@mcp.tool(
    name="arxiv_search",
    title="Search arXiv",
    description="Search official arXiv metadata with optional author, category, year, and sort filters.",
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
    structured_output=True,
)
def arxiv_search(
    query: Annotated[str, Field(description="Natural-language topic or paper-title keywords")],
    max_results: Annotated[int, Field(ge=1, le=50)] = 10,
    start: Annotated[int, Field(ge=0, le=1999)] = 0,
    sort_by: Literal["relevance", "lastUpdatedDate", "submittedDate"] = "relevance",
    sort_order: Literal["ascending", "descending"] = "descending",
    author: Annotated[str, Field(description="Optional author name")] = "",
    categories: Annotated[
        list[str] | None,
        Field(description="Optional arXiv categories such as cs.AI or cs.CL"),
    ] = None,
    year_from: Annotated[int | None, Field(ge=1991, le=2100)] = None,
    year_to: Annotated[int | None, Field(ge=1991, le=2100)] = None,
) -> dict[str, Any]:
    return get_service().arxiv.search(
        query,
        max_results=max_results,
        start=start,
        sort_by=sort_by,
        sort_order=sort_order,
        author=author,
        categories=categories,
        year_from=year_from,
        year_to=year_to,
    ).to_dict()


@mcp.tool(
    name="arxiv_get_paper",
    title="Get arXiv paper metadata",
    description="Resolve one arXiv ID or arXiv URL to its metadata and PDF URL.",
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
    structured_output=True,
)
def arxiv_get_paper(
    arxiv_id: Annotated[str, Field(description="arXiv ID, abs URL, or PDF URL")],
) -> dict[str, Any]:
    normalized = normalize_arxiv_id(arxiv_id)
    paper = get_service().arxiv.get_paper(normalized)
    if paper is None:
        return {"ok": False, "arxiv_id": normalized, "found": False}
    return {"ok": True, "found": True, "paper": paper.to_dict()}


@mcp.tool(
    name="arxiv_download_pdf",
    title="Download arXiv PDF",
    description=(
        "Download a paper into LLM-WIKI's managed arXiv cache. Repeated calls reuse "
        "the verified cached PDF and return its path and SHA-256."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    structured_output=True,
)
def arxiv_download_pdf(
    arxiv_id: Annotated[str, Field(description="arXiv ID, abs URL, or PDF URL")],
    force: Annotated[bool, Field(description="Replace a valid cached copy")] = False,
) -> dict[str, Any]:
    return get_service().arxiv.download_pdf(arxiv_id, force=force)


@mcp.tool(
    name="arxiv_import_paper",
    title="Import arXiv paper into LLM-WIKI",
    description=(
        "Download an arXiv PDF and submit it to the existing asynchronous parsing, "
        "evidence, verification, merge, revision, and indexing workflow."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    structured_output=True,
)
def arxiv_import_paper(
    arxiv_id: Annotated[str, Field(description="arXiv ID, abs URL, or PDF URL")],
    approval_mode: Literal["risk", "manual", "auto"] = "risk",
) -> dict[str, Any]:
    return get_service().import_paper(arxiv_id, approval_mode=approval_mode)


@mcp.tool(
    name="arxiv_ingestion_status",
    title="Read LLM-WIKI ingestion status",
    description="Read the asynchronous paper-ingestion job returned by arxiv_import_paper.",
    annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    structured_output=True,
)
def arxiv_ingestion_status(
    job_id: Annotated[str, Field(min_length=1, description="Ingestion job UUID")],
) -> dict[str, Any]:
    return get_service().ingestion.get_job(job_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the LLM-WIKI arXiv MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="stdio for a local MCP client; streamable-http for a shared service.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8011, help="HTTP bind port")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return
    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
