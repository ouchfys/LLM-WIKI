import json
import os
import time

import pytest

from system.agent_runtime.repositories import RepositoryReader, RepositoryError


SHA = "a" * 40


class Response:
    def __init__(self, data, status=200):
        self.raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.status_code, self.headers = status, {"Retry-After": "60"}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def iter_content(self, _size):
        yield self.raw


class HTTP:
    def __init__(self):
        self.urls = []
    def get(self, url, **kwargs):
        self.urls.append(url)
        if "/commits/" in url:
            return Response({"sha": SHA})
        if "/git/trees/" in url:
            return Response({"tree": [{"path": "src/memory.py", "type": "blob", "size": 200},
                                       {"path": "README.md", "type": "blob", "size": 100}], "truncated": False})
        return Response(b"def load_memory():\n    return session.read()\n\ndef save_memory(text):\n    session.write(text)\n")


def test_versioned_remote_read_search_and_cache_reuse(tmp_path):
    http = HTTP()
    reader = RepositoryReader(tmp_path / "repos", http=http)
    opened = reader.run("open", repository="https://github.com/example/agent")
    sid = opened["snapshot_id"]
    paths = reader.run("search", snapshot_id=sid, query="memory")
    assert paths["matches"][0]["path"] == "src/memory.py"
    found = reader.run("search", snapshot_id=sid, query="save_memory", paths=["src/memory.py"])
    assert found["matches"][0]["line"] == 4
    page = reader.run("read", snapshot_id=sid, path="src/memory.py", start_line=4, end_line=5)
    assert page["content"].startswith("def save_memory") and page["next_line"] is None
    assert SHA in page["url"] and page["span_id"].startswith("repo_")
    requests = len(http.urls)
    assert reader.run("open", repository="example/agent", ref=SHA)["cached"]
    assert reader.run("read", snapshot_id=sid, path="src/memory.py", start_line=4)["span_id"] == page["span_id"]
    assert len(http.urls) == requests
    assert reader.run("cache_status")["cache_bytes"] <= reader.max_bytes


def test_http_failure_preserves_code_and_retry_information(tmp_path):
    class Forbidden:
        def get(self, *args, **kwargs):
            return Response({}, 403)
    reader = RepositoryReader(tmp_path, http=Forbidden())
    with pytest.raises(RepositoryError) as caught:
        reader.run("discover", query="Hermes agent")
    assert caught.value.receipt()["kind"] == "operational_error"
    assert caught.value.code == "forbidden_or_rate_limited"
    assert caught.value.retry_after == "60"


def test_api_quota_uses_one_git_ref_fallback(tmp_path, monkeypatch):
    reader = RepositoryReader(tmp_path, http=HTTP())
    def denied(*args):
        raise RepositoryError("rate_limited", "quota")
    monkeypatch.setattr(reader, "_json", denied)
    calls = []
    monkeypatch.setattr(reader, "_git", lambda args, **kw: calls.append(args) or SHA + "\tHEAD\n")
    opened = reader.run("open", repository="example/agent")
    assert opened["commit"] == SHA and len(calls) == 1
    assert opened["fallback"]["error_code"] == "rate_limited"


def test_ttl_eviction_does_not_remove_pinned_or_non_cache_directories(tmp_path):
    reader = RepositoryReader(tmp_path / "repos", http=HTTP(), ttl_seconds=1)
    first = reader.run("open", repository="example/first")["snapshot_id"]
    second = reader.run("open", repository="example/second")["snapshot_id"]
    untouched = reader.root / "user-files"
    untouched.mkdir()
    for name in (first, second, "user-files"):
        os.utime(reader.root / name, (time.time() - 10, time.time() - 10))
    reader._pins[str(reader.root / first)] = {"running-task"}
    try:
        reader.run("cache_status")
        assert (reader.root / first).exists()
        assert not (reader.root / second).exists()
        assert untouched.exists()
    finally:
        reader.release("running-task")


def test_size_bound_rejects_response_before_writing_file(tmp_path):
    reader = RepositoryReader(tmp_path, http=HTTP())
    sid = reader.run("open", repository="example/agent")["snapshot_id"]
    reader.max_file = 5
    with pytest.raises(RepositoryError, match="limit"):
        reader.run("read", snapshot_id=sid, path="README.md")
    assert not (tmp_path / sid / "files").exists()


@pytest.mark.parametrize("path", ["../secret", "/absolute", ".git/config", "a/../../b", "C:/secret", "a\\b"])
def test_read_refuses_paths_outside_source_tree(tmp_path, path):
    reader = RepositoryReader(tmp_path, http=HTTP())
    sid = reader.run("open", repository="example/agent")["snapshot_id"]
    with pytest.raises(RepositoryError):
        reader.run("read", snapshot_id=sid, path=path)


def test_clone_limit_failure_discards_only_partial_checkout(tmp_path, monkeypatch):
    reader = RepositoryReader(tmp_path, http=HTTP(), max_bytes=100000)
    reader.clone_limit = 1000
    sid = reader.run("open", repository="example/agent")["snapshot_id"]
    def git(args, **kwargs):
        if "fetch" in args:
            (tmp_path / sid / "checkout" / "partial").write_text("partial")
            raise RepositoryError("cache_capacity", "over budget")
        return ""
    monkeypatch.setattr(reader, "_git", git)
    with pytest.raises(RepositoryError, match="budget"):
        reader.run("checkout", snapshot_id=sid)
    assert (tmp_path / sid / "manifest.json").exists()
    assert not (tmp_path / sid / "checkout").exists()
