"""Bounded, versioned GitHub reading. Source caches are disposable, evidence is not.

All network and Git operations live here instead of model-generated HTTP scripts.
The default snapshot downloads only a tree and the files actually requested.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import requests

from system.agent_runtime.control import check_run_control, get_run_control
from system.agent_runtime.local_shell import LocalShell
from system.storage.layout import get_storage_layout


class RepositoryError(ValueError):
    def __init__(self, code, message, *, retry_after=None, status_code=None, url=None, response_body=None,
                 response_truncated=False, process_result=None):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after
        self.status_code = status_code
        self.url, self.response_body, self.response_truncated = url, response_body, response_truncated
        self.process_result = process_result

    def receipt(self):
        return {"kind": "operational_error", "error_code": self.code,
                "message": str(self), "retry_after": self.retry_after, "status_code": self.status_code,
                **({"url": self.url, "response_body": self.response_body, "response_truncated": self.response_truncated}
                   if self.url else {}), **(self.process_result or {})}


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _positive_env(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


class RepositoryReader:
    _lock = threading.RLock()
    _pins: dict[str, set[str]] = {}

    def __init__(self, root=None, *, http=None, max_bytes=None, ttl_seconds=None):
        self.root = Path(root or os.environ.get("PAPERWIKI_REPO_CACHE_ROOT") or get_storage_layout().repo_cache_dir).expanduser().resolve()
        self.http = http or requests.Session()
        self.max_bytes = max_bytes or _positive_env("PAPERWIKI_REPO_CACHE_MB", 256) * 1024 * 1024
        self.ttl = ttl_seconds or _positive_env("PAPERWIKI_REPO_CACHE_DAYS", 7) * 86400
        self.max_file = _positive_env("PAPERWIKI_REPO_FILE_KB", 2048) * 1024
        self.clone_limit = min(self.max_bytes, _positive_env("PAPERWIKI_REPO_CLONE_MB", 128) * 1024 * 1024)

    @staticmethod
    def repository(value):
        value = str(value or "").strip()
        if value.startswith("https://"):
            url = urlsplit(value)
            if url.hostname != "github.com" or url.username or url.password or url.port:
                raise RepositoryError("invalid_repository", "Use an HTTPS github.com repository URL or owner/repo.")
            parts = url.path.strip("/").split("/")
            if len(parts) != 2:
                raise RepositoryError("invalid_repository", "Use the repository root URL; pass a branch/tag separately as ref.")
            value = "/".join(parts)
        value = value.removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+", value) or value.split("/")[-1] in {".", ".."}:
            raise RepositoryError("invalid_repository", "Expected owner/repo from an observed repository URL.")
        return value

    def _download(self, url, limit):
        check_run_control()
        headers = {"User-Agent": "PaperWiki-repository-reader", "Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token and urlsplit(url).hostname == "api.github.com":
            headers["Authorization"] = "Bearer " + token
        try:
            with self.http.get(url, headers=headers, stream=True, timeout=(10, 25)) as response:
                if response.status_code >= 400:
                    body, truncated = bytearray(), False
                    for chunk in response.iter_content(16384):
                        check_run_control()
                        room = 16384 - len(body)
                        body.extend(chunk[:room])
                        if len(chunk) > room:
                            truncated = True
                            break
                    code = {401: "authentication_required", 403: "forbidden_or_rate_limited",
                            404: "not_found_or_private", 429: "rate_limited"}.get(response.status_code, "http_error")
                    detail = f"GitHub HTTP {response.status_code}; this is an access failure, not source evidence."
                    if response.status_code in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
                        code = "rate_limited"
                        detail += (f" API quota exhausted (limit={response.headers.get('X-RateLimit-Limit', 'unknown')}, remaining=0). "
                                   + ("An authenticated token was supplied. " if token else "No GitHub token configured; this request is anonymous. ")
                                   + "Reuse a local snapshot or use checkout once, then repository list/search/read locally. Continue other projects.")
                    raise RepositoryError(code, detail,
                                          status_code=response.status_code,
                                          retry_after=response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset"),
                                          url=url, response_body=body.decode("utf-8", errors="replace"), response_truncated=truncated)
                data = bytearray()
                deadline = time.monotonic() + 60
                for chunk in response.iter_content(65536):
                    check_run_control()
                    if time.monotonic() > deadline:
                        raise RepositoryError("timeout", "Repository download exceeded 60 seconds.")
                    if len(data) + len(chunk) > limit:
                        raise RepositoryError("size_limit", f"Response exceeds the {limit}-byte reading limit.")
                    data.extend(chunk)
                return bytes(data)
        except requests.RequestException as exc:
            raise RepositoryError("network_error", str(exc)) from exc

    def _json(self, path):
        try:
            return json.loads(self._download("https://api.github.com/" + path, min(self.max_bytes, 16 * 1024 * 1024)))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RepositoryError("invalid_response", "GitHub returned invalid JSON.") from exc

    @staticmethod
    def _size(path):
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink()) if path.exists() else 0

    def _remove(self, path):
        # Verify the final target before recursive deletion, including on Windows.
        target = path.resolve()
        if target.parent != self.root or path.is_symlink() or target == self.root:
            raise RepositoryError("invalid_cache_path", "Refusing to remove a path outside the repository cache.")
        def writable(function, name, _exc):
            os.chmod(name, 0o700)
            function(name)
        shutil.rmtree(target, onerror=writable)

    def _prune(self, extra=0, keep=None):
        self.root.mkdir(parents=True, exist_ok=True)
        candidates = sorted((p for p in self.root.iterdir() if p.is_dir() and re.fullmatch(r"[a-f0-9]{24}", p.name)),
                            key=lambda p: p.stat().st_mtime)
        for path in candidates:
            if path == keep or self._pins.get(str(path)):
                continue
            if time.time() - path.stat().st_mtime > self.ttl or self._size(self.root) + extra > self.max_bytes:
                self._remove(path)
        if self._size(self.root) + extra > self.max_bytes:
            raise RepositoryError("cache_capacity", "Repository cache is full; active snapshots are retained. Finish active research or increase PAPERWIKI_REPO_CACHE_MB.")

    def _write(self, path, payload, snapshot):
        # Reserve the temporary copy too; a crashed write must not poison a cached source.
        self._prune(len(payload), keep=snapshot)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        os.utime(snapshot, None)

    def _pin(self, folder):
        control = get_run_control()
        if control and control.run_id:
            self._pins.setdefault(str(folder), set()).add(control.run_id)
        os.utime(folder, None)

    def release(self, run_id):
        with self._lock:
            for owners in self._pins.values():
                owners.discard(run_id)

    def run(self, operation, **args):
        with self._lock:
            check_run_control()
            self._prune()
            if operation == "cache_status":
                return {"kind": "navigation", "cache_bytes": self._size(self.root), "max_bytes": self.max_bytes,
                        "ttl_seconds": self.ttl, "path": str(self.root)}
            if operation == "discover":
                query = str(args.get("query") or "").strip()
                if not query:
                    raise RepositoryError("missing_query", "Repository discovery requires a query.")
                data = self._json("search/repositories?q=" + quote(query, safe="") + "&per_page=8")
                return {"kind": "navigation", "repositories": [{k: item.get(k) for k in
                        ("full_name", "html_url", "description", "homepage", "default_branch", "archived")}
                        for item in data.get("items", [])], "total_count": data.get("total_count"),
                        "note": "Candidates only; verify project identity from its own README and code."}
            if operation == "open":
                return self._open(args.get("repository"), str(args.get("ref") or "HEAD"))
            folder, manifest = self._snapshot(args.get("snapshot_id"))
            self._pin(folder)
            if operation == "checkout":
                self._checkout(folder, manifest)
                return {**self._metadata(manifest), "kind": "navigation", "checkout_path": str(folder / "checkout"),
                        "note": "repository list/search/read now use this local snapshot without GitHub API requests. Use read to obtain source span_ids for wiki_write.",
                        "cache_bytes": self._size(self.root)}
            if operation == "read":
                return self._read(folder, manifest, args)
            if operation in {"list", "search"}:
                return self._search(folder, manifest, operation, args)
            raise RepositoryError("invalid_operation", "Use discover, open, list, search, read, checkout or cache_status.")

    @staticmethod
    def _metadata(manifest):
        return {k: manifest[k] for k in ("snapshot_id", "repository", "commit")}

    def _snapshot(self, snapshot_id):
        if not re.fullmatch(r"[a-f0-9]{24}", str(snapshot_id or "")):
            raise RepositoryError("unknown_snapshot", "Use snapshot_id returned by open.")
        folder = self.root / snapshot_id
        if folder.is_symlink():
            raise RepositoryError("invalid_cache_path", "Snapshot cannot be a symlink.")
        try:
            return folder, json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise RepositoryError("expired_snapshot", "Snapshot expired; reopen the repository at the previously observed commit.") from exc

    def _open(self, repository, ref):
        repo = self.repository(repository)
        if not ref or len(ref) > 200 or ref.startswith("-") or any(c.isspace() for c in ref):
            raise RepositoryError("invalid_ref", "Invalid branch, tag or commit.")
        # A known immutable version can reuse the on-disk manifest without network.
        if re.fullmatch(r"[a-f0-9]{40}", ref):
            sid = _hash(repo.lower() + "@" + ref)[:24]
            if (self.root / sid / "manifest.json").exists():
                folder, manifest = self._snapshot(sid)
                self._pin(folder)
                return {**self._metadata(manifest), "kind": "navigation", "cached": True, "tree_available": bool(manifest.get("tree"))}
        fallback = None
        try:
            commit_data = self._json(f"repos/{repo}/commits/{quote(ref, safe='')}")
            commit = commit_data["sha"]
        except RepositoryError as exc:
            if exc.code not in {"forbidden_or_rate_limited", "rate_limited", "network_error"}:
                raise
            # Exactly one alternative transport; git reads refs without the API quota.
            lines = self._git(["ls-remote", "https://github.com/" + repo + ".git", ref,
                               "refs/heads/" + ref, "refs/tags/" + ref, "refs/tags/" + ref + "^{}"])
            matches = [line.split() for line in lines.splitlines() if line.strip()]
            if re.fullmatch(r"[a-f0-9]{40}", ref):
                commit = ref
            elif matches:
                commit = next((sha for sha, name in matches if name.endswith("^{}")), matches[0][0])
            else:
                raise RepositoryError("ref_unresolved", "Git could not resolve this ref after API failure.") from exc
            fallback = exc.receipt()
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise RepositoryError("invalid_response", "Repository commit is not a full SHA.")
        sid = _hash(repo.lower() + "@" + commit)[:24]
        folder = self.root / sid
        folder.mkdir(exist_ok=True)
        manifest = {"snapshot_id": sid, "repository": repo, "commit": commit, "tree": [], "tree_truncated": False}
        if (folder / "manifest.json").exists():
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self._write(folder / "manifest.json", json.dumps(manifest).encode(), folder)
        self._pin(folder)
        return {**self._metadata(manifest), "kind": "navigation", "url": f"https://github.com/{repo}/tree/{commit}",
                "fallback": fallback, "note": "Use list/search to locate files, read for evidence; checkout is an optional bounded Git fallback.",
                "cache_bytes": self._size(self.root)}

    def _tree(self, folder, manifest):
        if (folder / "checkout" / ".git").exists():
            paths = self._git(["-C", str(folder / "checkout"), "ls-tree", "-r", "-z", "--name-only", manifest["commit"]]).split("\x00")
            return [{"path": p, "type": "blob"} for p in paths if p]
        if not manifest.get("tree_loaded"):
            data = self._json(f"repos/{manifest['repository']}/git/trees/{manifest['commit']}?recursive=1")
            manifest.update(tree=[{k: row[k] for k in ("path", "type", "size") if k in row} for row in data.get("tree", [])],
                            tree_truncated=bool(data.get("truncated")), tree_loaded=True)
            self._write(folder / "manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8"), folder)
        return manifest["tree"]

    def _path(self, value):
        path = str(value or "")
        if not path or "\\" in path or "\x00" in path or ":" in path or PurePosixPath(path).is_absolute() or any(p in {"..", ".git"} for p in path.split("/")):
            raise RepositoryError("invalid_path", "Use an observed repository-relative file path without traversal.")
        return path

    def _file(self, folder, manifest, path):
        path = self._path(path)
        cached = folder / "files" / _hash(path)
        if cached.exists():
            return cached.read_bytes().decode("utf-8")
        local = folder / "checkout" / path
        if (folder / "checkout" / ".git").exists():
            base = (folder / "checkout").resolve()
            if not local.resolve().is_relative_to(base) or local.is_symlink() or not local.is_file():
                raise RepositoryError("invalid_path", "Requested file is unavailable or outside the checkout.")
            if local.stat().st_size > self.max_file:
                raise RepositoryError("size_limit", "File exceeds PAPERWIKI_REPO_FILE_KB.")
            raw = local.read_bytes()
        else:
            raw = self._download(f"https://raw.githubusercontent.com/{manifest['repository']}/{manifest['commit']}/{quote(path, safe='/')}", self.max_file)
        try:
            text = raw.decode("utf-8")
            if "\x00" in text:
                raise UnicodeError()
        except UnicodeError as exc:
            raise RepositoryError("binary_file", "Requested file is not UTF-8 source text.") from exc
        self._write(cached, raw, folder)
        return text

    def _read(self, folder, manifest, args):
        path = self._path(args.get("path"))
        content = self._file(folder, manifest, path)
        lines = content.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = min(len(lines), int(args.get("end_line") or start + 119), start + 239)
        if start > len(lines) or end < start:
            raise RepositoryError("invalid_range", f"File has {len(lines)} lines; choose a nonempty valid range.")
        chosen, chars = [], 0
        for line in lines[start - 1:end]:
            if chars + len(line) + 1 > 24000:
                break
            chosen.append(line)
            chars += len(line) + 1
        if not chosen:
            raise RepositoryError("line_too_large", "Single line exceeds 24000 characters; inspect a smaller structured artifact.")
        end = start + len(chosen) - 1
        body = "\n".join(chosen)
        return {**self._metadata(manifest), "kind": "source", "path": path, "source_path": path,
                "start_line": start, "end_line": end, "total_lines": len(lines),
                "next_line": end + 1 if end < len(lines) else None, "content": body,
                "content_hash": _hash(content), "read_version": manifest["commit"],
                "url": f"https://github.com/{manifest['repository']}/blob/{manifest['commit']}/{quote(path, safe='/')}#L{start}-L{end}",
                "span_id": "repo_" + _hash(manifest["snapshot_id"] + path + str(start) + ":" + str(end) + body)[:24]}

    def _search(self, folder, manifest, operation, args):
        rows = self._tree(folder, manifest)
        query = str(args.get("query") or "")
        prefix = str(args.get("path") or "").strip("/")
        offset = max(0, int(args.get("offset") or 0))
        limit = max(1, min(100, int(args.get("limit") or 40)))
        candidates = [r for r in rows if not prefix or r["path"] == prefix or r["path"].startswith(prefix + "/")]
        if operation == "search" and args.get("paths"):
            # Explicit bounded content search; return navigation hits, then read surrounding code.
            hits, errors = [], []
            paths = list(dict.fromkeys(args["paths"]))
            if not query or len(paths) > 8:
                raise RepositoryError("invalid_search", "Content search requires a literal query and at most eight observed paths.")
            for path in paths:
                try:
                    content = self._file(folder, manifest, path)
                    for number, line in enumerate(content.splitlines(), 1):
                        if query.casefold() in line.casefold():
                            hits.append({"path": path, "line": number, "preview": line[:400]})
                except RepositoryError as exc:
                    errors.append({"path": path, **exc.receipt()})
            candidates = hits
            coverage = {"searched_paths": paths, "errors": errors, "scope": "only listed paths; no-match is not repository-wide absence"}
        else:
            if query:
                candidates = [r for r in candidates if query.casefold() in r["path"].casefold()]
            coverage = {"scope": "file paths only", "tree_truncated": manifest.get("tree_truncated", False)}
        return {**self._metadata(manifest), "kind": "navigation", **coverage,
                "matches": candidates[offset:offset + limit], "total_matches": len(candidates),
                "next_offset": offset + limit if offset + limit < len(candidates) else None}

    def _git(self, args, *, growing=None, max_growth=None):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never", "GIT_LFS_SKIP_SMUDGE": "1"}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            with subprocess.Popen(["git", "-c", "credential.interactive=false", *args], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, creationflags=flags, start_new_session=os.name != "nt") as process:
                deadline = time.monotonic() + 120
                try:
                    while True:
                        check_run_control()
                        if time.monotonic() > deadline:
                            raise RepositoryError("timeout", "Git exceeded its 120-second limit.")
                        if growing and self._size(growing) > max_growth:
                            raise RepositoryError("cache_capacity", "Git snapshot exceeded its reserved cache budget.")
                        try:
                            out, err = process.communicate(timeout=0.1)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                    if growing and self._size(growing) > max_growth:
                        raise RepositoryError("cache_capacity", "Git snapshot exceeded its reserved cache budget.")
                    if process.returncode:
                        raise RepositoryError("git_failed", f"Git exited with code {process.returncode}", process_result={
                            "command": ["git", "-c", "credential.interactive=false", *args],
                            "exit_code": process.returncode, "stdout": out.decode("utf-8", "replace"),
                            "stderr": err.decode("utf-8", "replace")})
                    return out.decode("utf-8", "replace")
                finally:
                    if process.poll() is None:
                        LocalShell._stop_process_tree(process)
                        process.communicate(timeout=5)
        except OSError as exc:
            raise RepositoryError("git_unavailable", str(exc)) from exc

    def _checkout(self, folder, manifest):
        target = folder / "checkout"
        if (target / ".git").exists() and manifest.get("checked_out"):
            return
        self._prune(self.clone_limit, keep=folder)
        target.mkdir(exist_ok=True)
        try:
            self._git(["init", str(target)])
            self._git(["-C", str(target), "fetch", "--depth=1", "--no-tags", "https://github.com/" + manifest["repository"] + ".git", manifest["commit"]], growing=target, max_growth=self.clone_limit)
            self._git(["-C", str(target), "-c", "core.hooksPath=", "-c", "core.autocrlf=false", "checkout", "--detach", "FETCH_HEAD"], growing=target, max_growth=self.clone_limit)
            manifest["checked_out"] = True
            self._write(folder / "manifest.json", json.dumps(manifest).encode(), folder)
        except BaseException:
            # A failed checkout is disposable; preserve the manifest and downloaded evidence.
            resolved = target.resolve()
            if resolved.parent == folder.resolve() and resolved.is_relative_to(self.root) and not target.is_symlink():
                def writable(function, name, _exc):
                    os.chmod(name, 0o700)
                    function(name)
                shutil.rmtree(resolved, onerror=writable)
            raise
