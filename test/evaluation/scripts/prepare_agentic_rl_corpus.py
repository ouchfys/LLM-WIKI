"""Download and validate the fixed Agentic RL evaluation corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = (
    REPO_ROOT
    / "test"
    / "evaluation"
    / "datasets"
    / "agent_benchmark_v1"
    / "agentic_rl_corpus_v1.json"
)
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "test"
    / "evaluation"
    / "workspaces"
    / "paperwiki_agent_eval_v1"
    / "agentic_rl"
    / "sources"
    / "papers"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_pdf(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    with path.open("rb") as source:
        return source.read(5) == b"%PDF-"


def _download(url: str, destination: Path, retries: int) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PaperWiki-Agent-Eval/1.0 (research corpus)"},
    )
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        partial = destination.with_suffix(".pdf.part")
        try:
            with urllib.request.urlopen(request, timeout=90) as response, partial.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            if not _valid_pdf(partial):
                raise RuntimeError("downloaded content is not a valid PDF")
            partial.replace(destination)
            return
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            last_error = exc
            partial.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(min(5 * attempt, 20))
    raise RuntimeError(f"failed after {retries} attempts: {last_error}")


def main() -> int:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    papers = manifest.get("papers") or []
    if len(papers) != int(manifest.get("paper_count") or 0):
        raise SystemExit("manifest paper_count does not match papers")
    ids = [str(paper["arxiv_id"]) for paper in papers]
    if len(ids) != len(set(ids)):
        raise SystemExit("manifest contains duplicate arXiv IDs")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, paper in enumerate(papers, start=1):
        arxiv_id = str(paper["arxiv_id"])
        pdf_path = output_dir / f"{arxiv_id}.pdf"
        if not _valid_pdf(pdf_path):
            print(f"[{index}/{len(papers)}] downloading {arxiv_id}", flush=True)
            _download(str(paper["pdf_url"]), pdf_path, max(1, args.retries))
            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
        else:
            print(f"[{index}/{len(papers)}] reusing {arxiv_id}", flush=True)
        metadata = {
            **paper,
            "snapshot_version": manifest["version"],
            "snapshot_date": manifest["snapshot_date"],
            "filename": pdf_path.name,
            "size_bytes": pdf_path.stat().st_size,
            "sha256": _sha256(pdf_path),
        }
        pdf_path.with_suffix(".json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        rows.append(metadata)

    lock = {
        "version": manifest["version"],
        "snapshot_date": manifest["snapshot_date"],
        "paper_count": len(rows),
        "papers": rows,
    }
    (output_dir / "corpus.lock.json").write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "paper_count": len(rows), "output_dir": str(output_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
