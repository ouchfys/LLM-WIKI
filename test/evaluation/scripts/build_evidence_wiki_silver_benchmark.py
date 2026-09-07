"""Build a frozen, zero-human silver benchmark from persisted evidence.

The verifier split is authored by an independent teacher model from raw evidence
spans.  The table split is derived deterministically from persisted table cells,
so the system under test never supplies its own expected answers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sqlite3
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from system.core.config import SILICONFLOW_CHAT_MODEL
from system.core.llm_call import invoke_structured
from system.core.siliconflow_client import SiliconFlowChat
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.table_qa import TableResolver


DATASET_ID = "evidence-wiki-silver-v1"
VERIFIER_COUNT = 140
TABLE_COUNT = 100
SEED = 20260813


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="sessions.db")
    parser.add_argument(
        "--output",
        default="test/evaluation/datasets/evidence_wiki_silver_v1",
    )
    parser.add_argument("--teacher-model", default=SILICONFLOW_CHAT_MODEL)
    parser.add_argument("--teacher-workers", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    verifier_path = output / "verifier.jsonl"
    table_path = output / "table_qa.jsonl"
    if not args.force and verifier_path.exists() and table_path.exists():
        raise SystemExit(f"Dataset already exists at {output}; use --force to rebuild it.")

    store = PaperWikiPipelineStore(db_path=args.db)
    teacher = SiliconFlowChat(
        model=args.teacher_model,
        max_retries=1,
        temperature=0.0,
        max_tokens=1600,
    )
    verifier_cases = build_verifier_cases(
        store, teacher, args.teacher_model, workers=args.teacher_workers,
    )
    table_cases = build_table_cases(store)
    if len(verifier_cases) != VERIFIER_COUNT or len(table_cases) != TABLE_COUNT:
        raise RuntimeError(
            f"Unexpected benchmark size: verifier={len(verifier_cases)}, "
            f"table={len(table_cases)}"
        )

    _write_jsonl(verifier_path, verifier_cases)
    _write_jsonl(table_path, table_cases)
    manifest = {
        "dataset_id": DATASET_ID,
        "benchmark_kind": "llm-generated silver benchmark",
        "human_annotation_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": SEED,
        "source_db": str(Path(args.db).resolve()),
        "teacher_model": args.teacher_model,
        "system_under_test": {
            "verifier": "configured SILICONFLOW_REVIEW_MODEL",
            "table_qa": "configured SILICONFLOW_CHAT_MODEL",
        },
        "splits": {
            "verifier": {
                "count": len(verifier_cases),
                "file": verifier_path.name,
                "sha256": _sha256(verifier_path),
                "label_source": "independent teacher model over persisted evidence spans",
            },
            "table_qa": {
                "count": len(table_cases),
                "file": table_path.name,
                "sha256": _sha256(table_path),
                "label_source": "deterministic values and cell IDs from persisted tables",
            },
        },
        "limitations": [
            "No human annotation or adjudication was used.",
            "Verifier labels can contain teacher-model errors or ambiguous cases.",
            "Table cases cover the current 26-paper corpus and are not a universal table-QA benchmark.",
            "Scores must not be reported as human-gold accuracy.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "ok": True,
        "output": str(output),
        "verifier_count": len(verifier_cases),
        "table_count": len(table_cases),
        "teacher_model": args.teacher_model,
    }, ensure_ascii=False))


def build_verifier_cases(
    store: PaperWikiPipelineStore,
    teacher: SiliconFlowChat,
    teacher_model: str,
    *,
    workers: int = 3,
) -> list[dict[str, Any]]:
    seeds = _select_evidence_seeds(store.db_path, count=35)
    categories = [
        ("entailed", "entailed"),
        ("contradicted_relation", "contradicted"),
        ("insufficient_scope", "insufficient"),
        ("insufficient_attribution", "insufficient"),
    ]
    generated: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_generate_teacher_cases, teacher, seed, index, categories): (index, seed)
            for index, seed in enumerate(seeds, 1)
        }
        for future in as_completed(futures):
            index, seed = futures[future]
            generated[index] = (seed, future.result())
            print(f"[build] verifier seed {len(generated):02d}/{len(seeds)}", flush=True)

    output: list[dict[str, Any]] = []
    for index in sorted(generated):
        seed, payload = generated[index]
        by_category = {str(item["category"]): item for item in payload["cases"]}
        for category, expected_label in categories:
            item = by_category[category]
            claim = _clean(str(item["claim"]))
            if not claim:
                raise RuntimeError(f"Empty teacher claim for {seed['element_id']} / {category}")
            case_no = len(output) + 1
            output.append({
                "id": f"verifier-{case_no:03d}",
                "task": "semantic_verifier",
                "category": category,
                "expected_label": expected_label,
                "expected_gate": "accept" if expected_label == "entailed" else "reject",
                "claim": claim,
                "evidence_excerpt": seed["text"],
                "evidence_ids": [seed["element_id"]],
                "source_packet_id": seed["source_packet_id"],
                "source_title": seed["source_title"],
                "page": seed["page"],
                "teacher": {
                    "model": teacher_model,
                    "rationale": _clean(str(item.get("rationale") or "")),
                    "temperature": 0.0,
                    "human_reviewed": False,
                },
            })
    return output


def _generate_teacher_cases(
    teacher: SiliconFlowChat,
    seed: dict[str, Any],
    index: int,
    categories: list[tuple[str, str]],
) -> dict[str, Any]:
    error = ""
    for attempt in range(1, 4):
        try:
            raw = invoke_structured(
                teacher,
                _teacher_prompt(seed, use_chinese=index % 5 == 0),
                temperature=0.0,
                max_tokens=1600,
            )
            payload = _json_object(raw)
            cases = payload.get("cases") if isinstance(payload, dict) else None
            if _valid_teacher_cases(cases, categories):
                return payload
            error = f"invalid teacher payload on attempt {attempt}"
        except Exception as exc:  # retry transient API/JSON failures
            error = str(exc)
    raise RuntimeError(f"Teacher failed for evidence {seed['element_id']}: {error}")


def build_table_cases(store: PaperWikiPipelineStore) -> list[dict[str, Any]]:
    resolver = TableResolver(store)
    with store._connect() as conn:
        table_rows = conn.execute(
            """SELECT t.id, t.source_packet_id, t.caption, t.page, sp.title AS source_title
               FROM document_tables t JOIN source_packets sp ON sp.id=t.source_packet_id
               ORDER BY sp.title, t.page, t.id"""
        ).fetchall()
    table_meta = {str(row["id"]): dict(row) for row in table_rows}
    cells = resolver.evidence_rows(list(table_meta))
    eligible = [
        cell for cell in cells
        if _usable(cell.get("value"))
        and _usable(cell.get("row_label"))
        and _usable(cell.get("column_label"))
        and cell.get("cell_id")
    ]
    rng = random.Random(SEED)
    rng.shuffle(eligible)

    exact = _diverse_cells(eligible, 60)
    output = [_exact_case(index + 1, cell, table_meta[str(cell["table_id"])]) for index, cell in enumerate(exact)]

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for cell in eligible:
        if cell.get("numeric_value") is not None:
            grouped[(str(cell["table_id"]), str(cell["column_label"]))].append(cell)
    numeric_groups = [
        rows for rows in grouped.values()
        if len(rows) >= 2 and len({float(row["numeric_value"]) for row in rows}) >= 2
    ]
    numeric_groups.sort(key=lambda rows: (str(rows[0]["source_title"]), str(rows[0]["table_id"]), str(rows[0]["column_label"])))
    rng.shuffle(numeric_groups)
    if len(numeric_groups) < 30:
        raise RuntimeError(f"Need 30 numeric table groups, found {len(numeric_groups)}")

    for rows in numeric_groups[:20]:
        output.append(_extreme_case(len(output) + 1, rows, table_meta[str(rows[0]["table_id"])], "max"))
    for rows in numeric_groups[20:30]:
        output.append(_extreme_case(len(output) + 1, rows, table_meta[str(rows[0]["table_id"])], "min"))

    cross_pairs = _cross_source_pairs(eligible, count=10)
    for left, right in cross_pairs:
        output.append(_comparison_case(len(output) + 1, left, right, table_meta))
    return output


def _select_evidence_seeds(db_path: str, count: int) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT de.id AS element_id, de.source_packet_id, de.text, de.page,
                      de.reading_order, sp.title AS source_title
               FROM document_elements de
               JOIN source_packets sp ON sp.id=de.source_packet_id
               WHERE de.element_type NOT IN ('title','section_header','page_header','page_footer')
                 AND length(trim(de.text)) BETWEEN 420 AND 1800
               ORDER BY sp.title, de.reading_order"""
        ).fetchall()
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        if _evidence_quality(item["text"]):
            by_source[str(item["source_packet_id"])].append(item)
    ordered_sources = sorted(by_source, key=lambda key: by_source[key][0]["source_title"])
    selected: list[dict[str, Any]] = []
    depth = 0
    while len(selected) < count:
        added = False
        for source_id in ordered_sources:
            candidates = by_source[source_id]
            position = min(depth * 3, max(0, len(candidates) - 1))
            if depth < len(candidates) and candidates[position] not in selected:
                selected.append(candidates[position])
                added = True
                if len(selected) == count:
                    break
        if not added:
            break
        depth += 1
    if len(selected) != count:
        raise RuntimeError(f"Need {count} diverse evidence spans, found {len(selected)}")
    return selected


