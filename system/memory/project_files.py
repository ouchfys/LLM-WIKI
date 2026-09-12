"""Prompt-facing Markdown project notes with bounded, atomic file updates.

``purpose.md`` and ``MEMORY.md`` are the model-facing source of truth.  The
older SQLite memory tables and topic projections remain readable for existing
installations, but new chat turns maintain the compact ``MEMORY.md`` directly.
Wiki knowledge never shares this namespace.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Mapping


SCHEMA_VERSION = "paperwiki-memory/v1"

TOPIC_SPECS: Dict[str, Dict[str, Any]] = {
    "goals": {
        "title": "Goals",
        "memory_types": ("goal",),
        "description": "跨会话持续有效的项目目标。",
    },
    "constraints": {
        "title": "Constraints",
        "memory_types": ("constraint",),
        "description": "研究范围、资源边界和必须遵守的约束。",
    },
    "decisions": {
        "title": "Decisions",
        "memory_types": ("decision",),
        "description": "已经确认或明确否决的项目级决定。",
    },
    "open-questions": {
        "title": "Open Questions",
        "memory_types": ("open_question",),
        "description": "仍需回答、验证或继续推进的问题。",
    },
    "milestones": {
        "title": "Milestones",
        "memory_types": ("milestone",),
        "description": "已经完成并可能影响后续工作的阶段结果。",
    },
    "research-direction": {
        "title": "Research Direction",
        "memory_types": ("topic", "research_direction"),
        "description": "当前研究主题、关注对象和方向变化。",
    },
    "failed-attempts": {
        "title": "Failed Attempts",
        "memory_types": ("failed_attempt",),
        "description": "已经验证无效的做法、失败原因和避免重复尝试的经验。",
    },
}

MEMORY_TYPE_TO_TOPIC = {
    memory_type: topic
    for topic, spec in TOPIC_SPECS.items()
    for memory_type in spec["memory_types"]
}


class ProjectMemoryFiles:
    """Read and atomically maintain project-scoped Markdown notes."""

    MAX_PURPOSE_CHARS = 12_000
    MAX_MEMORY_CHARS = 12_000
    MAX_INDEX_CHARS = MAX_MEMORY_CHARS  # compatibility name for old callers
    MAX_TOPIC_CHARS = 64_000
    MANUAL_NOTES_HEADING = "## Manual notes"

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.projects_root = self.root / "projects"
        self._lock = RLock()

    def project_dir(self, project_id: str) -> Path:
        safe_id = self._safe_segment(project_id)
        path = (self.projects_root / safe_id).resolve()
        if not path.is_relative_to(self.projects_root.resolve()):
            raise ValueError("Project memory path escapes the configured root")
        return path

    def ensure_project(self, project_id: str, project_name: str, purpose: str = "") -> None:
        directory = self.project_dir(project_id)
        (directory / "topics").mkdir(parents=True, exist_ok=True)
        purpose_path = directory / "purpose.md"
        if not purpose_path.exists():
            self.write_purpose(project_id, project_name, purpose, updated_at="")
        index_path = directory / "MEMORY.md"
        if not index_path.exists():
            self._atomic_write(index_path, self._default_memory(project_name))

    def write_memory(
        self,
        project_id: str,
        project_name: str,
        content: str,
    ) -> Path:
        """Replace the current project's model-managed notes.

        The model owns the prose and sections.  Python owns only path isolation,
        size limits and atomic replacement.
        """
        directory = self.project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        text = str(content or "").replace("\x00", "").strip()
        if not text:
            text = self._default_memory(project_name)
        elif not re.search(r"(?m)^#\s+Project Memory\s*$", text):
            text = "# Project Memory\n\n" + text
        if len(text) > self.MAX_MEMORY_CHARS:
            raise ValueError(
                f"MEMORY.md exceeds {self.MAX_MEMORY_CHARS} characters; compact it before writing"
            )
        text = text.rstrip() + "\n"
        path = directory / "MEMORY.md"
        with self._lock:
            self._atomic_write(path, text)
        return path

    def read_memory(self, project_id: str) -> str:
        return self._read_bounded(
            self.project_dir(project_id) / "MEMORY.md",
            self.MAX_MEMORY_CHARS,
        )

    def write_purpose(
        self,
        project_id: str,
        project_name: str,
        purpose: str,
        *,
        source_session_id: str = "",
        evidence: str = "",
        updated_at: str = "",
    ) -> Path:
        directory = self.project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        body = str(purpose or "").strip()[: self.MAX_PURPOSE_CHARS]
        if not body:
            body = "尚未设置。可在对话中使用 `/purpose <目标>` 更新。"
        text = (
            "---\n"
            f"schema: {SCHEMA_VERSION}\n"
            f"project_id: {self._yaml_scalar(project_id)}\n"
            f"updated_at: {self._yaml_scalar(updated_at)}\n"
            "---\n\n"
            f"# Purpose\n\n{body}\n\n"
            "## Provenance\n\n"
            f"- source_session_id: {source_session_id or 'none'}\n"
            f"- evidence: {self._single_line(evidence) or 'none'}\n"
        )
        path = directory / "purpose.md"
        self._atomic_write(path, text)
        return path

    def read_purpose(self, project_id: str) -> str:
        path = self.project_dir(project_id) / "purpose.md"
        text = self._read_bounded(path, self.MAX_PURPOSE_CHARS + 4096)
        if not text:
            return ""
        match = re.search(r"(?ms)^# Purpose\s*\n+(.*?)(?=^## Provenance\s*$|\Z)", text)
        value = (match.group(1) if match else "").strip()
        if value.startswith("尚未设置。"):
            return ""
        return value[: self.MAX_PURPOSE_CHARS]

    def sync_project(
        self,
        project_id: str,
        project_name: str,
        memories: Iterable[Mapping[str, Any]],
        *,
        updated_at: str,
    ) -> None:
        with self._lock:
            self.ensure_project(project_id, project_name)
            records = [dict(item) for item in memories]
            directory = self.project_dir(project_id)
            for topic, spec in TOPIC_SPECS.items():
                topic_records = [
                    item for item in records
                    if str(item.get("memory_type") or "") in spec["memory_types"]
                ]
                path = directory / "topics" / f"{topic}.md"
                manual_notes = self._manual_notes(path)
                self._atomic_write(
                    path,
                    self._render_topic(project_id, topic, spec, topic_records, updated_at, manual_notes),
                )
            # Migrate the old generated index once.  Never overwrite a file that
            # has already become model-managed notes.
            memory_path = directory / "MEMORY.md"
            current = self._read_bounded(memory_path, self.MAX_MEMORY_CHARS)
            if not current or self._is_legacy_index(current):
                self._atomic_write(
                    memory_path,
                    self._render_memory_from_records(project_name, records),
                )

    def sync_preferences(self, preferences: Iterable[Mapping[str, Any]], *, updated_at: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        lines = [
            "---",
            f"schema: {SCHEMA_VERSION}",
            "scope: user",
            f"updated_at: {self._yaml_scalar(updated_at)}",
            "---",
            "",
            "# Preferences",
            "",
            "> 只保存用户明确表达的持续偏好；论文事实和项目决定不属于这里。",
            "",
        ]
        rows = list(preferences)
        if not rows:
            lines.append("暂无稳定偏好。")
        for item in rows:
            key = self._single_line(item.get("key"))
            lines.extend([
                f"## preference:{key}",
                "",
                f"- value: {self._single_line(item.get('value'))}",
                f"- source_session_id: {self._single_line(item.get('source_session_id')) or 'none'}",
                f"- evidence_message_id: {int(item.get('evidence_message_id') or 0)}",
                f"- confidence: {float(item.get('confidence') or 0.0):.2f}",
                f"- updated_at: {self._single_line(item.get('updated_at')) or 'unknown'}",
                "",
            ])
        path = self.root / "preferences.md"
        self._atomic_write(path, "\n".join(lines).rstrip() + "\n")
        return path

    def read_index(self, project_id: str) -> str:
        return self.read_memory(project_id)

    def open_topic(self, project_id: str, topic: str) -> Dict[str, str]:
        topic = str(topic or "").strip().lower().removesuffix(".md")
        if topic.startswith("topics/"):
            topic = topic.split("/", 1)[1]
        if topic not in TOPIC_SPECS:
            raise ValueError(f"Unknown project-memory topic: {topic}")
        path = self.project_dir(project_id) / "topics" / f"{topic}.md"
        content = self._read_bounded(path, self.MAX_TOPIC_CHARS)
        return {
            "topic": topic,
            "path": f"topics/{topic}.md",
            "content": content,
        }

    def prompt_index(self, project_id: str) -> str:
        purpose = self._read_bounded(
            self.project_dir(project_id) / "purpose.md",
            self.MAX_PURPOSE_CHARS + 4096,
        )
        memory = self.read_memory(project_id)
        parts = []
        if purpose:
            parts.append("[PROJECT_PURPOSE_FILE]\n" + purpose)
        if memory:
            parts.append("[PROJECT_MEMORY_FILE]\n" + memory)
        return "\n\n".join(parts)

    @staticmethod
    def _default_memory(project_name: str) -> str:
        return (
            "# Project Memory\n\n"
            f"> {project_name} 的跨会话工作状态。由 Agent 在任务过程中直接维护。\n\n"
            "## Current Goal\n\n暂无记录。\n\n"
            "## Constraints\n\n暂无记录。\n\n"
            "## Decisions\n\n暂无记录。\n\n"
            "## Progress\n\n暂无记录。\n\n"
            "## Failed Attempts\n\n暂无记录。\n\n"
            "## Open Questions\n\n暂无记录。\n\n"
            "## Next Steps\n\n暂无记录。\n\n"
            "## Boundary\n\n"
            "- 这里只保存跨会话继续项目所需的目标、约束、决定、进度和经验。\n"
            "- 论文事实、Claim、Evidence 和综述属于 Wiki 知识库。\n"
            "- 原始聊天与 compact 摘要属于 SQLite 会话记录。\n"
        )

    @staticmethod
    def _is_legacy_index(content: str) -> bool:
        return "跨会话记忆索引" in content or "paperwiki-memory/v1" in content

    def _render_memory_from_records(
        self,
        project_name: str,
        records: List[Dict[str, Any]],
    ) -> str:
        active = [item for item in records if str(item.get("status") or "active") == "active"]
        sections = [
            ("Current Goal", {"goal"}),
            ("Constraints", {"constraint"}),
            ("Decisions", {"decision"}),
            ("Progress", {"milestone", "topic", "research_direction"}),
            ("Failed Attempts", {"failed_attempt"}),
            ("Open Questions", {"open_question"}),
            ("Next Steps", set()),
        ]
        lines = [
            "# Project Memory", "",
            f"> {project_name} 的跨会话工作状态。由 Agent 在任务过程中直接维护。", "",
        ]
        for title, memory_types in sections:
            lines.extend([f"## {title}", ""])
            values = [
                self._single_line(item.get("content"))
                for item in active
                if str(item.get("memory_type") or "") in memory_types
                and self._single_line(item.get("content"))
            ]
            lines.extend([f"- {value}" for value in values] or ["暂无记录。"])
            lines.append("")
        lines.extend([
            "## Boundary", "",
            "- 这里只保存跨会话继续项目所需的目标、约束、决定、进度和经验。",
            "- 论文事实、Claim、Evidence 和综述属于 Wiki 知识库。",
            "- 原始聊天与 compact 摘要属于 SQLite 会话记录。", "",
        ])
        return "\n".join(lines)[: self.MAX_MEMORY_CHARS].rstrip() + "\n"

    def _render_index(
        self,
        project_id: str,
        project_name: str,
        records: List[Dict[str, Any]],
        updated_at: str,
    ) -> str:
        lines = [
            "---",
            f"schema: {SCHEMA_VERSION}",
            f"project_id: {self._yaml_scalar(project_id)}",
            f"updated_at: {self._yaml_scalar(updated_at)}",
            "---",
            "",
            "# Project Memory",
            "",
            f"> {project_name} 的跨会话记忆索引。索引固定加载，主题文件按需打开。",
            "",
        ]
        active = [item for item in records if str(item.get("status") or "active") == "active"]
        for topic, spec in TOPIC_SPECS.items():
            matches = [
                item for item in active
                if str(item.get("memory_type") or "") in spec["memory_types"]
            ]
            latest = sorted(matches, key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            summary = self._single_line(latest[0].get("content"))[:180] if latest else "暂无记录"
            lines.append(
                f"- [{spec['title']}](topics/{topic}.md) — {len(matches)} 条 active；{summary}"
            )
        lines.extend([
            "",
            "## Boundary",
            "",
            "- 这里保存项目目标、约束、决定、开放问题和经验。",
            "- 论文事实、Claim、Evidence 和综述属于 Wiki 知识库。",
            "- 原始聊天与 compact 摘要属于 SQLite 会话记录。",
        ])
        return "\n".join(lines).rstrip()[: self.MAX_INDEX_CHARS] + "\n"

    def _render_topic(
        self,
        project_id: str,
        topic: str,
        spec: Mapping[str, Any],
        records: List[Dict[str, Any]],
        updated_at: str,
        manual_notes: str,
    ) -> str:
        lines = [
            "---",
            f"schema: {SCHEMA_VERSION}",
            f"project_id: {self._yaml_scalar(project_id)}",
            f"topic: {topic}",
            f"updated_at: {self._yaml_scalar(updated_at)}",
            "---",
            "",
            f"# {spec['title']}",
            "",
            f"> {spec['description']}",
            "",
            "## Managed records",
            "",
        ]
        ordered = sorted(
            records,
            key=lambda item: (
                str(item.get("status") or "active") == "active",
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        if not ordered:
            lines.append("暂无记录。")
        for item in ordered:
            memory_id = int(item.get("id") or 0)
            evidence_ids = item.get("evidence_message_ids") or []
            if not isinstance(evidence_ids, list):
                evidence_ids = []
            lines.extend([
                f"### memory:{memory_id}",
                "",
                f"- status: {self._single_line(item.get('status')) or 'active'}",
                f"- content: {self._single_line(item.get('content'))}",
                f"- source_session_id: {self._single_line(item.get('source_session_id')) or 'none'}",
                f"- evidence_message_ids: {', '.join(str(int(value)) for value in evidence_ids if str(value).isdigit()) or 'none'}",
                f"- confidence: {float(item.get('confidence') or 0.0):.2f}",
                f"- importance: {float(item.get('importance') or 0.0):.2f}",
                f"- updated_at: {self._single_line(item.get('updated_at')) or 'unknown'}",
                f"- supersedes: {('memory:' + str(int(item['supersedes_id']))) if item.get('supersedes_id') else 'none'}",
                f"- expires_at: {self._single_line(item.get('expires_at')) or 'none'}",
                "",
            ])
        lines.extend([self.MANUAL_NOTES_HEADING, "", manual_notes.strip() or "可在这里补充人工备注；自动同步不会覆盖本节。", ""])
        text = "\n".join(lines)
        return text[: self.MAX_TOPIC_CHARS].rstrip() + "\n"

    def _manual_notes(self, path: Path) -> str:
        current = self._read_bounded(path, self.MAX_TOPIC_CHARS)
        if self.MANUAL_NOTES_HEADING not in current:
            return ""
        return current.split(self.MANUAL_NOTES_HEADING, 1)[1].strip()

    @staticmethod
    def _safe_segment(value: str) -> str:
        raw = str(value or "").strip()
        if not raw or raw in {".", ".."}:
            raise ValueError("Project id is required")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")[:96]
        if not safe:
            raise ValueError("Project id has no safe path characters")
        return safe

    @staticmethod
    def _single_line(value: Any) -> str:
        return " ".join(str(value or "").replace("\x00", "").split())

    @classmethod
    def _yaml_scalar(cls, value: Any) -> str:
        return cls._single_line(value).replace(":", "-") or "none"

    @staticmethod
    def _read_bounded(path: Path, limit: int) -> str:
        try:
            return path.read_text(encoding="utf-8")[: max(0, int(limit))]
        except (FileNotFoundError, OSError, UnicodeError):
            return ""

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(str(content or ""), encoding="utf-8", newline="\n")
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
