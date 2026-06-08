"""
Source Files — download and manage source files for ingestion.

Handles downloading Xiaohongshu images to source storage for later OCR.
Keeps files under sources/ as a local developer cache and uploads to OSS.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import List, Optional

import requests

from system.storage import get_object_storage, get_storage_layout

REPO_ROOT = Path(__file__).resolve().parents[2]

MAX_IMAGES = 12
DOWNLOAD_TIMEOUT = 30


def download_images(
    urls: List[str],
    note_id: str,
    max_count: int = MAX_IMAGES,
    timeout: int = DOWNLOAD_TIMEOUT,
) -> List[str]:
    """Download Xiaohongshu images to local storage.

    Args:
        urls: List of image URLs.
        note_id: Xiaohongshu note ID (used as subdirectory name).
        max_count: Maximum images to download (default 12).
        timeout: HTTP request timeout in seconds.

    Returns:
        List of downloaded local file paths (relative to repo root).
        Already-existing files are NOT re-downloaded and count as downloaded.
    """
    urls = _sanitize_urls(urls, max_count)
    if not urls:
        return []

    dest_dir = get_storage_layout().source_asset_dir("xiaohongshu", note_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    downloaded: List[str] = []
    for idx, url in enumerate(urls):
        local_path = _download_single(url, dest_dir, idx, timeout)
        if local_path:
            downloaded.append(str(local_path.relative_to(REPO_ROOT)))

    return downloaded


def download_image(url: str, dest_dir: Path, index: int = 0, timeout: int = DOWNLOAD_TIMEOUT) -> Optional[Path]:
    """Download a single image. Returns local path or None."""
    return _download_single(url, dest_dir, index, timeout)


def url_hash(url: str) -> str:
    """Short hash of a URL for deduplication."""
    return hashlib.md5((url or "").encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize_urls(urls: List[str], max_count: int) -> List[str]:
    """Filter and deduplicate URLs."""
    seen = set()
    result = []
    for url in urls:
        url = (url or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        url = url.split(");", 1)[0]
        url = url.split(")", 1)[0]
        url = url.split('"', 1)[0].strip()
        h = url_hash(url)
        if h not in seen:
            seen.add(h)
            result.append(url)
    return result[:max_count]


def _download_single(url: str, dest_dir: Path, index: int, timeout: int) -> Optional[Path]:
    """Download a single image. Skip if file already exists."""
    h = url_hash(url)

    # Check for existing file with same hash
    for existing in dest_dir.glob(f"{h}.*"):
        print(f"[source_files] Already downloaded: {existing}")
        return existing

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            },
            timeout=timeout,
            stream=True,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"[source_files] Download failed for {url[:80]}: {exc}")
        return None

    # Determine extension
    ext = _guess_extension(url, response)
    filename = f"{h}{ext}"
    dest = dest_dir / filename

    # Save
    try:
        with dest.open("wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        get_object_storage().upload_file(dest, content_type=response.headers.get("Content-Type", ""))
    except Exception as exc:
        print(f"[source_files] Write/upload failed for {dest}: {exc}")
        return None

    print(f"[source_files] Downloaded: {dest}")
    return dest


def _guess_extension(url: str, response) -> str:
    """Guess file extension from URL or Content-Type header."""
    # Check Content-Type
    content_type = response.headers.get("Content-Type", "")
    ct_map = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    for mime, ext in ct_map.items():
        if mime in content_type:
            return ext

    # Check URL path
    url_lower = url.lower().split("?")[0]
    for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        if ext in url_lower:
            return ext if ext != ".jpeg" else ".jpg"

    # Fall back to magic bytes if a small read is possible
    return ".jpg"
