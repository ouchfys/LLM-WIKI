"""Build the current-paper Agentic Wiki Chat evaluation dataset.

The current benchmark is intentionally compact: 30 questions, five for each
ingested PaperPage. It evaluates the private Wiki's current paper knowledge
instead of the older 50-question historical benchmark.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List


TEST_EVAL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = TEST_EVAL_ROOT / "datasets" / "wiki_chat"

FIELDS = [
    "id",
    "source",
    "query",
    "query_type",
    "difficulty",
    "expected_keywords",
    "expected_source",
    "expected_card_titles",
    "allow_web",
    "notes",
]


PAPERS = {
    "attention": "Attention Is All You Need",
    "deepseek_r1": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning",
    "rl_reasoning": "Does Reinforcement Learning Really Incentivize Reasoning Capacity in LLMs Beyond the Base Model?",
    "lawyer_system": "律师推荐和服务小程序的设计与实现",
    "probing_difficulty": "Probing the Difficulty Perception Mechanism of Large Language Models",
    "llm_knows": "The LLM Already Knows: Estimating LLM-Perceived Question Difficulty via Hidden Representations",
}


def current_papers_30() -> List[Dict[str, str]]:
    questions = [
        # Attention Is All You Need
        ("paper_001", "attention", "What is the core architecture proposed in Attention Is All You Need?", "concept_explanation", "easy", "Transformer | self-attention | encoder-decoder"),
        ("paper_002", "attention", "Why does scaled dot-product attention divide by sqrt(d_k)?", "mechanism", "medium", "scaled dot-product attention | sqrt(d_k) | softmax stability"),
        ("paper_003", "attention", "How does multi-head attention help the Transformer model?", "method", "medium", "multi-head attention | representation subspaces | parallel heads"),
        ("paper_004", "attention", "What role does positional encoding play in the Transformer?", "mechanism", "medium", "positional encoding | sequence order | sinusoidal"),
        ("paper_005", "attention", "Explain the encoder-decoder structure of the Transformer for an interview answer.", "interview", "medium", "encoder | decoder | interview answer"),

        # DeepSeek-R1 / GRPO
        ("paper_006", "deepseek_r1", "What is the main goal of DeepSeek-R1?", "concept_explanation", "easy", "reasoning capability | reinforcement learning | DeepSeek-R1"),
        ("paper_007", "deepseek_r1", "How does reinforcement learning incentivize reasoning behavior in DeepSeek-R1?", "method", "medium", "reinforcement learning | reasoning behavior | verifiable rewards"),
        ("paper_008", "deepseek_r1", "What is GRPO and why is it useful compared with PPO-style training?", "comparison", "medium", "GRPO | PPO | value model | group relative"),
        ("paper_009", "deepseek_r1", "What reasoning behaviors emerge during the DeepSeek-R1 training process?", "evidence", "medium", "emergent reasoning | reflection | long chain-of-thought"),
        ("paper_010", "deepseek_r1", "Give an interview-style explanation of why DeepSeek-R1 matters.", "interview", "medium", "interview answer | RL | reasoning LLM"),

        # RL reasoning capacity paper
        ("paper_011", "rl_reasoning", "What question does the RL reasoning capacity paper investigate?", "concept_explanation", "easy", "RLVR | reasoning capacity | base model"),
        ("paper_012", "rl_reasoning", "What does the paper conclude about whether RL expands reasoning capacity beyond the base model?", "findings", "medium", "capacity boundary | RLVR | base model"),
        ("paper_013", "rl_reasoning", "How does the paper compare reinforcement learning with distillation?", "comparison", "hard", "reinforcement learning | distillation | reasoning boundary"),
        ("paper_014", "rl_reasoning", "What experimental evidence supports the paper's conclusion about RLVR?", "evidence", "hard", "experiments | RLVR | out-of-distribution"),
        ("paper_015", "rl_reasoning", "How should I explain this RL reasoning capacity paper in an interview?", "interview", "medium", "interview answer | RLVR | reasoning capacity"),

        # Lawyer recommendation system paper
        ("paper_016", "lawyer_system", "What user roles does the lawyer recommendation system include, and what does each role do?", "list", "easy", "user | lawyer | administrator | roles"),
        ("paper_017", "lawyer_system", "How is collaborative filtering used for lawyer recommendation in the system?", "method", "medium", "collaborative filtering | lawyer recommendation | ratings"),
        ("paper_018", "lawyer_system", "What are the main frontend and backend technologies used by the lawyer service mini-program?", "fact_lookup", "medium", "frontend | backend | mini-program | framework"),
        ("paper_019", "lawyer_system", "What functions does the AI legal consultation module provide?", "fact_lookup", "medium", "AI legal consultation | self-service Q&A | lawyer service"),
        ("paper_020", "lawyer_system", "Summarize the overall design value of the lawyer recommendation and service system.", "summary", "medium", "system value | online legal service | recommendation"),

        # Probing difficulty perception
        ("paper_021", "probing_difficulty", "What is the main research question in Probing the Difficulty Perception Mechanism of Large Language Models?", "concept_explanation", "easy", "difficulty perception | probing | LLM internal representations"),
        ("paper_022", "probing_difficulty", "How does the probing paper use linear probes to study question difficulty?", "method", "medium", "linear probe | final token representation | difficulty"),
        ("paper_023", "probing_difficulty", "What does the paper find about attention heads and difficulty perception?", "findings", "medium", "attention heads | activation pattern | difficulty perception"),
        ("paper_024", "probing_difficulty", "What evidence shows that question difficulty is encoded inside LLM representations?", "evidence", "hard", "encoded difficulty | representations | ablation"),
        ("paper_025", "probing_difficulty", "Give an interview-style explanation of the probing difficulty perception paper.", "interview", "medium", "interview answer | probing | difficulty perception"),

        # The LLM Already Knows
        ("paper_026", "llm_knows", "What is the core hypothesis of The LLM Already Knows?", "concept_explanation", "easy", "LLM-perceived question difficulty | hidden representations | hypothesis"),
        ("paper_027", "llm_knows", "How does The LLM Already Knows estimate question difficulty from hidden representations?", "method", "medium", "hidden representations | value function | difficulty estimation"),
        ("paper_028", "llm_knows", "Why is estimating difficulty without generating output tokens useful?", "mechanism", "medium", "no token generation | efficiency | hidden state"),
        ("paper_029", "llm_knows", "What evidence supports the claim that the LLM already knows perceived difficulty?", "evidence", "hard", "evidence | perceived difficulty | hidden states"),
        ("paper_030", "llm_knows", "Compare The LLM Already Knows with the probing difficulty perception paper.", "comparison", "hard", "comparison | hidden representations | linear probing | mechanism"),
    ]

    rows: List[Dict[str, str]] = []
    for row_id, paper_key, query, query_type, difficulty, keywords in questions:
        source = PAPERS[paper_key]
        rows.append({
            "id": row_id,
            "source": source,
            "query": query,
            "query_type": query_type,
            "difficulty": difficulty,
            "expected_keywords": keywords,
            "expected_source": source,
            "expected_card_titles": source,
            "allow_web": "false",
            "notes": "current 6-paper private-Wiki benchmark; no web expected",
        })
    return rows


def _split_pipe(value: str) -> List[str]:
    return [item.strip() for item in str(value or "").split("|") if item.strip()]


def write_csv(path: Path, rows: Iterable[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDS})


def write_expected_cards(path: Path, rows: Iterable[Dict[str, str]]) -> None:
    payload = {
        row["id"]: {
            "expected_source": row.get("expected_source", ""),
            "expected_card_titles": _split_pipe(row.get("expected_card_titles", "")),
            "expected_keywords": _split_pipe(row.get("expected_keywords", "")),
            "allow_web": row.get("allow_web", "false").lower() == "true",
        }
        for row in rows
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    rows = current_papers_30()
    if len(rows) != 30:
        raise ValueError(f"Expected 30 rows, got {len(rows)}")
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate dataset IDs found")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("base_50.csv", "new_difficulty_papers_20.csv"):
        stale_path = args.output_dir / stale
        if stale_path.exists():
            stale_path.unlink()

    write_csv(args.output_dir / "current_papers_30.csv", rows)
    write_csv(args.output_dir / "all_agentic_wiki_eval.csv", rows)
    write_expected_cards(args.output_dir / "expected_cards.json", rows)

    print(f"Wrote {len(rows)} current-paper rows to {args.output_dir}")
    print("Primary dataset: all_agentic_wiki_eval.csv")


if __name__ == "__main__":
    main()
