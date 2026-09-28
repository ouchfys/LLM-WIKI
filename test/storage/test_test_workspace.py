"""The test runner must never inherit live workspace and memory destinations."""
import os
from pathlib import Path
import subprocess
import sys


def test_collection_replaces_live_paths_before_app_imports(tmp_path):
    checkout = Path(__file__).resolve().parents[2]
    sentinel = tmp_path / "live project"
    sentinel.mkdir()
    marker = sentinel / "MEMORY.md"
    marker.write_text("Existing user knowledge", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(checkout),
        "PAPERWIKI_HOME": str(sentinel),
        "PAPERWIKI_WORKSPACE_ROOT": str(sentinel),
        "PAPERWIKI_DB_PATH": str(sentinel / "sessions.db"),
        "PAPERWIKI_MEMORY_ROOT": str(sentinel),
        "PAPERWIKI_TASKS_ROOT": str(sentinel / "tasks"),
        "PAPERWIKI_LOAD_DOTENV": "1",
    }
    code = '''
import os, runpy
from pathlib import Path
live = Path(os.environ['PAPERWIKI_WORKSPACE_ROOT']).resolve()
guard = runpy.run_path('test/conftest.py')
try:
    from system.core import config
    from system.conversation.session_store import SessionStore
    from system.storage.layout import get_storage_layout
    root = get_storage_layout().repo_root.resolve()
    assert root != live and not root.is_relative_to(live)
    assert os.environ['PAPERWIKI_LOAD_DOTENV'] == '0'
    store = SessionStore()
    sid = store.create_session()
    store.write_project_memory(sid, '# Project Memory\\n\\nTest only.')
    assert Path(store.db_path).is_relative_to(root)
    assert store.project_memory_files.root.is_relative_to(root)
    assert set(p.name for p in live.iterdir()) == {'MEMORY.md'}
    assert (live / 'MEMORY.md').read_text(encoding='utf-8') == 'Existing user knowledge'
finally:
    guard['pytest_unconfigure'](None)
assert os.environ['PAPERWIKI_MEMORY_ROOT'] == str(live)
assert os.environ['PAPERWIKI_HOME'] == str(live)
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=checkout, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in sentinel.iterdir()) == ["MEMORY.md"]
