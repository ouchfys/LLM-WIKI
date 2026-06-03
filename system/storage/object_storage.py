"""Object storage abstraction for durable source artifacts.

Local files are still kept as a developer cache, but callers can store and read
`oss://...` URIs directly. This keeps the ingestion/indexing path from depending
on generated Markdown under data/wiki.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from system.core import config


class ObjectStorage:
    def __init__(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.backend = (getattr(config, "STORAGE_BACKEND", "local") or "local").lower()
        self.root_prefix = (getattr(config, "STORAGE_ROOT_PREFIX", "") or "").strip("/")
        self.bucket_name = getattr(config, "OSS_BUCKET", "") or ""
        self.endpoint = getattr(config, "OSS_ENDPOINT", "") or ""
        self._bucket = None

    @property
    def enabled(self) -> bool:
        return self.backend == "oss"

    def uri_for_key(self, key: str) -> str:
        key = self._normalize_key(key)
        if self.enabled:
            return f"oss://{self.bucket_name}/{key}"
        return f"local://{key}"

    def key_for_local_path(self, path: str | Path) -> str:
        local_path = Path(path)
        if not local_path.is_absolute():
            local_path = self.repo_root / local_path
        try:
            rel = local_path.resolve().relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            rel = local_path.name
        return self._normalize_key(rel)

    def upload_text(self, key: str, text: str, content_type: str = "text/markdown; charset=utf-8") -> str:
        key = self._normalize_key(key)
        if self.enabled:
            self._get_bucket().put_object(
                key,
                text.encode("utf-8"),
                headers={"Content-Type": content_type},
            )
        return self.uri_for_key(key)

    def read_text(self, reference: str, encoding: str = "utf-8") -> str:
        """Read text from an oss:// URI, local:// URI, storage key, or local path.

        OSS reads fall back to the matching local developer cache when present.
        The fallback is deliberate: ingestion should remain usable during local
        development, while production paths can persist and replay from OSS.
        """
        reference = (reference or "").strip()
        if not reference:
            return ""

        if reference.startswith("oss://"):
            key = self.key_from_uri(reference)
            if self.enabled:
                try:
                    data = self._get_bucket().get_object(key).read()
                    return data.decode(encoding)
                except Exception:
                    cached = self.local_cache_path_for_key(key)
                    if cached and cached.exists():
                        return cached.read_text(encoding=encoding)
                    raise
            cached = self.local_cache_path_for_key(key)
            return cached.read_text(encoding=encoding) if cached and cached.exists() else ""

        if reference.startswith("local://"):
            key = self.key_from_uri(reference)
            cached = self.local_cache_path_for_key(key)
            return cached.read_text(encoding=encoding) if cached and cached.exists() else ""

        path = Path(reference)
        if not path.is_absolute():
            path = self.repo_root / reference
        if path.exists():
            return path.read_text(encoding=encoding)

        key = self._normalize_key(reference)
        if self.enabled:
            try:
                data = self._get_bucket().get_object(key).read()
                return data.decode(encoding)
            except Exception:
                return ""
        cached = self.local_cache_path_for_key(key)
        return cached.read_text(encoding=encoding) if cached and cached.exists() else ""

    def upload_file(self, path: str | Path, key: Optional[str] = None, content_type: str = "") -> str:
        local_path = Path(path)
        if not local_path.is_absolute():
            local_path = self.repo_root / local_path
        key = self._normalize_key(key or self.key_for_local_path(local_path))
        if self.enabled:
            headers = {"Content-Type": content_type} if content_type else None
            self._get_bucket().put_object_from_file(key, str(local_path), headers=headers)
        return self.uri_for_key(key)

    def exists(self, reference: str) -> bool:
        key = self.key_from_uri(reference)
        if self.enabled:
            return bool(self._get_bucket().object_exists(key))
        cached = self.local_cache_path_for_key(key)
        return bool(cached and cached.exists())

    def delete(self, reference: str) -> bool:
        key = self.key_from_uri(reference)
        if not key:
            return False
        existed = self.exists(self.uri_for_key(key) if not reference.startswith(("oss://", "local://")) else reference)
        if self.enabled:
            self._get_bucket().delete_object(key)
            return existed
        cached = self.local_cache_path_for_key(key)
        if cached and cached.exists() and cached.is_file():
            cached.unlink()
            return True
        return False

    def list(self, prefix: str = "", limit: int = 100) -> list[dict[str, object]]:
        key_prefix = self.key_from_uri(prefix) if prefix else self._normalize_key("")
        if self.enabled:
            items: list[dict[str, object]] = []
            for obj in self._iter_oss_objects(key_prefix, limit=limit):
                items.append({
                    "key": obj.key,
                    "uri": self.uri_for_key(obj.key),
                    "size": getattr(obj, "size", 0),
                    "last_modified": getattr(obj, "last_modified", None),
                })
            return items

        base = self.local_cache_path_for_key(key_prefix)
        if not base or not base.exists():
            return []
        paths = [base] if base.is_file() else list(base.rglob("*"))
        items = []
        for path in paths:
            if not path.is_file():
                continue
            key = self.key_for_local_path(path)
            items.append({
                "key": key,
                "uri": self.uri_for_key(key),
                "size": path.stat().st_size,
                "last_modified": int(path.stat().st_mtime),
            })
            if len(items) >= limit:
                break
        return items

    def delete_prefix(self, prefix: str, limit: int = 1000) -> int:
        key_prefix = self.key_from_uri(prefix)
        if not key_prefix or key_prefix in {"/", "."}:
            raise ValueError("Refusing to delete an empty object storage prefix.")

        deleted = 0
        if self.enabled:
            bucket = self._get_bucket()
            keys = [obj.key for obj in self._iter_oss_objects(key_prefix, limit=limit)]
            for index in range(0, len(keys), 1000):
                batch = keys[index:index + 1000]
                if batch:
                    bucket.batch_delete_objects(batch)
                    deleted += len(batch)
            return deleted

        cached = self.local_cache_path_for_key(key_prefix)
        if not cached or not cached.exists():
            return 0
        if cached.is_file():
            cached.unlink()
            return 1
        for path in sorted(cached.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
                deleted += 1
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
        try:
            cached.rmdir()
        except OSError:
            pass
        return deleted

    def key_from_uri(self, uri: str) -> str:
        uri = (uri or "").strip()
        if uri.startswith("oss://"):
            parsed = urlparse(uri)
            return parsed.path.lstrip("/")
        if uri.startswith("local://"):
            return uri.removeprefix("local://").strip("/")
        return self._normalize_key(uri)

    def local_cache_path_for_key(self, key: str) -> Optional[Path]:
        key = self.key_from_uri(key)
        if self.root_prefix:
            prefix = self.root_prefix.rstrip("/") + "/"
            if key.startswith(prefix):
                key = key[len(prefix):]
        if not key:
            return None
        return self.repo_root / key

    def _normalize_key(self, key: str) -> str:
        key = (key or "").replace("\\", "/").strip("/")
        if not key:
            return self.root_prefix.rstrip("/") if self.root_prefix else ""
        if self.root_prefix:
            prefix = self.root_prefix.rstrip("/")
            if key != prefix and not key.startswith(prefix + "/"):
                key = f"{prefix}/{key}"
        return key

    def _iter_oss_objects(self, key_prefix: str, limit: int = 100):
        try:
            import oss2
        except ImportError as exc:
            raise RuntimeError("STORAGE_BACKEND=oss requires `pip install oss2`.") from exc

        bucket = self._get_bucket()
        count = 0
        for obj in oss2.ObjectIterator(bucket, prefix=key_prefix):
            if count >= limit:
                break
            yield obj
            count += 1

    def _get_bucket(self):
        if self._bucket is not None:
            return self._bucket
        try:
            import oss2
        except ImportError as exc:
            raise RuntimeError("STORAGE_BACKEND=oss requires `pip install oss2`.") from exc

        access_key_id = getattr(config, "OSS_ACCESS_KEY_ID", "") or getattr(config, "ACCESS_KEY_ID", "")
        access_key_secret = getattr(config, "OSS_ACCESS_KEY_SECRET", "") or getattr(config, "ACCESS_KEY", "")
        if not self.endpoint or not self.bucket_name or not access_key_id or not access_key_secret:
            raise RuntimeError(
                "OSS config is incomplete. Required: OSS_ENDPOINT, OSS_BUCKET, "
                "OSS_ACCESS_KEY_ID/ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET/ACCESS_KEY."
            )

        auth = oss2.Auth(access_key_id, access_key_secret)
        self._bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
        return self._bucket


_STORAGE: ObjectStorage | None = None


def get_object_storage() -> ObjectStorage:
    global _STORAGE
    if _STORAGE is None:
        _STORAGE = ObjectStorage()
    return _STORAGE
