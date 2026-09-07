from pathlib import Path
import socket

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend import cli
from backend.web_frontend import mount_frontend


def test_frontend_history_assets_and_api_boundaries(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>PaperWiki</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('wiki')", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("private", encoding="utf-8")
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    mount_frontend(app, dist)
    client = TestClient(app)
    for route in ("/", "/vault", "/reviews", "/capture", "/evaluation"):
        response = client.get(route)
        assert response.status_code == 200
        assert "PaperWiki" in response.text
    assert client.get("/assets/app.js").text == "console.log('wiki')"
    assert client.get("/api/health").json() == {"status": "ok"}
    for route in ("/api/missing", "/assets/missing.js", "/.env", "/%2e%2e/secret.txt"):
        assert client.get(route).status_code == 404
    assert client.post("/vault").status_code == 405
    assert client.get("/openapi.json").json()["info"]


def test_api_development_without_dist(tmp_path):
    app = FastAPI()
    mount_frontend(app, tmp_path / "missing")
    client = TestClient(app)
    assert client.get("/").status_code == 404
    assert client.get("/docs").status_code == 200


def test_existing_build_does_not_need_npm(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    (frontend / "dist").mkdir(parents=True)
    (frontend / "package.json").write_text("{}")
    (frontend / "dist" / "index.html").write_text("ready")
    monkeypatch.setattr(cli.shutil, "which", lambda _: pytest.fail("npm should not be needed"))
    cli.ensure_frontend(tmp_path)


def test_occupied_port_does_not_initialize_project(monkeypatch, capsys):
    monkeypatch.setattr(cli, "ensure_frontend", lambda *args: pytest.fail("port should be checked first"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert cli.main(["web", "--port", str(listener.getsockname()[1]), "--no-browser"]) == 1
    assert "--port" in capsys.readouterr().err


def test_launch_anchors_project_and_opens_only_when_ready(tmp_path, monkeypatch):
    import uvicorn

    root = tmp_path / "project"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(cli, "PROJECT_ROOT", root)
    monkeypatch.setattr(cli, "ensure_frontend", lambda *args: None)
    opened = cli.threading.Event()
    observations = []

    class Server:
        started = False

        def __init__(self, config):
            observations.append(Path.cwd())

        def run(self, sockets):
            assert not opened.is_set()
            assert sockets[0].getsockname()[0] == "127.0.0.1"
            self.started = True
            assert opened.wait(3)

    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.set())
    assert cli.serve(0, True, False) == 0
    assert observations == [root]


def test_failed_build_stops_launch(tmp_path, monkeypatch):
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "npm")

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(cli.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        cli.ensure_frontend(tmp_path)


def test_changed_frontend_is_rebuilt_before_launch(tmp_path, monkeypatch):
    import os

    frontend = tmp_path / "frontend"
    (frontend / "dist").mkdir(parents=True)
    (frontend / "src").mkdir()
    (frontend / "package.json").write_text("{}")
    (frontend / "dist" / "index.html").write_text("old build")
    os.utime(frontend / "dist" / "index.html", (1, 1))
    (frontend / "src" / "App.vue").write_text("new source")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "npm")
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    cli.ensure_frontend(tmp_path)
    assert [args[1:] for args, _ in calls] == [["ci"], ["run", "build"]]
    assert all(options["cwd"] == frontend and options["check"] for _, options in calls)
