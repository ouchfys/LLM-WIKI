import os
import time
from threading import Timer

import pytest

from system.agent_runtime.local_shell import LocalShell
from system.agent_runtime.control import RunControl, RunCancelled
from system.storage.layout import StorageLayout


@pytest.mark.parametrize("session_id,run_id,folder", [
    ("chat-one", "run-one", "chat-one"),
    ("", "run-one", "run-one"),
    ("", "", "default"),
])
def test_default_shell_creates_only_the_active_scratch_directory_on_execution(
    tmp_path, monkeypatch, session_id, run_id, folder,
):
    layout = StorageLayout(tmp_path)
    monkeypatch.setattr("system.agent_runtime.local_shell.get_storage_layout", lambda: layout)
    shell = LocalShell()
    assert not (tmp_path / "tmp").exists()
    control = RunControl(None, run_id, "")
    control.session_id = session_id
    command = "Set-Content -LiteralPath note.txt -Value 'temporary'" if os.name == "nt" else "printf temporary > note.txt"
    with control.bind():
        assert shell.cwd == (tmp_path / "tmp" / folder).resolve()
        assert not (tmp_path / "tmp").exists()
        result = shell.run(command, shell="powershell" if os.name == "nt" else "bash")
    assert result["exit_code"] == 0
    assert result["cwd"] == str((tmp_path / "tmp" / folder).resolve())
    assert (tmp_path / "tmp" / folder / "note.txt").read_text(encoding="utf-8-sig").strip() == "temporary"
    assert not (tmp_path / "note.txt").exists()
    assert not (tmp_path / "projects").exists()


def test_explicit_shell_cwd_does_not_create_default_scratch(tmp_path, monkeypatch):
    layout = StorageLayout(tmp_path / "data")
    monkeypatch.setattr("system.agent_runtime.local_shell.get_storage_layout", lambda: layout)
    selected = tmp_path / "selected"
    selected.mkdir()
    result = LocalShell().run(
        "Write-Output 'ok'" if os.name == "nt" else "printf ok",
        cwd=selected, shell="powershell" if os.name == "nt" else "bash",
    )
    assert result["exit_code"] == 0
    assert result["cwd"] == str(selected.resolve())
    assert not (layout.data_root / "tmp").exists()


@pytest.mark.parametrize("requested_cwd", ["absolute-default", "."])
def test_first_shell_call_can_explicitly_select_its_default_directory(tmp_path, monkeypatch, requested_cwd):
    layout = StorageLayout(tmp_path)
    monkeypatch.setattr("system.agent_runtime.local_shell.get_storage_layout", lambda: layout)
    shell = LocalShell()
    control = RunControl(None, "run-one", "")
    control.session_id = "chat-one"
    with control.bind():
        expected = shell.cwd
        assert not expected.exists()
        result = shell.run(
            "Write-Output 'ok'" if os.name == "nt" else "printf ok",
            cwd=expected if requested_cwd == "absolute-default" else requested_cwd,
            shell="powershell" if os.name == "nt" else "bash",
        )
    assert result["exit_code"] == 0
    assert result["cwd"] == str(expected)
    assert expected.is_dir()


def test_default_shell_does_not_create_an_explicit_different_directory(tmp_path, monkeypatch):
    layout = StorageLayout(tmp_path)
    monkeypatch.setattr("system.agent_runtime.local_shell.get_storage_layout", lambda: layout)
    with pytest.raises(ValueError, match="working directory does not exist"):
        LocalShell().run("echo ignored", cwd="missing")
    assert not (tmp_path / "tmp").exists()


def test_shared_shell_resolves_each_conversation_at_use_time(tmp_path, monkeypatch):
    layout = StorageLayout(tmp_path)
    monkeypatch.setattr("system.agent_runtime.local_shell.get_storage_layout", lambda: layout)
    shell = LocalShell()
    for session_id in ("first-session", "second-session"):
        control = RunControl(None, "same-run", "")
        control.session_id = session_id
        with control.bind():
            assert shell.cwd == layout.scratch_dir(session_id)
    assert not (tmp_path / "tmp").exists()


