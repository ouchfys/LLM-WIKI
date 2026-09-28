"""Data-home defaults and legacy paths must not create a second live store."""
from pathlib import Path
import sqlite3

import pytest

from system.storage import layout as layout_module
from system.storage.layout import StorageLayout, resolve_database_path


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    for name in ("PAPERWIKI_HOME", "PAPERWIKI_WORKSPACE_ROOT", "PAPERWIKI_DB_PATH",
                 "PAPERWIKI_MEMORY_ROOT", "PAPERWIKI_TASKS_ROOT", "PAPERWIKI_REPO_CACHE_ROOT"):
        monkeypatch.setenv(name, "")
    root = tmp_path / "data home"
    monkeypatch.setattr(layout_module, "_LAYOUT", StorageLayout(root))
    return root


def test_home_priority_and_default_are_independent_of_checkout(isolated_home, tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    assert StorageLayout().data_root == user_home / ".paperwiki"
    monkeypatch.setenv("PAPERWIKI_WORKSPACE_ROOT", str(tmp_path / "legacy"))
    assert StorageLayout().data_root == tmp_path / "legacy"
    monkeypatch.setenv("PAPERWIKI_HOME", str(isolated_home))
    assert StorageLayout().data_root == isolated_home
    assert StorageLayout(tmp_path / "explicit").data_root == tmp_path / "explicit"
    assert not isolated_home.exists()


def test_layout_computes_paths_without_creating_directories(isolated_home):
    layout = StorageLayout(isolated_home)
    assert layout.repo_root == layout.data_root == isolated_home
    assert layout.database_path == isolated_home / "sessions" / "sessions.db"
    assert layout.memory_dir == isolated_home / "memory"
    assert layout.repo_cache_dir == isolated_home / "cache" / "repos"
    assert layout.logs_dir == isolated_home / "logs"
    assert layout.projects_dir == isolated_home / "projects"
    assert layout.queries_dir == isolated_home / "archives" / "queries"
    assert layout.maintenance_dir == isolated_home / "maintenance"
    assert layout.scratch_dir("session-123") == isolated_home / "tmp" / "session-123"
    assert layout.scratch_dir() == isolated_home / "tmp" / "default"
    assert layout.scratch_dir("../../escape").parent == layout.tmp_dir
    assert layout.scratch_dir("CON").name == "_con"
    assert not isolated_home.exists()


def test_existing_root_database_is_used_without_starting_empty_store(isolated_home):
    isolated_home.mkdir()
    legacy = isolated_home / "sessions.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("CREATE TABLE migration_probe(value TEXT)")
        conn.execute("INSERT INTO migration_probe VALUES ('existing')")
    assert resolve_database_path() == legacy
    assert not (isolated_home / "sessions").exists()
    from system.conversation.session_store import SessionStore
    store = SessionStore()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT value FROM migration_probe").fetchone()[0] == "existing"
    assert store.project_memory_files.root == isolated_home / "memory"
    assert not (isolated_home / ".paperwiki").exists()


def test_new_database_precedes_legacy_and_explicit_paths_override(isolated_home, tmp_path, monkeypatch):
    layout = StorageLayout(isolated_home)
    layout.sessions_dir.mkdir(parents=True)
    (isolated_home / "sessions.db").touch()
    canonical = layout.sessions_dir / "sessions.db"
    canonical.touch()
    assert resolve_database_path() == canonical
    override = tmp_path / "override" / "state.db"
    monkeypatch.setenv("PAPERWIKI_DB_PATH", str(override))
    assert resolve_database_path(create_parent=False) == override
    assert not override.parent.exists()
    explicit = tmp_path / "explicit" / "state.db"
    assert resolve_database_path(explicit) == explicit
    assert explicit.parent.exists()
    assert not override.parent.exists()


def test_default_stores_share_home_and_explicit_db_keeps_memory_isolated(isolated_home, tmp_path, monkeypatch):
    from system.agent_runtime.repositories import RepositoryReader
    from system.agent_runtime.task_plans import TaskPlanStore
    from system.conversation.session_store import SessionStore
    configured_memory = isolated_home / "custom-memory"
    monkeypatch.setenv("PAPERWIKI_MEMORY_ROOT", str(configured_memory))
    application = SessionStore()
    assert Path(application.db_path) == isolated_home / "sessions" / "sessions.db"
    assert application.project_memory_files.root == configured_memory
    assert Path(TaskPlanStore().db_path) == Path(application.db_path)
    assert RepositoryReader().root == isolated_home / "cache" / "repos"
    assert not RepositoryReader().root.exists()
    assert not (isolated_home / ".paperwiki").exists()
    probe = configured_memory / "probe.txt"
    probe.write_text("live memory", encoding="utf-8")
    explicit_database = tmp_path / "isolated" / "state.db"
    isolated = SessionStore(db_path=str(explicit_database))
    assert isolated.project_memory_files.root == explicit_database.parent / "memory"
    assert probe.read_text(encoding="utf-8") == "live memory"
    explicit_memory = tmp_path / "explicit-memory"
    assert SessionStore(str(explicit_database), str(explicit_memory)).project_memory_files.root == explicit_memory
    monkeypatch.setenv("PAPERWIKI_REPO_CACHE_ROOT", str(tmp_path / "repo-override"))
    assert RepositoryReader().root == tmp_path / "repo-override"


def test_legacy_memory_is_read_in_place_without_creating_a_second_memory(isolated_home):
    from system.conversation.session_store import SessionStore
    legacy = isolated_home / ".paperwiki" / "memory"
    legacy.mkdir(parents=True)
    store = SessionStore()
    assert store.project_memory_files.root == legacy
    assert not (isolated_home / "memory").exists()
