"""Run one real PaperWiki chat turn and save its public SSE trace; never retry a POST.

Example:
    python scripts/run_research_smoke.py --prompt-file prompt.txt --output-dir runs/rsi-01

A successful transport/turn does not establish that the research goal was met.
Inspect summary.json's stop_reason, answer.md, and events.jsonl separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import quote, urlsplit

import requests


class StreamProtocolError(ValueError):
    pass


def iter_sse_events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Decode JSON data fields, including multiline SSE and comment heartbeats."""
    payload: list[str] = []
    for line in lines:
        line = line.rstrip("\r")
        if not line:
            if not payload:
                continue
            try:
                event = json.loads("\n".join(payload))
            except json.JSONDecodeError as exc:
                raise StreamProtocolError("Invalid JSON SSE event") from exc
            payload = []
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise StreamProtocolError("SSE event is missing its type")
            yield event
        elif line.startswith("data:"):
            value = line[5:]
            payload.append(value[1:] if value.startswith(" ") else value)
    if payload:
        raise StreamProtocolError("Connection ended inside an SSE event")


def public_event(value: Any) -> Any:
    """Never persist provider-private reasoning or structured credential fields."""
    hidden = {"reasoning_content", "reasoning", "thinking", "api_key", "apikey", "authorization", "access_token", "password", "secret"}
    if isinstance(value, dict):
        return {key: public_event(item) for key, item in value.items() if key.lower() not in hidden}
    if isinstance(value, list):
        return [public_event(item) for item in value]
    return value


def _save_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_research_smoke(
    *, base_url: str, prompt_file: Path, output_dir: Path,
    http: Any = None, report: Callable[[str], None] = print,
    read_timeout: float = 120,
) -> int:
    base_url = base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base-url must be an HTTP(S) URL without credentials, query, or fragment")
    prompt = prompt_file.read_text(encoding="utf-8-sig")
    if not prompt.strip():
        raise ValueError("Prompt file is empty")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("Output directory must be empty; existing runs are never overwritten or resubmitted")
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    # Reserve this run before any network request, including session creation.
    with summary_path.open("x", encoding="utf-8") as reserved:
        reserved.write("{}\n")
    (output_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    api_root = base_url if base_url.endswith("/api") else base_url + "/api"
    app_root = base_url[:-4] if base_url.endswith("/api") else base_url
    started = time.monotonic()
    summary: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "session_id": None, "run_id": None, "session_url": None,
        "stream_done": False, "turn_complete": False, "cancelled": False,
        "stop_reason": None, "research_goal_verified": None,
        "research_goal_note": "Not independently verified; a done event only closes this turn.",
        "transport_status": "starting", "event_count": 0, "event_types": {},
        "errors": [], "answer_status": "partial", "exit_code": None,
    }
    _save_json(summary_path, summary)
    client = http if http is not None else requests.Session()
    exit_code = 2
    try:
        with (output_dir / "events.jsonl").open("x", encoding="utf-8") as journal, (output_dir / "answer.md").open("x", encoding="utf-8") as answer:
            response = client.post(api_root + "/wiki/sessions", json={"title": "Research smoke: " + prompt_file.stem[:50]}, timeout=(10, 30))
            try:
                response.raise_for_status()
                session = response.json()
            finally:
                response.close()
            session_id = session.get("id") if isinstance(session, dict) else None
            if not isinstance(session_id, str) or not session_id:
                raise StreamProtocolError("Session creation returned no session ID")
            summary.update(session_id=session_id, session_url=app_root + "/?session=" + quote(session_id, safe=""), transport_status="streaming")
            _save_json(summary_path, summary)
            report(f"Session {session_id}; saving events to {output_dir}")
            # Exactly one chat POST. A lost connection never triggers a new run.
            with client.post(
                api_root + "/wiki/chat", json={"message": prompt, "session_id": session_id, "stream": True},
                headers={"Accept": "text/event-stream"}, stream=True, timeout=(10, read_timeout),
            ) as stream:
                stream.raise_for_status()
                if "text/event-stream" not in stream.headers.get("Content-Type", "").lower():
                    raise StreamProtocolError("Chat response is not text/event-stream")
                stream.encoding = "utf-8"
                last_checkpoint = time.monotonic()
                for raw in iter_sse_events(stream.iter_lines(decode_unicode=True)):
                    event = public_event(raw)
                    kind = event["type"]
                    journal.write(json.dumps(event, ensure_ascii=False) + "\n")
                    journal.flush()
                    summary["event_count"] += 1
                    summary["event_types"][kind] = summary["event_types"].get(kind, 0) + 1
                    if kind == "run_started":
                        summary["run_id"] = event.get("run_id")
                        report(f"Run {summary['run_id']}")
                    elif kind == "token":
                        answer.write(event.get("text") or "")
                        answer.flush()
                    elif kind == "answer_reset":
                        answer.seek(0)
                        answer.truncate()
                        answer.flush()
                    elif kind == "agent_trace":
                        trace = event.get("trace") or {}
                        summary["stop_reason"] = trace.get("stop_reason")
                        summary["runtime"] = trace.get("runtime") or {}
                        _save_json(output_dir / "trace.json", trace)
                    elif kind == "error":
                        summary["errors"].append({"type": "server_error", "event_number": summary["event_count"]})
                        report("Server reported an error; see the saved public event.")
                    elif kind == "cancelled":
                        summary["cancelled"] = True
                    elif kind == "tool_status":
                        report(f"{time.monotonic() - started:.0f}s tool {event.get('tool', '?')}: {event.get('status', '?')}")
                    elif kind == "progress":
                        report(f"{time.monotonic() - started:.0f}s progress received")
                    elif kind == "done":
                        summary["stream_done"] = True
                        summary["cancelled"] = summary["cancelled"] or bool(event.get("cancelled"))
                        break
                    if kind != "token" or time.monotonic() - last_checkpoint >= 1:
                        _save_json(summary_path, summary)
                        last_checkpoint = time.monotonic()
                if not summary["stream_done"]:
                    raise StreamProtocolError("Connection ended before the done event")
            summary["transport_status"] = "completed"
            summary["turn_complete"] = not summary["cancelled"] and not summary["errors"]
            if summary["turn_complete"]:
                summary["answer_status"] = "final_turn_answer"
                exit_code = 0
            elif summary["cancelled"]:
                exit_code = 3
    except KeyboardInterrupt:
        summary["transport_status"] = "client_interrupted"
        summary["errors"].append({"type": "KeyboardInterrupt"})
        exit_code = 130
    except (requests.RequestException, StreamProtocolError, ValueError) as exc:
        summary["transport_status"] = "http_error" if isinstance(exc, requests.HTTPError) else "interrupted"
        summary["errors"].append({"type": type(exc).__name__})
        # Do not echo exception bodies, URLs with credentials, or model content.
        report(f"Stopped: {type(exc).__name__}; no request will be retried.")
    finally:
        if http is None:
            client.close()
        summary.update(finished_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=round(time.monotonic() - started, 3), exit_code=exit_code)
        _save_json(summary_path, summary)
    report(f"Turn complete={summary['turn_complete']}; stop_reason={summary['stop_reason'] or 'not reported'}; research goal not independently verified.")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return run_research_smoke(**vars(args))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
