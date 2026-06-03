"""Run the Agentic Wiki answer-confidence evaluation against the real API."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import statistics
import textwrap
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import requests

TEST_EVAL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentic_wiki_answer_reviewer import AgenticWikiAnswerReviewer

DEFAULT_DATASET = TEST_EVAL_ROOT / "datasets" / "wiki_chat" / "all_agentic_wiki_eval.csv"
DEFAULT_API_URL = "http://127.0.0.1:8000/api/wiki/chat"
DEFAULT_SESSION_ID = "eval-agentic-wiki"

DETAIL_FIELDS = [
    "id",
    "query",
    "query_type",
    "difficulty",
    "expected_source",
    "expected_card_titles",
    "allow_web",
    "answer",
    "citations",
    "resources",
    "tool_plan",
    "latency_seconds",
    "retrieval_hit",
    "top1_hit",
    "web_allowed",
    "web_used",
    "tool_routing_correct",
    "answer_confidence",
    "answer_completeness",
    "citation_grounding",
    "unsupported_claim_risk",
    "final_score",
    "reviewer_reason",
]


def load_dataset(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows


def filter_rows(rows: Sequence[Dict[str, str]], ids: List[str], limit: int | None) -> List[Dict[str, str]]:
    filtered = list(rows)
    if ids:
        id_set = set(ids)
        filtered = [row for row in filtered if row.get("id") in id_set]
    if limit is not None:
        filtered = filtered[:limit]
    return filtered


def split_pipe(value: str) -> List[str]:
    return [item.strip() for item in str(value or "").split("|") if item.strip()]


def citation_titles(citations: Iterable[Dict[str, Any]]) -> List[str]:
    titles = []
    for item in citations or []:
        title = str(item.get("title") or "").strip()
        if title:
            titles.append(title)
    return titles


def retrieval_metrics(row: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    expected_cards = split_pipe(row.get("expected_card_titles", ""))
    expected_source = str(row.get("expected_source", "")).strip()
    citations = payload.get("citations") or []
    titles = citation_titles(citations)
    lower_titles = [title.lower() for title in titles]
    expected_lower = [title.lower() for title in expected_cards if title]
    hits = sum(1 for title in expected_lower if any(title in actual for actual in lower_titles))
    retrieval_hit_rate = hits / len(expected_lower) if expected_lower else 0.0
    top1_title = lower_titles[0] if lower_titles else ""
    top1_hit = bool(expected_lower and any(title in top1_title for title in expected_lower))
    if not expected_lower and expected_source:
        retrieval_hit_rate = 1.0 if any(expected_source.lower() in title for title in lower_titles) else 0.0
        top1_hit = bool(lower_titles and expected_source.lower() in top1_title)
    return {
        "retrieval_hit_rate": retrieval_hit_rate,
        "top1_hit": top1_hit,
        "citation_titles": titles,
    }


def tool_routing_correct(row: Dict[str, str], tool_plan: Dict[str, Any]) -> bool:
    query_type = str(row.get("query_type", "")).lower()
    use_wiki = bool(tool_plan.get("use_wiki"))
    use_web = bool(tool_plan.get("use_web"))
    use_resources = bool(tool_plan.get("use_resources"))
    if "resource" not in query_type and use_resources:
        return False
    return use_wiki or use_web or use_resources


def call_api(url: str, session_id: str, query: str, timeout_seconds: float) -> tuple[Dict[str, Any], float]:
    started = time.perf_counter()
    response = requests.post(
        url,
        json={"message": query, "session_id": session_id, "stream": False},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    latency = time.perf_counter() - started
    return response.json(), latency


def ensure_log_dir(path: Path | None) -> Path:
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        return path
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = TEST_EVAL_ROOT / "runs" / f"agentic_wiki_eval_{stamp}"
    output.mkdir(parents=True, exist_ok=True)
    return output


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DETAIL_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in DETAIL_FIELDS})


def summarize(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {
            "count": 0,
            "overall_final_score": 0.0,
            "overall_answer_confidence": 0.0,
            "overall_citation_grounding": 0.0,
            "pass_rate": 0.0,
            "wrong_web_usage_rate": 0.0,
            "no_citation_rate": 0.0,
            "retrieval_hit_rate": 0.0,
            "top1_hit_rate": 0.0,
            "mean_citations_per_answer": 0.0,
            "splits": {},
        }

    def avg(key: str) -> float:
        return statistics.mean(float(item.get(key, 0.0)) for item in results)

    wrong_web = sum(1 for item in results if str(item.get("allow_web", "")).lower() == "false" and str(item.get("web_used", "")).lower() == "true")
    no_citation = sum(1 for item in results if not str(item.get("citations", "")).strip())
    passed = sum(1 for item in results if float(item.get("final_score", 0.0)) >= 0.75 and float(item.get("answer_confidence", 0.0)) >= 0.75 and float(item.get("citation_grounding", 0.0)) >= 0.65)

    splits: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in results:
        item_id = str(item.get("id", ""))
        if item_id.startswith("base_"):
            prefix = "base_50"
        elif item_id.startswith("new_"):
            prefix = "new_difficulty_papers_20"
        else:
            prefix = "current_papers_30"
        splits[prefix].append(item)

    split_summary = {}
    for name, items in splits.items():
        split_summary[name] = {
            "count": len(items),
            "overall_final_score": round(statistics.mean(float(item.get("final_score", 0.0)) for item in items), 4),
            "overall_answer_confidence": round(statistics.mean(float(item.get("answer_confidence", 0.0)) for item in items), 4),
            "overall_citation_grounding": round(statistics.mean(float(item.get("citation_grounding", 0.0)) for item in items), 4),
        }

    return {
        "count": len(results),
        "overall_final_score": round(avg("final_score"), 4),
        "overall_answer_confidence": round(avg("answer_confidence"), 4),
        "overall_citation_grounding": round(avg("citation_grounding"), 4),
        "pass_rate": round(passed / len(results), 4),
        "wrong_web_usage_rate": round(wrong_web / len(results), 4),
        "no_citation_rate": round(no_citation / len(results), 4),
        "retrieval_hit_rate": round(avg("retrieval_hit"), 4),
        "top1_hit_rate": round(avg("top1_hit"), 4),
        "mean_citations_per_answer": round(statistics.mean(len(json.loads(item.get("citations", "[]"))) for item in results), 4),
        "splits": split_summary,
    }


def failure_reason_bucket(result: Dict[str, Any]) -> str:
    if float(result.get("tool_routing_correct", 0.0)) < 0.5:
        return "tool_routing_wrong"
    if float(result.get("retrieval_hit", 0.0)) < 0.5:
        return "retrieval_rank_wrong"
    if float(result.get("citation_grounding", 0.0)) < 0.55:
        return "citation_grounding_weak"
    if float(result.get("answer_confidence", 0.0)) < 0.6:
        return "answer_synthesis_unfaithful"
    return "wiki_card_content_too_weak"


def write_failed_cases(path: Path, failed: Sequence[Dict[str, Any]]) -> None:
    lines: List[str] = []
    for item in failed:
        lines.extend([
            f"## {item['id']} {item['query']}",
            "",
            "Expected:",
            f"- source: {item['expected_source']}",
            f"- cards: {item['expected_card_titles']}",
            f"- allow_web: {item['allow_web']}",
            "",
            "Actual:",
            f"- tool_plan: {item['tool_plan']}",
            f"- citations: {item['citations']}",
            f"- answer excerpt: {textwrap.shorten(item['answer'], width=300, placeholder='...')}",
            "",
            "Reviewer:",
            f"- final_score: {item['final_score']}",
            f"- answer_confidence: {item['answer_confidence']}",
            f"- citation_grounding: {item['citation_grounding']}",
            f"- reason: {item['reviewer_reason']}",
            "",
            "Likely fix:",
            f"- {failure_reason_bucket(item)}",
            "",
        ])
    path.write_text("\n".join(lines).strip() + ("\n" if lines else ""), encoding="utf-8")


def write_summary_markdown(path: Path, summary: Dict[str, Any], results: Sequence[Dict[str, Any]]) -> None:
    failed = sorted(results, key=lambda item: float(item.get("final_score", 0.0)))[:10]
    lines = [
        "# Agentic Wiki Eval Summary",
        "",
        f"- Overall final_score: {summary['overall_final_score']}",
        f"- Overall answer_confidence: {summary['overall_answer_confidence']}",
        f"- Overall citation_grounding: {summary['overall_citation_grounding']}",
        f"- Pass rate: {summary['pass_rate']}",
        f"- Retrieval hit rate: {summary['retrieval_hit_rate']}",
        f"- Top1 hit rate: {summary['top1_hit_rate']}",
        f"- Wrong web usage rate: {summary['wrong_web_usage_rate']}",
        f"- No citation rate: {summary['no_citation_rate']}",
        "",
        "## Splits",
        "",
    ]
    for name, item in summary.get("splits", {}).items():
        lines.extend([
            f"### {name}",
            f"- count: {item['count']}",
            f"- overall_final_score: {item['overall_final_score']}",
            f"- overall_answer_confidence: {item['overall_answer_confidence']}",
            f"- overall_citation_grounding: {item['overall_citation_grounding']}",
            "",
        ])
    lines.extend(["## Top 10 failed cases", ""])
    for item in failed:
        lines.extend([
            f"- {item['id']} | final_score={item['final_score']} | answer_confidence={item['answer_confidence']} | {failure_reason_bucket(item)}",
        ])
    lines.extend([
        "",
        "## Recommended fixes",
        "",
        "- retrieval_alias_missing: add aliases or expected topic rewrites",
        "- retrieval_rank_wrong: improve wiki_search ranking or query rewrite",
        "- wiki_card_content_too_weak: strengthen distilled card fields",
        "- tool_routing_wrong: adjust tool router or deterministic fallback",
        "- answer_synthesis_unfaithful: tighten answer prompt and citation usage",
        "- citation_grounding_weak: improve citation discipline and card selection",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--session-id", default=DEFAULT_SESSION_ID)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--ids", nargs="*", default=[])
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--disable-review-llm", action="store_true")
    parser.add_argument("--shared-session", action="store_true")
    args = parser.parse_args()

    rows = load_dataset(args.dataset)
    rows = filter_rows(rows, args.ids, args.limit)
    output_dir = ensure_log_dir(args.output_dir)
    reviewer = AgenticWikiAnswerReviewer(llm=None) if args.disable_review_llm else AgenticWikiAnswerReviewer.create_default()
    session_prefix = (args.session_id or DEFAULT_SESSION_ID).strip() or DEFAULT_SESSION_ID
    run_token = output_dir.name

    raw_rows: List[Dict[str, Any]] = []
    csv_rows: List[Dict[str, Any]] = []
    for row in rows:
        session_id = session_prefix if args.shared_session else f"{session_prefix}-{run_token}-{row['id']}"
        payload, latency = call_api(args.api_url, session_id, row["query"], args.timeout_seconds)
        metrics = retrieval_metrics(row, payload)
        review = reviewer.review(row, payload, metrics["retrieval_hit_rate"], metrics["top1_hit"])
        tool_plan = payload.get("tool_plan") or {}
        csv_row = {
            "id": row["id"],
            "query": row["query"],
            "query_type": row["query_type"],
            "difficulty": row["difficulty"],
            "expected_source": row["expected_source"],
            "expected_card_titles": row["expected_card_titles"],
            "allow_web": row["allow_web"],
            "answer": payload.get("answer", ""),
            "citations": json.dumps(payload.get("citations", []), ensure_ascii=False),
            "resources": json.dumps(payload.get("resources", []), ensure_ascii=False),
            "tool_plan": json.dumps(tool_plan, ensure_ascii=False),
            "latency_seconds": round(latency, 4),
            "retrieval_hit": round(metrics["retrieval_hit_rate"], 4),
            "top1_hit": 1.0 if metrics["top1_hit"] else 0.0,
            "web_allowed": row["allow_web"],
            "web_used": str(bool(tool_plan.get("use_web"))).lower(),
            "tool_routing_correct": round(1.0 if tool_routing_correct(row, tool_plan) else 0.0, 4),
            "answer_confidence": review.answer_confidence,
            "answer_completeness": review.answer_completeness,
            "citation_grounding": review.citation_grounding,
            "unsupported_claim_risk": review.unsupported_claim_risk,
            "final_score": review.final_score,
            "reviewer_reason": review.reason,
        }
        raw_rows.append({
            "dataset_row": row,
            "session_id": session_id,
            "answer_payload": payload,
            "latency_seconds": latency,
            "metrics": metrics,
            "review": review.to_dict(),
        })
        csv_rows.append(csv_row)
        print(f"[done] {row['id']} final_score={review.final_score:.4f} latency={latency:.2f}s", flush=True)

    summary = summarize(csv_rows)
    failed = sorted(csv_rows, key=lambda item: float(item.get("final_score", 0.0)))[:10]

    write_csv(output_dir / "queries_details.csv", csv_rows)
    write_jsonl(output_dir / "queries_details.jsonl", raw_rows)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_failed_cases(output_dir / "failed_cases.md", failed)
    write_summary_markdown(output_dir / "summary.md", summary, csv_rows)

    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"Results written to {output_dir}", flush=True)


if __name__ == "__main__":
    main()