def test_local_shell_runs_with_project_process_permissions(tmp_path):
    shell = LocalShell(tmp_path)
    if os.name == "nt":
        result = shell.run("Write-Output 'hello'; Write-Output (Get-Location).Path")
    else:
        result = shell.run("printf 'hello\\n%s\\n' \"$PWD\"", shell="bash")

    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert "hello" in result["stdout"]
    assert str(tmp_path.resolve()) in result["stdout"]


def test_local_shell_accepts_project_relative_working_directory(tmp_path):
    nested = tmp_path / "artifacts"
    nested.mkdir()
    shell = LocalShell(tmp_path)
    command = "Write-Output (Get-Location).Path" if os.name == "nt" else "pwd"
    requested_shell = "powershell" if os.name == "nt" else "bash"

    result = shell.run(command, cwd="artifacts", shell=requested_shell)

    assert result["exit_code"] == 0
    assert str(nested.resolve()) in result["stdout"]


def test_local_shell_rejects_missing_working_directory(tmp_path):
    shell = LocalShell(tmp_path)

    with pytest.raises(ValueError, match="working directory does not exist"):
        shell.run("echo ignored", cwd="missing")


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell encoding regression")
def test_shell_reads_utf8_markdown_without_explicit_encoding(tmp_path):
    content = "# 项目记忆\n已完成论文阅读，下次继续比较。"
    (tmp_path / "MEMORY.md").write_text(content, encoding="utf-8")
    result = LocalShell(tmp_path).run("Get-Content -Raw MEMORY.md")
    assert result["exit_code"] == 0
    assert content in result["stdout"].replace("\r\n", "\n")


def test_cancel_stops_shell_promptly_and_preserves_partial_output(tmp_path):
    control = RunControl(None, "", "")
    timer = Timer(1.5, control.abandoned.set)
    command = "Write-Output 'before-cancel'; Start-Sleep -Seconds 20; Write-Output 'after-cancel'" if os.name == "nt" else "printf 'before-cancel\\n'; sleep 20; printf 'after-cancel\\n'"
    started = time.monotonic()
    timer.start()
    try:
        with control.bind(), pytest.raises(RunCancelled) as exc:
            LocalShell(tmp_path).run(command, shell="powershell" if os.name == "nt" else "bash", timeout_seconds=30)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 6
    assert exc.value.tool_result["cancelled"] is True
    assert "before-cancel" in exc.value.tool_result["stdout"]
    assert "after-cancel" not in exc.value.tool_result["stdout"]


def test_timeout_preserves_partial_output(tmp_path):
    command = "Write-Output 'partial'; Start-Sleep -Seconds 20" if os.name == "nt" else "printf 'partial\\n'; sleep 20"
    result = LocalShell(tmp_path).run(command, shell="powershell" if os.name == "nt" else "bash", timeout_seconds=2)
    assert result["timed_out"] is True
    assert "partial" in result["stdout"]


def test_cancelled_shell_output_is_saved_by_chat_tool():
    from system.wiki.wiki_chat import AgentToolCall, WikiChatService
    class InterruptedShell:
        def run(self, *args, **kwargs):
            error = RunCancelled("cancelled")
            error.tool_result = {"stdout": "one result already returned", "stderr": "", "cancelled": True}
            raise error
    class Store:
        saved = []
        def save_tool_result(self, *args):
            self.saved.append(args)
            return 1
    store = Store()
    chat = WikiChatService(object(), wiki_resolver=object(), local_shell=InterruptedShell())
    chat.session_store = store
    control = RunControl(None, "", "")
    control.session_id = "test-session"
    with control.bind(), pytest.raises(RunCancelled):
        chat._execute_agent_tool_call_impl(AgentToolCall("local_shell", {"command": "read"}), [], [], [], 6)
    assert store.saved[0][0] == "test-session"
    assert store.saved[0][3]["items"][0]["stdout"] == "one result already returned"
