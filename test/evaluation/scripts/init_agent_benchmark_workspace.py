"""Create isolated, reproducible PaperWiki workspaces for Agent benchmark v1."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

VERSION = "paperwiki_agent_eval_v1"
DEFAULT_BASE = REPO_ROOT / "test" / "evaluation" / "workspaces" / VERSION
DEFAULT_MANIFEST = REPO_ROOT / "test" / "evaluation" / "datasets" / "agent_benchmark_v1" / "manifest.json"

TASKS = {
    "agentic_rl": (
        "在固定的 30 篇 Agentic RL 论文快照内提出有证据、可验证、可执行的研究方向；"
        "禁止用 Web 补充语料，论文事实与 Agent 综合判断必须分开。"
    ),
    "kv_cache": (
        "从空白任务库开始，自主发现、筛选并入库 KV Cache 一手论文，形成技术分类、"
        "条件对齐、开放问题和研究路线图。"
    ),
    "agent_harness": (
        "自主研究 Agent Harness 的规划、工具、上下文、记忆、状态、恢复、追踪、评测与安全，"
        "生成绑定来源且结合 PaperWiki 实现的面试指导书。"
    ),
}

TASK_MEMORY = {
    "agent_harness": """# Project Memory

## Current Goal

研究 Agent Harness，并把论文/官方资料与 PaperWiki 的实际工程实现分开陈述，生成可用于面试复习的指导书。

## Implementation Baseline

- 上下文：每轮按预算组合 purpose.md、完整有界 MEMORY.md、压缩检查点、近期原文和工具观察；摘要不是事实源，必要时按消息 ID 回读原文。
- 记忆分层：会话状态、项目记忆、用户偏好、Wiki 论文知识分开存储；项目历史只能同项目检索，不能跨项目读取。
- 记忆写入：DeepSeek V4 Flash 负责多轮问题改写和结构化候选提取；Python 校验证据、作用域、置信度、TTL、重复与冲突，新约束可 supersede 旧约束并保留审计记录。
- Agent Runtime：任务状态与中间产物持久化到 SQLite，支持 checkpoint、worker lease、heartbeat、sweeper、失败重试、恢复扫描和人工审批。
- 工具治理：Function Calling 后还有确定性策略层；arXiv 导入 ID 必须来自已观察的搜索结果，固定 Wiki/no-Web 任务会阻止外部工具，项目读取受作用域约束。
- Trace：聊天和论文入库统一记录 run/event/checkpoint/approval，包含节点状态、模型/工具耗时、Token、失败和重试。
- 本地化：评测 workspace、SQLite、PDF、Markdown Wiki 和索引均使用 local storage，不依赖 OSS。
- 删除语义：清理会话会删除消息、工具结果、压缩检查点、会话状态及该会话关联的 Agent Runtime 审计；项目长期记忆和用户偏好独立保留。

## Constraints

- 上述内容是当前代码实现基线，不是论文证据；回答中必须与论文结论、官方框架文档和未来方案分栏或显式区分。
- 不得把计划中的能力写成已实现；不确定的代码事实标为待核验。

## Boundary

