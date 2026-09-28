"""A configured project owns runtime state even when launched from elsewhere."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("separate_database_directory", [False, True])
@pytest.mark.parametrize("root_variable", ["PAPERWIKI_HOME", "PAPERWIKI_WORKSPACE_ROOT"])
def test_runtime_writes_and_reads_stay_in_configured_workspace(tmp_path, separate_database_directory, root_variable):
    root = tmp_path / "测试 workspace"
    root.mkdir()
    db = root / "custom" / "runtime.db" if separate_database_directory else root / "sessions" / "sessions.db"
    checkout = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "PYTHONPATH": str(checkout),
        "PAPERWIKI_HOME": str(root) if root_variable == "PAPERWIKI_HOME" else "",
        "PAPERWIKI_WORKSPACE_ROOT": str(root),
        "PAPERWIKI_DB_PATH": str(db) if separate_database_directory else "",
        "PAPERWIKI_MEMORY_ROOT": str(root / "memory"),
        "PAPERWIKI_TASKS_ROOT": str(root / "projects" / "tasks"),
        "PAPERWIKI_REPO_CACHE_ROOT": str(root / "cache" / "repos"),
        "STORAGE_BACKEND": "local",
        "PYTHONIOENCODING": "utf-8",
    }
    code = r'''
import json, os, sqlite3
from pathlib import Path
from system.storage.layout import get_storage_layout, resolve_database_path
from system.storage.object_storage import get_object_storage
from system.conversation.session_store import SessionStore
from system.agent_runtime.store import AgentRunStore
from system.agent_runtime.research_sources import ResearchSourceStore
from system.agent_runtime.research_ledger import ResearchTaskLedgerStore
from system.agent_runtime.task_plans import TaskPlanStore
from system.agent_runtime.local_shell import LocalShell
from system.agent_runtime.repositories import RepositoryReader
from system.wiki.wiki_store import WikiStore
from system.wiki.chunk_index import WikiChunkIndex
from system.wiki.wiki_search_index import WikiSearchIndex
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.ingestion_jobs import IngestionJobStore
from system.wiki.maintenance.store import WikiMaintenanceStore
from system.wiki.maintenance.validator import WikiValidator
from system.wiki.maintenance.index_generator import WikiIndexGenerator
from system.wiki.raw_source_vault import RawSourceVault
from system.paper_index.store import PaperIndexStore
from system.recommender.monthly_reads import MonthlyReadingStore
from system.memory.learning_profile import LearningProfileStore

root = Path(os.environ['PAPERWIKI_WORKSPACE_ROOT']).resolve()
layout = get_storage_layout()
expected_db = Path(os.environ['PAPERWIKI_DB_PATH']) if os.environ['PAPERWIKI_DB_PATH'] else root / 'sessions' / 'sessions.db'
session = SessionStore()
wiki = WikiStore()
run_store = AgentRunStore()
chunks = WikiChunkIndex()
stores = [session, wiki, run_store, chunks, ResearchSourceStore(), ResearchTaskLedgerStore(),
          WikiSearchIndex(), PaperWikiPipelineStore(), IngestionJobStore(), WikiMaintenanceStore(),
          WikiValidator(), WikiIndexGenerator(), PaperIndexStore(), MonthlyReadingStore(),
          LearningProfileStore(session)]
assert all(Path(store.db_path) == expected_db for store in stores)
assert resolve_database_path(root / 'explicit' / 'custom.db') == root / 'explicit' / 'custom.db'
assert Path(TaskPlanStore().db_path) == expected_db
assert not (root / 'projects' / 'tasks').exists()
assert RepositoryReader().root == root / 'cache' / 'repos'
sid = session.create_session('Workspace test')
session.save_message(sid, 'user', 'Remember this workspace')
session.project_memory_files.write_memory('paperwiki-default', 'Workspace test', '# Project Memory\n\nA test decision.')
run_store.create_run(run_type='wiki_chat', source_uri=sid, context={'session_id': sid, 'require_session': True})
assert session.get_messages(sid)[0]['content'] == 'Remember this workspace'
assert (root / 'memory' / 'projects' / 'paperwiki-default' / 'MEMORY.md').is_file()

raw = RawSourceVault().write_source('notes', 'Workspace source', 'Original source text')
assert raw == 'sources/notes/workspace-source.md'
assert get_object_storage().read_text(raw).endswith('Original source text\n')
card_id = wiki.create_card(title='Workspace knowledge', page_type='ConceptPage',
                          content_json={'reading_guide': 'An isolated Wiki body.'})
card = wiki.get_card(card_id)
assert card['markdown_path'].startswith('wiki/')
assert (root / card['markdown_path']).is_file()
assert 'An isolated Wiki body.' in chunks._read_markdown(card['markdown_path'])
assert 'An isolated Wiki body.' in wiki.vault.read_reference(card['markdown_path'])

layout.projects_dir.mkdir(exist_ok=True)
shell = LocalShell()
assert shell.cwd == layout.scratch_dir()
command = "Set-Content -LiteralPath 'probe.txt' -Value 'temporary output'" if os.name == 'nt' else "printf 'temporary output' > probe.txt"
result = shell.run(command, shell='powershell' if os.name == 'nt' else 'bash')
assert result['exit_code'] == 0, result
assert (layout.scratch_dir() / 'probe.txt').read_text(encoding='utf-8-sig').strip() == 'temporary output'
assert not (root / 'probe.txt').exists()
session.save_tool_result(sid, 'local_shell', {'command': command}, result)

# API path resolvers must use the same root as the stores, including uploads.
from backend.api import papers, wiki as wiki_api
from system.document import source_files
assert papers.REPO_ROOT == wiki_api.REPO_ROOT == source_files.REPO_ROOT == root
assert papers.UPLOAD_DIR.is_relative_to(root)
assert wiki_api.EVALUATION_RUNS_DIR.is_relative_to(root)
with sqlite3.connect(expected_db) as c:
    assert c.execute('select count(*) from messages').fetchone()[0] == 1
    assert c.execute('select count(*) from session_tool_results').fetchone()[0] == 1
    assert c.execute('select count(*) from agent_runs').fetchone()[0] == 1
    assert c.execute('select count(*) from wiki_pages').fetchone()[0] == 1
print(json.dumps({'database': str(expected_db), 'card_path': card['markdown_path']}))
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert Path(report["database"]) == db
    assert not (tmp_path / "sessions.db").exists()
