import sqlite3
from unittest.mock import Mock

import pytest

from system.core import bailian_embeddings as module
from system.core.bailian_embeddings import BailianEmbeddings
from system.wiki.wiki_search_index import WikiSearchIndex


def response(status=200, data=None):
    return Mock(status_code=status, json=Mock(return_value=data))


def rows(count):
    return {"output": {"embeddings": [{"text_index": i, "embedding": [float(i + 1)] * 256} for i in reversed(range(count))]}}


def test_batches_and_response_order_follow_input_order(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response(data=rows(len(kwargs["json"]["input"]["texts"])))
    monkeypatch.setattr(module.requests, "post", post)
    client = BailianEmbeddings(api_key="private-key", dimensions=256, batch_size=100)
    vectors = client.embed_documents([str(i) for i in range(23)])
    assert [len(call[1]["json"]["input"]["texts"]) for call in calls] == [20, 3]
    assert [v[0] for v in vectors] == list(range(1, 21)) + [1, 2, 3]
    url, kwargs = calls[0]
    assert url.endswith("/api/v1/services/embeddings/text-embedding/text-embedding")
    assert kwargs["json"]["parameters"]["dimension"] == 256
    assert kwargs["json"]["parameters"]["text_type"] == "document"
    assert kwargs["headers"]["Authorization"] == "Bearer private-key"
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("data", [
    {"data": []},
    {"data": [{"text_index": 1, "embedding": [1.] * 256}]},
    {"data": [{"text_index": 0, "embedding": [1.] * 1024}]},
    {"data": [{"text_index": 0, "embedding": [float('nan')] * 256}]},
    {"data": [{"text_index": 0, "embedding": [0.] * 256}]},
    {"data": [{"text_index": 0, "embedding": "invalid"}]},
])
def test_rejects_unusable_vectors(monkeypatch, data):
    monkeypatch.setattr(module.requests, "post", lambda *a, **k: response(data={"output": {"embeddings": data["data"]}}))
    with pytest.raises(RuntimeError, match="invalid vector"):
        BailianEmbeddings(api_key="test", dimensions=256).embed_documents(["test"])


def test_duplicate_batch_indices_cannot_silently_corrupt_index(monkeypatch):
    data = {"output": {"embeddings": [{"text_index": 0, "embedding": [1.] * 256}] * 2}}
    monkeypatch.setattr(module.requests, "post", lambda *a, **k: response(data=data))
    with pytest.raises(RuntimeError, match="invalid vector"):
        BailianEmbeddings(api_key="test", dimensions=256).embed_documents(["paper A", "paper B"])


def test_compatible_base_url_is_translated_to_native_endpoint():
    client = BailianEmbeddings(api_key="test", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/")
    assert client.url == "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"


def test_flash_native_index_field_is_supported(monkeypatch):
    data = {"output": {"embeddings": [
        {"index": 1, "embedding": [2.] * 256, "type": "dense"},
        {"index": 0, "embedding": [1.] * 256, "type": "dense"},
    ]}}
    monkeypatch.setattr(module.requests, "post", lambda *a, **k: response(data=data))
    assert [v[0] for v in BailianEmbeddings(api_key="test", dimensions=256).embed_documents(["A", "B"])] == [1., 2.]


def test_transient_error_retried_but_auth_failure_sanitized(monkeypatch):
    post = Mock(side_effect=[response(429), response(data=rows(1))])
    monkeypatch.setattr(module.requests, "post", post)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    client = BailianEmbeddings(api_key="private-key", dimensions=256)
    assert len(client.embed_query("query")) == 256
    assert len(client.embed_query("query")) == 256
    assert post.call_count == 2  # repeated query uses cache
    assert post.call_args.kwargs["json"]["parameters"]["text_type"] == "query"
    post.reset_mock(side_effect=True)
    post.return_value = response(401, {"error": "private-key with request text"})
    with pytest.raises(RuntimeError) as error:
        client.embed_query("another query")
    assert str(error.value) == "Bailian embedding HTTP 401"
    assert post.call_count == 1


def test_timeout_retry_is_bounded(monkeypatch):
    post = Mock(side_effect=module.requests.Timeout("private-key"))
    monkeypatch.setattr(module.requests, "post", post)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="timed out"):
        BailianEmbeddings(api_key="test").embed_query("query")
    assert post.call_count == 2


def test_model_migration_and_concurrent_edit_do_not_reuse_stale_vectors(tmp_path):
    class Embedder:
        model = "same-model"
        index_model = "bailian:same-model:256:v1"
        def embed_documents(self, texts):
            return [[1., 0.] for _ in texts]
        def embed_query(self, text):
            return [1., 0.]

    index = WikiSearchIndex(db_path=str(tmp_path / "index.db"), embedder=Embedder())
    index.replace_page({"id": "p", "title": "Paper", "page_type": "PaperPage", "summary": "original", "content_json": {}})
    assert index.backfill_embeddings() > 0
    assert index.pending_embedding_count(index.embedder.index_model) == 0
    index.embedder.index_model = "bailian:same-model:1024:v1"
    assert index.pending_embedding_count(index.embedder.index_model) > 0
    assert index.search_vector("query") == []  # old vectors cannot participate

    def concurrent_edit(texts):
        with sqlite3.connect(index.db_path) as conn:
            conn.execute("UPDATE wiki_search_units SET content_hash='new-content', embedding=NULL")
        return [[1., 0.] for _ in texts]
    index.embedder.embed_documents = concurrent_edit
    assert index.backfill_embeddings() == 0
    assert index.pending_embedding_count(index.embedder.index_model) > 0
