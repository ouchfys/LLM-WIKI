"""Evaluate the production evidence verifier on the frozen silver set."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.deps import get_review_llm
from system.core.config import SILICONFLOW_REVIEW_MODEL
from system.wiki.evidence_verifier import EvidenceVerifier
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="sessions.db")
    parser.add_argument("--dataset", default="test/evaluation/datasets/evidence_wiki_silver_v1")
    parser.add_argument("--output-root", default="test/evaluation/runs/evidence_wiki_silver_v1")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.time()
    dataset_dir = Path(args.dataset)
    cases = _read_jsonl(dataset_dir / "verifier.jsonl")
    if args.limit:
        cases = cases[: args.limit]

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    verifier = EvidenceVerifier(PaperWikiPipelineStore(db_path=args.db), llm=get_review_llm())
    results = _parallel_run(
        cases,
        lambda case: _eval_verifier(case, verifier),
        workers=args.workers,
        checkpoint=run_dir / "verifier_details.jsonl",
    )
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = {
        "run_id": run_id,
        "dataset_id": manifest["dataset_id"],
        "benchmark_kind": "llm-generated and independently adjudicated silver benchmark",
        "human_annotation_count": 0,
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.time() - started, 2),
        "models": {"verifier": SILICONFLOW_REVIEW_MODEL},
        "verifier": _verifier_metrics(results),
        "limitations": [
            "The semantic labels are model-adjudicated silver labels with no human annotation.",
            "Results measure the frozen project corpus and are not human-gold accuracy.",
        ],
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "summary.md").write_text(_summary_markdown(summary), encoding="utf-8")
    failures = [row for row in results if not row.get("passed")]
    (run_dir / "failures.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in failures),
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "run_dir": str(run_dir), "summary": summary}, ensure_ascii=False))


def _eval_verifier(case: dict[str, Any], verifier: EvidenceVerifier) -> dict[str, Any]:
    started = time.time()
    try:
        result = verifier.verify_claim(
            statement=str(case["claim"]),
            evidence_excerpt=str(case["evidence_excerpt"]),
            evidence_ids=[str(value) for value in case["evidence_ids"]],
            structured_required=True,
        )
        predicted_gate = "accept" if result.result == "supported" else "reject"
        predicted_label = (
            result.semantic_result
            if result.semantic_result in {"entailed", "contradicted", "insufficient"}
            else ("entailed" if predicted_gate == "accept" else "insufficient")
        )
        return {
            **case,
            "predicted_gate": predicted_gate,
            "predicted_label": predicted_label,
            "passed": predicted_gate == case["expected_gate"],
            "semantic_exact": predicted_label == case["expected_label"],
            "result": result.to_dict(),
            "elapsed_seconds": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {
            **case,
            "passed": False,
            "semantic_exact": False,
            "error": str(exc),
            "elapsed_seconds": round(time.time() - started, 3),
        }


def _parallel_run(cases, function, *, workers: int, checkpoint: Path) -> list[dict[str, Any]]:
    lock = threading.Lock()
    results: list[dict[str, Any]] = []

    def save(result: dict[str, Any]) -> None:
        with lock:
            results.append(result)
            with checkpoint.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(function, case) for case in cases]
        for future in as_completed(futures):
            save(future.result())
            print(f"[verifier] {len(results)}/{len(cases)}", flush=True)
    order = {str(case.get("id")): index for index, case in enumerate(cases)}
    return sorted(results, key=lambda row: order.get(str(row.get("id")), len(order)))


def _verifier_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    passed = sum(bool(row.get("passed")) for row in rows)
    semantic_exact = sum(bool(row.get("semantic_exact")) for row in rows)
    categories = Counter(str(row.get("category") or "unknown") for row in rows)
    return {
        "count": count,
        "gate_accuracy": passed / count if count else 0.0,
        "semantic_exact_accuracy": semantic_exact / count if count else 0.0,
        "failures": count - passed,
        "categories": dict(categories),
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _summary_markdown(summary: dict[str, Any]) -> str:
    verifier = summary["verifier"]
    return "\n".join([
        "# Evidence Verifier Silver Benchmark",
        "",
        f"- Run: `{summary['run_id']}`",
        f"- Verifier model: `{summary['models']['verifier']}`",
        f"- Cases: {verifier['count']}",
        f"- Gate accuracy: {verifier['gate_accuracy']:.2%}",
        f"- Semantic exact accuracy: {verifier['semantic_exact_accuracy']:.2%}",
        f"- Failures: {verifier['failures']}",
        "",
        "This is a model-adjudicated silver benchmark, not a human-gold benchmark.",
        "",
    ])


if __name__ == "__main__":
    main()
