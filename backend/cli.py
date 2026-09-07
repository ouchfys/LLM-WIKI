"""One-command local launch; install this checkout with pip install -e ."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def ensure_frontend(root: Path, rebuild: bool = False) -> None:
    frontend = root / "frontend"
    if not (frontend / "package.json").is_file():
        raise RuntimeError("Project checkout not found. Install from the project folder with: python -m pip install -e .")
    index = frontend / "dist" / "index.html"
    if not rebuild and index.is_file():
        inputs = [frontend / name for name in ("package.json", "package-lock.json", "index.html", "vite.config.ts", "tsconfig.json")]
        for folder in ("src", "public"):
            inputs.append(frontend / folder)
            inputs.extend((frontend / folder).rglob("*"))
        if all(path.stat().st_mtime_ns <= index.stat().st_mtime_ns for path in inputs if path.exists()):
            return
        print("Frontend source changed; rebuilding...", flush=True)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise RuntimeError("Node.js/npm is required to build the frontend once. Install Node.js, then retry paperwiki web.")
    if not (frontend / "node_modules" / ".bin" / ("vite.cmd" if os.name == "nt" else "vite")).is_file():
        print("Installing frontend dependencies...", flush=True)
        subprocess.run([npm, "ci"], cwd=frontend, check=True)
    print("Building frontend...", flush=True)
    subprocess.run([npm, "run", "build"], cwd=frontend, check=True)
    if not (frontend / "dist" / "index.html").is_file():
        raise RuntimeError("Frontend build did not produce dist/index.html.")


def serve(port: int, open_browser: bool, rebuild: bool) -> int:
    # Reserve the port first, so an occupied port fails before any initialization.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"Cannot listen on 127.0.0.1:{port}. The port may be in use. "
                f"Try: paperwiki web --port {port + 1 if port < 65535 else 8000}"
            ) from exc
        ensure_frontend(PROJECT_ROOT, rebuild)
        # Keep relative paths anchored here even when launched from another folder.
        os.chdir(PROJECT_ROOT)
        import uvicorn

        url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config("backend.app:app", host="127.0.0.1", port=port))
        stopped = threading.Event()

        def on_ready() -> None:
            while not stopped.wait(0.1):
                if server.started:
                    print(f"\nPaperWiki: {url}\nPress Ctrl+C to stop.\n", flush=True)
                    if open_browser:
                        try:
                            webbrowser.open(url)
                        except Exception as exc:
                            print(f"Could not open browser: {exc}. Open {url} manually.", file=sys.stderr)
                    return

        ready_thread = threading.Thread(target=on_ready, name="paperwiki-browser", daemon=True)
        ready_thread.start()
        try:
            server.run(sockets=[listener])
        finally:
            stopped.set()
            ready_thread.join(timeout=1)
        return 0 if server.started else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paperwiki", description="Start the local PaperWiki project.")
    commands = parser.add_subparsers(dest="command", required=True)
    web = commands.add_parser("web", help="Start API and frontend, then open the browser")
    web.add_argument("--port", type=int, default=8000, help="Local port (default: 8000)")
    web.add_argument("--no-browser", action="store_true", help="Do not open the browser")
    web.add_argument("--build", action="store_true", help="Rebuild frontend after changing its source")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        return serve(args.port, not args.no_browser, args.build)
    except KeyboardInterrupt:
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError, ImportError) as exc:
        print(f"PaperWiki: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
