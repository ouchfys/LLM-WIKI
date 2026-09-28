from __future__ import annotations

from pathlib import Path

import pytest

from system.discovery.arxiv_service import (
    ArxivClient,
    ArxivService,
    ArxivServiceError,
    RequestPacer,
    WikiIngestionClient,
    normalize_arxiv_id,
)


ATOM_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>1</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>1</opensearch:itemsPerPage>
  <opensearch:Query role="request" searchTerms="all:&quot;attention is all you need&quot;" />
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <updated>2023-08-02T00:41:18Z</updated>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All You Need</title>
    <summary>We propose a new simple network architecture, the Transformer.</summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <category term="cs.CL" />
    <category term="cs.LG" />
    <arxiv:primary_category term="cs.CL" />
    <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
    <link href="https://arxiv.org/abs/1706.03762v7" rel="alternate" type="text/html" />
    <link title="pdf" href="https://arxiv.org/pdf/1706.03762v7" rel="related" type="application/pdf" />
  </entry>
</feed>
"""

EMPTY_ATOM_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>0</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>0</opensearch:itemsPerPage>
</feed>
"""


class FakeResponse:
    def __init__(self, *, content: bytes = b"", json_data=None, headers=None, status_code: int = 200):
        self.content = content
        self._json_data = json_data
        self.headers = headers or {}
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed = True

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self, *, get_responses=None, post_response=None):
        self.get_responses = list(get_responses or [])
        self.post_response = post_response
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.get_responses:
            raise AssertionError("Unexpected GET")
        response = self.get_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def post(self, url, **kwargs):
        uploaded = kwargs["files"]["file"]
        self.post_calls.append({
            "url": url,
            "data": dict(kwargs["data"]),
            "filename": uploaded[0],
            "payload": uploaded[1].read(),
            "content_type": uploaded[2],
        })
        return self.post_response


def test_rate_limit_closes_response_and_shares_retry_after():
    class Pacer:
        def __init__(self):
            self.delays = []
        def wait(self):
            pass
        def defer(self, seconds):
            self.delays.append(seconds)
    limited = FakeResponse(status_code=429, headers={"Retry-After": "12"})
    session = FakeSession(get_responses=[limited, FakeResponse(content=ATOM_FEED)])
    pacer = Pacer()
    papers = ArxivClient(session=session, pacer=pacer).get_papers(["1706.03762", "2401.00001"])
    assert papers
    assert limited.closed
    assert pacer.delays == [12.0]
    assert len(session.get_calls) == 2
    assert session.get_calls[0][1]["params"]["id_list"] == "1706.03762,2401.00001"


def test_non_retryable_error_does_not_repeat_request():
    failed = FakeResponse(status_code=404)
    session = FakeSession(get_responses=[failed])
    with pytest.raises(ArxivServiceError, match="1 attempt"):
        ArxivClient(session=session, pacer=RequestPacer(0)).get_papers(["1706.03762"])
    assert len(session.get_calls) == 1
    assert failed.closed


@pytest.mark.parametrize("value", [
    "1706.03762v7",
    "arXiv:1706.03762v7",
    "https://arxiv.org/abs/1706.03762v7",
    "https://arxiv.org/pdf/1706.03762v7.pdf?download=1",
])
def test_normalize_arxiv_id(value: str) -> None:
    assert normalize_arxiv_id(value) == "1706.03762v7"


def test_normalize_legacy_arxiv_id() -> None:
    assert normalize_arxiv_id("https://arxiv.org/abs/hep-th/9901001") == "hep-th/9901001"
    with pytest.raises(ValueError):
        normalize_arxiv_id("../../secrets")


def test_search_builds_filters_and_parses_rich_metadata() -> None:
    session = FakeSession(get_responses=[FakeResponse(content=ATOM_FEED)])
    client = ArxivClient(session=session, pacer=RequestPacer(0))
    page = client.search(
        "attention is all you need",
        max_results=5,
        sort_by="submittedDate",
        author="Vaswani",
        categories=["cs.CL", "cs.AI", "../bad"],
        year_from=2017,
        year_to=2020,
    )

    assert page.total_results == 1
    assert page.papers[0].arxiv_id == "1706.03762v7"
    assert page.papers[0].primary_category == "cs.CL"
    assert page.papers[0].year == 2017
    assert page.papers[0].pdf_url.endswith("1706.03762v7")
    params = session.get_calls[0][1]["params"]
    assert 'all:"attention is all you need"' in params["search_query"]
    assert 'au:"Vaswani"' in params["search_query"]
    assert "cat:cs.CL OR cat:cs.AI" in params["search_query"]
    assert "../bad" not in params["search_query"]
    assert "submittedDate:[201701010000 TO 202012312359]" in params["search_query"]