def _teacher_prompt(seed: dict[str, Any], *, use_chinese: bool) -> str:
    language = (
        "Write all four claims in Chinese while preserving technical names and numbers."
        if use_chinese else
        "Write the claims in the evidence's language."
    )
    return f"""You are authoring a frozen silver benchmark for evidence entailment.
Use ONLY SOURCE EVIDENCE. Do not use outside knowledge. {language}
Return strict JSON as {{"cases":[...]}} with exactly these four cases and labels:
1. category=entailed, label=entailed: a concise claim fully supported by the evidence.
2. category=contradicted_relation, label=contradicted: reverse an explicit relation,
   comparison, presence/absence, or conclusion so the evidence directly conflicts with it.
3. category=insufficient_scope, label=insufficient: add a universal scope, population,
   condition, or guarantee that the evidence neither proves nor directly disproves.
4. category=insufficient_attribution, label=insufficient: add a causal attribution,
   mechanism, external actor, or result that the evidence does not establish.

Every item must contain category, claim, label, rationale. Claims must be independently
readable and under 45 words (or 80 Chinese characters). Do not introduce a new numeric
value unless it literally occurs in the evidence. Keep insufficient claims plausible but
clearly not entailed. Do not turn an insufficient case into a direct contradiction.

SOURCE TITLE: {seed['source_title']}
PAGE: {seed['page']}
SOURCE EVIDENCE:
{seed['text']}
"""