- 论文事实、Claim、Evidence 与综述进入 Wiki；这里仅保存跨会话目标、工程实现基线和评测约束。
""",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--task", choices=["all", *TASKS], default="all")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete and recreate only the selected task directories under workspace-base.",
    )
    return parser.parse_args()


def _safe_reset(task_root: Path, workspace_base: Path) -> None:
    resolved_task = task_root.resolve()
    resolved_base = workspace_base.resolve()
    if resolved_task == resolved_base or resolved_base not in resolved_task.parents:
        raise RuntimeError(f"Refusing to reset path outside benchmark workspace: {resolved_task}")
    if resolved_task.exists():
        shutil.rmtree(resolved_task)


def _configure_environment(task_root: Path, db_path: Path) -> None:
    os.environ["PAPERWIKI_WORKSPACE_ROOT"] = str(task_root)
    os.environ["PAPERWIKI_DB_PATH"] = str(db_path)
    os.environ["PAPERWIKI_MEMORY_ROOT"] = str(task_root / ".paperwiki" / "memory")
    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["STORAGE_TENANT_ID"] = "paperwiki-agent-eval-v1"
    os.environ["STORAGE_ROOT_PREFIX"] = "users/paperwiki-agent-eval-v1"


def _initialize_schema(db_path: Path, memory_root: Path) -> None:
    from system.agent_runtime import AgentRunStore
    from system.conversation.session_store import SessionStore
    from system.paper_index.store import PaperIndexStore
    from system.wiki.chunk_index import WikiChunkIndex
    from system.wiki.ingestion_jobs import IngestionJobStore
    from system.wiki.maintenance.query_archive import QueryArchive
    from system.wiki.maintenance.store import WikiMaintenanceStore
    from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
    from system.wiki.wiki_store import WikiStore

    SessionStore(str(db_path), memory_root=str(memory_root))
    PaperIndexStore(str(db_path))
    WikiStore(str(db_path))
    WikiChunkIndex(str(db_path))
    PaperWikiPipelineStore(str(db_path))
    IngestionJobStore(str(db_path))
    AgentRunStore(str(db_path))
    WikiMaintenanceStore(str(db_path))
    QueryArchive(str(db_path))


def _stamp_workspace(db_path: Path, task: str, purpose: str, manifest_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agent_eval_workspace (
                   version TEXT PRIMARY KEY,
                   task TEXT NOT NULL,
                   purpose TEXT NOT NULL,
                   manifest_path TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """INSERT OR REPLACE INTO agent_eval_workspace
               (version, task, purpose, manifest_path, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (VERSION, task, purpose, str(manifest_path.resolve()), now),
        )
        conn.execute(
            """UPDATE projects
               SET name=?, purpose=?, purpose_evidence='benchmark manifest',
                   purpose_updated_at=?, updated_at=?
               WHERE id='paperwiki-default'""",
            (f"PaperWiki Agent Eval · {task}", purpose, now, now),
        )
        conn.commit()


def _sync_model_facing_project_files(task_root: Path, task: str, purpose: str) -> None:
    from system.memory.project_files import ProjectMemoryFiles

    files = ProjectMemoryFiles(task_root / ".paperwiki" / "memory")
    project_id = "paperwiki-default"
    project_name = f"PaperWiki Agent Eval · {task}"
    files.write_purpose(
        project_id,
        project_name,
        purpose,
        evidence="benchmark manifest",
        updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if task in TASK_MEMORY:
        files.write_memory(project_id, project_name, TASK_MEMORY[task])


def initialize_task(workspace_base: Path, manifest_path: Path, task: str, reset: bool) -> dict:
    task_root = (workspace_base / task).resolve()
    if reset:
        _safe_reset(task_root, workspace_base)
    task_root.mkdir(parents=True, exist_ok=True)
    for name in ("sources", "wiki", "queries", "maintenance", "runs", "snapshots"):
        (task_root / name).mkdir(parents=True, exist_ok=True)
    db_path = task_root / f"{VERSION}.db"
    _configure_environment(task_root, db_path)
    _initialize_schema(db_path, task_root / ".paperwiki" / "memory")
    _stamp_workspace(db_path, task, TASKS[task], manifest_path)
    _sync_model_facing_project_files(task_root, task, TASKS[task])
    with sqlite3.connect(db_path) as conn:
        initial_wiki_pages = int(
            conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
        )
    descriptor = {
        "version": VERSION,
        "task": task,
        "purpose": TASKS[task],
        "workspace_root": str(task_root),
        "db_path": str(db_path),
        "manifest_path": str(manifest_path.resolve()),
        "storage_backend": "local",
        "initial_wiki_pages": initial_wiki_pages,
    }
    (task_root / "workspace.json").write_text(
        json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return descriptor


def main() -> int:
    args = parse_args()
    workspace_base = args.workspace_base.resolve()
    manifest_path = args.manifest.resolve()
    if not manifest_path.is_file():
        raise SystemExit(f"Benchmark manifest not found: {manifest_path}")
    selected = list(TASKS) if args.task == "all" else [args.task]
    results = [
        initialize_task(workspace_base, manifest_path, task, args.reset)
        for task in selected
    ]
    print(json.dumps({"ok": True, "version": VERSION, "items": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
