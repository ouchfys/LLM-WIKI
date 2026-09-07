"""Independently adjudicate verifier silver labels without human involvement."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.core.config import SILICONFLOW_SUMMARY_MODEL
from system.core.llm_call import invoke_structured
from system.core.siliconflow_client import SiliconFlowChat


LABELS = {"entailed", "contradicted", "insufficient"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default="test/evaluation/datasets/evidence_wiki_silver_v1",
    )
    parser.add_argument("--model", default=SILICONFLOW_SUMMARY_MODEL)
    parser.add_argument("--workers", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.dataset)
    path = root / "verifier.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["evidence_ids"][0])].append(row)

    judge = SiliconFlowChat(model=args.model, max_retries=1, temperature=0.0, max_tokens=1800)
    decisions: dict[str, dict[str, Any]] = {}
    groups = sorted(grouped.items())
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(_judge_group, judge, cases): evidence_id
            for evidence_id, cases in groups
        }
        for future in as_completed(futures):
            payload = future.result()
            for item in payload:
                decisions[str(item["id"])] = item
            print(f"[adjudicate] {len(decisions)}/{len(rows)}", flush=True)

    if set(decisions) != {str(row["id"]) for row in rows}:
        missing = sorted({str(row["id"]) for row in rows} - set(decisions))
        raise RuntimeError(f"Adjudication missing cases: {missing}")

    changes = 0
    for row in rows:
        decision = decisions[str(row["id"])]
        original = str(row["expected_label"])
        final = str(decision["label"])
        if final != original:
            changes += 1
        row["teacher"]["original_label"] = original
        row["adjudicator"] = {
            "model": args.model,
            "label": final,
            "rationale": str(decision.get("rationale") or ""),
            "agrees_with_teacher": final == original,
            "human_reviewed": False,
        }
        row["expected_label"] = final
        row["expected_gate"] = "accept" if final == "entailed" else "reject"
        row["label_policy"] = "independent adjudicator final label"

    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["adjudicated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["adjudicator_model"] = args.model
    manifest["verifier_label_policy"] = "independent adjudicator final label"
    manifest["teacher_adjudicator_disagreements"] = changes
    manifest["splits"]["verifier"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["splits"]["verifier"]["label_source"] = (
        "teacher-generated cases independently relabeled by a second model over persisted evidence spans"
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "cases": len(rows), "label_changes": changes, "model": args.model}, ensure_ascii=False))


def _judge_group(judge: SiliconFlowChat, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt = """You are the independent final adjudicator of a silver entailment benchmark.
Judge each CLAIM only against SOURCE EVIDENCE; never use paper-title knowledge or outside
facts. Labels: entailed = fully supported; contradicted = evidence directly conflicts;
insufficient = plausible but not established, overbroad, or attribution/causality is only
implied. Be strict about all/every/always/solely, causal language, named entities absent
from the span, conditions, comparison direction, and modality. Return strict JSON as
{"decisions":[{"id":"...","label":"entailed|contradicted|insufficient","rationale":"..."}]}.
Return exactly one decision per case.

SOURCE EVIDENCE:
""" + str(cases[0]["evidence_excerpt"]) + "\n\nCASES:\n" + json.dumps(
        [{"id": row["id"], "claim": row["claim"]} for row in cases],
        ensure_ascii=False,
        indent=2,
    )
    expected_ids = {str(row["id"]) for row in cases}
    error = ""
    for attempt in range(1, 4):
        try:
            payload = _json_object(invoke_structured(judge, prompt, temperature=0.0, max_tokens=1800))
            decisions = payload.get("decisions")
            if (
                isinstance(decisions, list)
                and {str(item.get("id")) for item in decisions if isinstance(item, dict)} == expected_ids
                and all(str(item.get("label")) in LABELS for item in decisions)
            ):
                return decisions
            error = f"invalid payload on attempt {attempt}"
        except Exception as exc:
            error = str(exc)
    raise RuntimeError(f"Adjudicator failed: {error}")


def _json_object(value: Any) -> dict[str, Any]:
    match = re.search(r"\{.*\}", str(value or ""), flags=re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


if __name__ == "__main__":
    main()