def _valid_teacher_cases(cases: Any, expected: list[tuple[str, str]]) -> bool:
    if not isinstance(cases, list) or len(cases) != len(expected):
        return False
    actual = {(str(item.get("category")), str(item.get("label"))) for item in cases if isinstance(item, dict)}
    return actual == set(expected) and all(_clean(str(item.get("claim") or "")) for item in cases)


def _exact_case(case_no: int, cell: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    question = (
        f"In the paper '{cell['source_title']}', table '{_short(meta['caption'], 180)}', "
        f"what is the exact value at row '{cell['row_label']}' and column "
        f"'{cell['column_label']}'? Return the table value exactly."
    )
    return _table_case(case_no, "exact_cell", question, [cell], [str(cell["source_packet_id"])])


def _extreme_case(case_no: int, rows: list[dict[str, Any]], meta: dict[str, Any], mode: str) -> dict[str, Any]:
    extreme = max if mode == "max" else min
    expected = extreme(rows, key=lambda row: float(row["numeric_value"]))
    word = "highest" if mode == "max" else "lowest"
    question = (
        f"In the paper '{expected['source_title']}', table '{_short(meta['caption'], 180)}', "
        f"which row has the {word} numeric value in column '{expected['column_label']}'? "
        "Return the row label and exact table value."
    )
    case = _table_case(case_no, f"within_table_{mode}", question, [expected], [str(expected["source_packet_id"])])
    case["candidate_cell_ids"] = [str(row["cell_id"]) for row in rows]
    return case


def _comparison_case(
    case_no: int,
    left: dict[str, Any],
    right: dict[str, Any],
    table_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    winner = left if float(left["numeric_value"]) > float(right["numeric_value"]) else right
    question = (
        f"Compare two table cells: (A) paper '{left['source_title']}', row '{left['row_label']}', "
        f"column '{left['column_label']}'; and (B) paper '{right['source_title']}', row "
        f"'{right['row_label']}', column '{right['column_label']}'. Which numeric value is larger? "
        "Return A or B, both exact values, and retain both cell citations."
    )
    case = _table_case(
        case_no,
        "cross_paper_comparison",
        question,
        [left, right],
        [str(left["source_packet_id"]), str(right["source_packet_id"])],
    )
    case["winner_cell_id"] = str(winner["cell_id"])
    case["winner_source_title"] = str(winner["source_title"])
    case["expected_table_ids"] = [str(left["table_id"]), str(right["table_id"])]
    return case


def _table_case(
    case_no: int,
    category: str,
    question: str,
    expected_cells: list[dict[str, Any]],
    source_ids: list[str],
) -> dict[str, Any]:
    return {
        "id": f"table-{case_no:03d}",
        "task": "table_qa",
        "category": category,
        "question": question,
        "source_packet_ids": list(dict.fromkeys(source_ids)),
        "expected_cell_ids": [str(cell["cell_id"]) for cell in expected_cells],
        "expected_table_ids": list(dict.fromkeys(str(cell["table_id"]) for cell in expected_cells)),
        "expected_values": [str(cell["value"]) for cell in expected_cells],
        "expected_row_labels": [str(cell["row_label"]) for cell in expected_cells],
        "expected_column_labels": [str(cell["column_label"]) for cell in expected_cells],
        "expected_pages": [int(cell["page"] or 0) for cell in expected_cells],
        "label_source": "deterministic persisted table cells",
        "human_reviewed": False,
    }


def _diverse_cells(cells: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_tables: set[str] = set()
    seen_cells: set[str] = set()
    for pass_no in (0, 1):
        for cell in cells:
            table_id = str(cell["table_id"])
            cell_id = str(cell["cell_id"])
            if cell_id in seen_cells or (pass_no == 0 and table_id in seen_tables):
                continue
            selected.append(cell)
            seen_cells.add(cell_id)
            seen_tables.add(table_id)
            if len(selected) == count:
                return selected
    raise RuntimeError(f"Need {count} eligible table cells, found {len(selected)}")


def _cross_source_pairs(cells: list[dict[str, Any]], count: int) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    numeric = [cell for cell in cells if cell.get("numeric_value") is not None]
    numeric.sort(key=lambda cell: (str(cell["source_title"]), str(cell["table_id"]), str(cell["cell_id"])))
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used: set[str] = set()
    for offset in range(1, len(numeric)):
        for index, left in enumerate(numeric):
            right = numeric[(index + offset) % len(numeric)]
            if left["source_packet_id"] == right["source_packet_id"]:
                continue
            if float(left["numeric_value"]) == float(right["numeric_value"]):
                continue
            if left["cell_id"] in used or right["cell_id"] in used:
                continue
            pairs.append((left, right))
            used.update((str(left["cell_id"]), str(right["cell_id"])))
            if len(pairs) == count:
                return pairs
    raise RuntimeError(f"Need {count} cross-source numeric pairs, found {len(pairs)}")


def _evidence_quality(text: str) -> bool:
    value = _clean(text)
    if len(value.split()) < 55:
        return False
    alpha = sum(char.isalpha() for char in value)
    return alpha / max(len(value), 1) > 0.55 and not value.lower().startswith("references")


def _usable(value: Any) -> bool:
    text = _clean(str(value or ""))
    return bool(text and text not in {"-", "--", "—", "n/a"} and len(text) <= 180)


def _short(value: Any, limit: int) -> str:
    text = _clean(str(value or "untitled table"))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


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
