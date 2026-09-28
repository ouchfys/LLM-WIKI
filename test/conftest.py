"""Isolate runtime defaults before collecting tests that import app modules.

Explicit paths in tests still take priority. Empty memory/task overrides keep
those stores next to each test's own database instead of the user's .env paths.
"""
import os
from pathlib import Path
import tempfile


_workspace = tempfile.TemporaryDirectory(prefix="paperwiki-pytest-", ignore_cleanup_errors=True)
_root = Path(_workspace.name).resolve()
_defaults = {
    "PAPERWIKI_LOAD_DOTENV": "0",
    "PAPERWIKI_HOME": str(_root),
    "PAPERWIKI_WORKSPACE_ROOT": str(_root),
    "PAPERWIKI_DB_PATH": str(_root / "sessions.db"),
    "PAPERWIKI_MEMORY_ROOT": "",
    "PAPERWIKI_TASKS_ROOT": "",
    "PAPERWIKI_REPO_CACHE_ROOT": str(_root / "cache" / "repos"),
    "STORAGE_BACKEND": "local",
}
_previous = {key: os.environ.get(key) for key in _defaults}
os.environ.update(_defaults)


def pytest_unconfigure(config):
    for key, value in _previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    # Only the directory allocated above is eligible for cleanup.
    if _root.is_relative_to(Path(tempfile.gettempdir()).resolve()) and _root.name.startswith("paperwiki-pytest-"):
        _workspace.cleanup()
