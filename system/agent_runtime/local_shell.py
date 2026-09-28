"""A small, full-access local shell for the single-user PaperWiki agent."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from system.agent_runtime.control import check_run_control, get_run_control, RunCancelled, RunInterrupted
from system.storage.layout import get_storage_layout


class LocalShell:
    """Run PowerShell or Bash commands with a bounded wait and captured output.

    PaperWiki is a local, single-user application.  The shell intentionally has
    the same filesystem permissions as the server process; the runtime still
    bounds execution time and persists tool output for recovery and audit.
    """

    def __init__(self, cwd: str | Path | None = None, max_timeout_seconds: int = 300):
        self._cwd = Path(cwd).expanduser().resolve() if cwd else None
        self.max_timeout_seconds = max(1, int(max_timeout_seconds))

    @property
    def cwd(self) -> Path:
        """Resolve the current session's scratch path without creating it."""
        if self._cwd is not None:
            return self._cwd
        control = get_run_control()
        identity = (
            getattr(control, "session_id", "") or getattr(control, "run_id", "")
            if control else ""
        )
        return get_storage_layout().scratch_dir(session_id=identity).expanduser().resolve()

    def run(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        shell: str = "powershell",
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        text = str(command or "").strip()
        if not text:
            raise ValueError("command is required")
        timeout = max(1, min(int(timeout_seconds or 120), self.max_timeout_seconds))
        argv = self._argv(text, shell)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        check_run_control()
        working_directory = self._working_directory(cwd)
        with subprocess.Popen(
                argv,
                cwd=str(working_directory),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
                start_new_session=os.name != "nt",
        ) as process:
            deadline = time.monotonic() + timeout
            stopped = None
            timed_out = False
            while True:
                try:
                    check_run_control()
                except (RunCancelled, RunInterrupted) as exc:
                    stopped = exc
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if stopped or timed_out:
                self._stop_process_tree(process)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    # A detached child may still hold a pipe open. Preserve
                    # captured bytes and never block cancellation on that pipe.
                    stdout, stderr = exc.stdout or b"", exc.stderr or b""
                    process.stdout.close()
                    process.stderr.close()
            result = {
                "exit_code": process.returncode,
                "stdout": self._decode(stdout), "stderr": self._decode(stderr),
                "cwd": str(working_directory), "shell": str(shell or "powershell").lower(),
                "timed_out": timed_out,
            }
            if timed_out:
                result["timeout_seconds"] = timeout
            if stopped:
                result["cancelled"] = isinstance(stopped, RunCancelled)
                result["interrupted"] = isinstance(stopped, RunInterrupted)
                stopped.tool_result = result
                raise stopped
            return result

    @staticmethod
    def _stop_process_tree(process: subprocess.Popen) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()

    def _working_directory(self, cwd: str | Path | None) -> Path:
        base = self.cwd
        candidate = Path(cwd).expanduser() if cwd else base
        if not candidate.is_absolute():
            candidate = base / candidate
        candidate = candidate.resolve()
        if self._cwd is None and candidate == base.resolve():
            candidate.mkdir(parents=True, exist_ok=True)
        if not candidate.is_dir():
            raise ValueError(f"working directory does not exist: {candidate}")
        return candidate

    @staticmethod
    def _argv(command: str, shell: str) -> list[str]:
        requested = str(shell or "powershell").strip().lower()
        if requested in {"powershell", "pwsh"}:
            executable = "powershell.exe" if os.name == "nt" else "pwsh"
            preamble = (
                "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
                "$OutputEncoding=[Console]::OutputEncoding;"
                "$PSDefaultParameterValues['Get-Content:Encoding']='utf8';"
                "$PSDefaultParameterValues['Set-Content:Encoding']='utf8';"
                "$PSDefaultParameterValues['Add-Content:Encoding']='utf8';"
            )
            return [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                preamble + command,
            ]
        if requested == "bash":
            return ["bash", "-lc", command]
        raise ValueError("shell must be powershell or bash")

    @staticmethod
    def _decode(payload: bytes | str) -> str:
        if isinstance(payload, str):
            return payload
        for encoding in ("utf-8", "gb18030"):
            try:
                return payload.decode(encoding)
            except UnicodeDecodeError:
                continue
        return payload.decode("utf-8", errors="replace")
