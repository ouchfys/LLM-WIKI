"""Lightweight, parser-neutral document ingestion adapters."""

from system.document.models import ParsedDocument
from system.document.parser_router import PaperParserRouter

__all__ = ["ParsedDocument", "PaperParserRouter"]
