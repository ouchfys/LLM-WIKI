from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from system.document.arxiv_html import ArxivHtmlParser
from system.document.mineru import _read_archive, _from_markdown
from system.document.models import ParsedDocument
from system.document.parser_router import PaperParserRouter


def _arxiv_html() -> str:
    paragraphs = "".join(
        f'<p class="ltx_p" id="p{i}">Paragraph {i} contains enough structured scientific text ' + ("evidence " * 60) + "</p>"
        for i in range(12)
    )
    return f"""
    <html><body><article class="ltx_document">
      <h1 class="ltx_title">A Fast Structured Paper</h1>
      <section class="ltx_abstract"><h2>Abstract</h2><p>We present a parser routing method.</p></section>
      <h2 id="S1">1 Introduction</h2>{paragraphs}
      <h2 id="S2">2 Results</h2>
      <p>The score is <math alttext="91.2"><annotation encoding="application/x-tex">91.2</annotation></math>.</p>
      <figure id="T1"><figcaption>Table 1: Results</figcaption>
        <table><tr><th>Model</th><th>Score</th></tr><tr><td>Ours</td><td>91.2</td></tr></table>
      </figure>
      <h2 id="bib">References</h2><p class="ltx_p">Reference entry.</p>
    </article></body></html>
    """


def test_arxiv_html_passes_quality_gate_and_builds_table_evidence():
    parsed = ArxivHtmlParser().parse_html(
        _arxiv_html(),
        source_url="https://arxiv.org/html/1234.5678v2",
        source_key="paper-hash",
        arxiv_id="1234.5678v2",
    )
    assert parsed.parser == "arxiv-html"
    assert parsed.metadata["quality_gate"]["has_references"] is True
    assert parsed.metadata["bbox_available"] is False
    assert parsed.tables[0]["headers"] == [["Model", "Score"]]
    assert parsed.tables[0]["rows"] == [["Ours", "91.2"]]
    assert "$91.2$" in parsed.markdown
    assert parsed.elements and all(item["bbox"] == {} for item in parsed.elements)


class _RejectedHtml:
    def parse(self, *_args, **_kwargs):
        from system.document.arxiv_html import ArxivHtmlError
        raise ArxivHtmlError("missing references")


class _UnavailableMinerU:
    available = False


class _Fallback:
    def parse_pdf(self, _path):
        return {
            "title": "Fallback paper",
            "summary": "text",
            "metadata": {"page_count": 1},
            "blocks": [{"page": 1, "section": "Body", "text": "fallback evidence"}],
        }


def test_router_records_why_it_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr("system.document.parser_router.PAPER_PARSER_MODE", "auto")
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF fake")
    parsed = PaperParserRouter(
        html_parser=_RejectedHtml(),
        mineru_parser=_UnavailableMinerU(),
        fallback_parser=_Fallback(),
    ).parse(path, source_url="https://arxiv.org/abs/1234.5678", source_key="hash")
    assert parsed.parser == "pymupdf-fallback"
    assert parsed.metadata["degraded"] is True
    assert parsed.metadata["parser_attempts"][0]["reason"] == "missing references"


def test_mineru_archive_is_consumed_in_memory_without_extracting_files():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("full.md", "# MinerU Paper\n\n## Abstract\n\nUseful evidence.\n\n<table><tr><td>Model</td><td>Score</td></tr><tr><td>A</td><td>90</td></tr></table>")
    markdown, metadata = _read_archive(buffer.getvalue())
    parsed = _from_markdown(markdown, source_key="hash", source_path="paper.pdf")
    assert metadata["markdown_file"] == "full.md"
    assert parsed.title == "MinerU Paper"
    assert parsed.tables[0]["rows"] == [["A", "90"]]