def test_get_papers_resolves_exact_ids_with_one_api_request() -> None:
    session = FakeSession(get_responses=[FakeResponse(content=ATOM_FEED)])
    client = ArxivClient(session=session, pacer=RequestPacer(0))

    papers = client.get_papers(["1706.03762", "2401.00001", "1706.03762"])

    assert [paper.arxiv_id for paper in papers] == ["1706.03762v7"]
    params = session.get_calls[0][1]["params"]
    assert params["id_list"] == "1706.03762,2401.00001"
    assert params["max_results"] == 2


def test_search_retries_transient_arxiv_timeout() -> None:
    import requests

    session = FakeSession(get_responses=[requests.ReadTimeout("slow"), FakeResponse(content=ATOM_FEED)])
    client = ArxivClient(
        session=session,
        pacer=RequestPacer(0),
        max_attempts=2,
        retry_backoff_seconds=0,
    )

    page = client.search("attention", max_results=1)

    assert page.papers[0].arxiv_id == "1706.03762v7"
    assert len(session.get_calls) == 2


def test_search_broadens_over_specific_natural_language_query_once() -> None:
    session = FakeSession(get_responses=[
        FakeResponse(content=EMPTY_ATOM_FEED),
        FakeResponse(content=ATOM_FEED),
    ])
    client = ArxivClient(session=session, pacer=RequestPacer(0))

    page = client.search("KV cache quantization low-bit LLM serving", max_results=5)

    assert page.papers[0].arxiv_id == "1706.03762v7"
    assert len(session.get_calls) == 2
    assert session.get_calls[0][1]["params"]["search_query"] == (
        'all:"KV cache quantization low-bit LLM serving"'
    )
    assert session.get_calls[1][1]["params"]["search_query"] == 'all:"KV cache quantization"'


def test_search_deduplicates_terms_and_uses_two_term_fallback() -> None:
    session = FakeSession(get_responses=[
        FakeResponse(content=EMPTY_ATOM_FEED),
        FakeResponse(content=EMPTY_ATOM_FEED),
        FakeResponse(content=ATOM_FEED),
    ])
    client = ArxivClient(session=session, pacer=RequestPacer(0))

    page = client.search(
        "LLM agent harness, agent loop, tool use, planning, context management",
        max_results=5,
    )

    assert page.papers[0].arxiv_id == "1706.03762v7"
    queries = [call[1]["params"]["search_query"] for call in session.get_calls]
    assert queries[-2:] == ['all:"agent harness loop"', 'all:"agent harness"']


def test_pdf_download_is_validated_hashed_and_cached(tmp_path: Path) -> None:
    pdf = b"%PDF-1.7\ncontrolled test payload\n%%EOF"
    response = FakeResponse(content=pdf, headers={"Content-Length": str(len(pdf))})
    session = FakeSession(get_responses=[response])
    client = ArxivClient(session=session, pacer=RequestPacer(0), max_pdf_mb=1)

    first = client.download_pdf("1706.03762", destination_dir=tmp_path)
    second = client.download_pdf("1706.03762", destination_dir=tmp_path)

    assert first["cached"] is False
    assert second["cached"] is True
    assert first["sha256"] == second["sha256"]
    assert Path(first["path"]).read_bytes() == pdf
    assert Path(first["metadata_path"]).exists()
    assert len(session.get_calls) == 1
    assert response.closed is True


def test_non_pdf_download_is_rejected_without_cache_artifact(tmp_path: Path) -> None:
    session = FakeSession(get_responses=[FakeResponse(content=b"<html>rate limited</html>")])
    client = ArxivClient(session=session, pacer=RequestPacer(0), max_pdf_mb=1)
    with pytest.raises(ArxivServiceError, match="valid PDF"):
        client.download_pdf("1706.03762", destination_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_import_submits_to_existing_async_ingestion_endpoint(tmp_path: Path) -> None:
    pdf_path = tmp_path / "1706.03762.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nagent test\n")
    response = FakeResponse(json_data={"ok": True, "job_id": "job-1", "agent_run_id": "run-1"})
    session = FakeSession(post_response=response)
    ingestion = WikiIngestionClient(base_url="http://127.0.0.1:8000", session=session)

    class StubArxiv:
        def download_pdf(self, arxiv_id):
            return {"ok": True, "path": str(pdf_path), "cached": True, "arxiv_id": arxiv_id}

    result = ArxivService(arxiv=StubArxiv(), ingestion=ingestion).import_paper("1706.03762")

    assert result["ingestion"]["job_id"] == "job-1"
    assert "arxiv(action=status)" in result["next"]
    assert session.post_calls[0]["url"].endswith("/api/papers/ingest")
    assert session.post_calls[0]["data"] == {
        "source_url": "https://arxiv.org/abs/1706.03762",
        "pipeline": "wiki_compile",
        "approval_mode": "risk",
    }
    assert session.post_calls[0]["payload"].startswith(b"%PDF-")
