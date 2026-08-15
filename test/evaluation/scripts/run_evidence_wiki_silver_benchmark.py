"""Evaluate the production Verifier and Table QA on the frozen silver set."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.deps import get_chat_llm, get_review_llm
from system.core.config import SILICONFLOW_CHAT_MODEL, SILICONFLOW_REVIEW_MODEL
from system.wiki.evidence_verifier import EvidenceVerifier
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.table_qa import TableQuestionAnswerer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="sessions.db")
    parser.add_argument(
        "--dataset",
        default="test/evaluation/datasets/evidence_wiki_silver_v1",
    )
    parser.add_argument(
        "--output-root",
        default="test/evaluation/runs/evidence_wiki_silver_v1",
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--verifier-limit", type=int, default=0)
    parser.add_argument("--table-limit", type=int, default=0)
    parser.add_argument("--skip-verifier", action="store_true")
    parser.add_argument("--skip-table", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.time()
    dataset_dir = Path(args.dataset)
    verifier_cases = _read_jsonl(dataset_dir / "verifier.jsonl")
    table_cases = _read_jsonl(dataset_dir / "table_qa.jsonl")
    if args.skip_verifier:
        verifier_cases = []
    if args.skip_table:
        table_cases = []
    if args.verifier_limit:
        verifier_cases = verifier_cases[: args.verifier_limit]
    if args.table_limit:
        table_cases = table_cases[: args.table_limit]

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    store = PaperWikiPipelineStore(db_path=args.db)
    verifier = EvidenceVerifier(store, llm=get_review_llm())
    table_qa = TableQuestionAnswerer(store, llm=get_chat_llm())

    verifier_results = _parallel_run(
        verifier_cases,
        lambda case: _eval_verifier(case, verifier),
        workers=args.workers,
        label="verifier",
        checkpoint=run_dir / "verifier_details.jsonl",
    )
    table_results = _parallel_run(
        table_cases,
        lambda case: _eval_table(case, table_qa),
        workers=args.workers,
        label="table",
        checkpoint=run_dir / "table_qa_details.jsonl",
    )

    summary = {
        "run_id": run_id,
        "dataset_id": json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))["dataset_id"],
        "benchmark_kind": "llm-generated silver benchmark",
        "human_annotation_count": 0,
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.time() - started, 2),
        "models": {
            "verifier": SILICONFLOW_REVIEW_MODEL,
            "table_qa": SILICONFLOW_CHAT_MODEL,
        },
        "verifier": _verifier_metrics(verifier_results),
        "table_qa": _table_metrics(table_results),
        "limitations": [
            "The semantic labels are teacher-model silver labels with no human adjudication.",
            "The table labels are deterministic, but questions are generated from the current corpus.",
            "Results measure this frozen 26-paper corpus and must not be described as human-gold accuracy.",
        ],
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "summary.md").write_text(_summary_markdown(summary), encoding="utf-8")
    failures = [row for row in verifier_results + table_results if not row.get("passed")]
    (run_dir / "failures.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in failures),
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "run_dir": str(run_dir), "summary": summary}, ensure_ascii=False, default=str))


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
            else "precheck_reject" if predicted_gate == "reject" else "unknown"
        )
        return {
            **case,
            "predicted_gate": predicted_gate,
            "predicted_label": predicted_label,
            "passed": predicted_gate == case["expected_gate"],
            "semantic_exact": predicted_label == case["expected_label"],
            "result": result.as_dict(),
            "elapsed_seconds": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {**case, "passed": False, "error": str(exc), "elapsed_seconds": round(time.time() - started, 3)}


def _eval_table(case: dict[str, Any], table_qa: TableQuestionAnswerer) -> dict[str, Any]:
    started = time.time()
    try:
        result = table_qa.answer(
            str(case["question"]),
            source_packet_ids=[str(value) for value in case["source_packet_ids"]],
            limit=10,
        )
        resolved_tables = {str(row.get("table_id")) for row in result.get("tables") or []}
        cited_cells = {str(row.get("cell_id")) for row in result.get("citations") or []}
        returned_cells = {str(row.get("cell_id")) for row in result.get("rows") or [] if row.get("cell_id")}
        expected_tables = {str(value) for value in case["expected_table_ids"]}
        expected_cells = {str(value) for value in case["expected_cell_ids"]}
        values_found = all(_value_present(value, result) for value in case["expected_values"])
        labels_found = all(_label_present(value, result) for value in case["expected_row_labels"])
        resolver_recall = expected_tables.issubset(resolved_tables)
        row_recall = expected_cells.issubset(returned_cells)
        citation_recall = expected_cells.issubset(cited_cells)
        answer_correct = values_found and (
            labels_found if case["category"] != "exact_cell" else True
        )
        passed = resolver_recall and row_recall and citation_recall and answer_correct
        return {
            **case,
            "passed": passed,
            "resolver_recall": resolver_recall,
            "row_recall": row_recall,
            "citation_recall": citation_recall,
            "answer_correct": answer_correct,
            "sql_planned": bool(result.get("sql")),
            "sql_success": bool(result.get("sql")) and not result.get("sql_error"),
            "engine": result.get("engine"),
            "actual": result,
            "elapsed_seconds": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {**case, "passed": False, "error": str(exc), "elapsed_seconds": round(time.time() - started, 3)}


def _parallel_run(
    cases: list[dict[str, Any]],
    fn: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    workers: int,
    label: str,
    checkpoint: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    checkpoint.write_text("", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fn, case): case["id"] for case in cases}
        for future in as_completed(futures):
            row = future.result()
            with lock:
                results.append(row)
                with checkpoint.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                done = len(results)
            if done % 10 == 0 or done == len(cases):
                passed = sum(bool(item.get("passed")) for item in results)
                print(f"[eval] {label} {done}/{len(cases)} passed={passed}", flush=True)
    results.sort(key=lambda row: str(row["id"]))
    checkpoint.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in results),
        encoding="utf-8",
    )
    return results


def _verifier_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(bool(row.get("passed")) for row in rows)
    entailed = [row for row in rows if row["expected_gate"] == "accept"]
    unsupported = [row for row in rows if row["expected_gate"] == "reject"]
    false_reject = sum(row.get("predicted_gate") != "accept" for row in entailed)
    false_accept = sum(row.get("predicted_gate") == "accept" for row in unsupported)
    semantic_rows = [row for row in rows if row.get("predicted_label") in {"entailed", "contradicted", "insufficient"}]
    categories = _by_category(rows, lambda row: bool(row.get("passed")))
    return {
        "count": total,
        "gate_accuracy": _rate(passed, total),
        "false_accept_rate": _rate(false_accept, len(unsupported)),
        "false_reject_rate": _rate(false_reject, len(entailed)),
        "semantic_exact_accuracy_on_llm_reached": _rate(
            sum(bool(row.get("semantic_exact")) for row in semantic_rows), len(semantic_rows)
        ),
        "semantic_llm_reached_count": len(semantic_rows),
        "error_count": sum(bool(row.get("error")) for row in rows),
        "average_latency_seconds": _average(row.get("elapsed_seconds", 0) for row in rows),
        "by_category": categories,
        "confusion": dict(Counter(
            f"{row['expected_label']}->{row.get('predicted_label', 'error')}" for row in rows
        )),
    }


def _table_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    fields = ["passed", "resolver_recall", "row_recall", "citation_recall", "answer_correct", "sql_planned", "sql_success"]
    result = {"count": total}
    for field in fields:
        result[f"{field}_rate"] = _rate(sum(bool(row.get(field)) for row in rows), total)
    result.update({
        "fallback_rate": _rate(sum(row.get("engine") == "table-resolver-fallback" for row in rows), total),
        "error_count": sum(bool(row.get("error")) for row in rows),
        "average_latency_seconds": _average(row.get("elapsed_seconds", 0) for row in rows),
        "by_category": _by_category(rows, lambda row: bool(row.get("passed"))),
    })
    return result


def _by_category(rows: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["category"])].append(row)
    return {
        category: {"count": len(items), "accuracy": _rate(sum(predicate(item) for item in items), len(items))}
        for category, items in sorted(grouped.items())
    }


def _value_present(expected: Any, result: dict[str, Any]) -> bool:
    needle = _normalize_text(expected)
    if not needle:
        return True
    answer = _normalize_text(result.get("answer"))
    if needle in answer:
        return True
    return any(needle == _normalize_text(row.get("value")) for row in result.get("rows") or [])


def _label_present(expected: Any, result: dict[str, Any]) -> bool:
    needle = _normalize_text(expected)
    if not needle:
        return True
    answer = _normalize_text(result.get("answer"))
    if needle in answer:
        return True
    return any(needle == _normalize_text(row.get("row_label")) for row in result.get("rows") or [])


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower().replace(",", "")


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _average(values: Any) -> float:
    numbers = [float(value) for value in values]
    return round(sum(numbers) / len(numbers), 3) if numbers else 0.0


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _summary_markdown(summary: dict[str, Any]) -> str:
    verifier = summary["verifier"]
    table = summary["table_qa"]
    lines = [
        "# Evidence Wiki Silver Benchmark",
        "",
        f"- Run: `{summary['run_id']}`",
        f"- Dataset: `{summary['dataset_id']}`",
        f"- Benchmark type: **{summary['benchmark_kind']}** (0 human annotations)",
        f"- Verifier model: `{summary['models']['verifier']}`",
        f"- Table QA model: `{summary['models']['table_qa']}`",
        f"- Runtime: {summary['elapsed_seconds']} seconds",
        "",
        "## Verifier",
        "",
        f"- Cases: {verifier['count']}",
        f"- Gate accuracy: {verifier['gate_accuracy']:.2%}",
        f"- False accept rate: {verifier['false_accept_rate']:.2%}",
        f"- False reject rate: {verifier['false_reject_rate']:.2%}",
        f"- Semantic exact accuracy (cases reaching LLM): {verifier['semantic_exact_accuracy_on_llm_reached']:.2%}",
        "",
        "## Table QA",
        "",
        f"- Cases: {table['count']}",
        f"- End-to-end pass rate: {table['passed_rate']:.2%}",
        f"- Resolver recall: {table['resolver_recall_rate']:.2%}",
        f"- Result-row recall: {table['row_recall_rate']:.2%}",
        f"- Cell-citation recall: {table['citation_recall_rate']:.2%}",
        f"- Answer correctness: {table['answer_correct_rate']:.2%}",
        f"- SQL success: {table['sql_success_rate']:.2%}",
        "",
        "## Honest boundary",
        "",
        "These are silver-label results without human adjudication. They must not be presented as human-gold accuracy.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
