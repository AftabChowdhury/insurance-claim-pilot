"""Compare retrieval quality across search modes: keyword vs vector vs hybrid vs hybrid + semantic ranker.

Usage: uv run python -m eval.retrieval_eval [--k 5]

Metrics per mode:
  hit@k  – share of questions where at least one expected clause is in the top k
  recall@k – share of expected clauses found in the top k
  MRR    – mean reciprocal rank of the first expected clause
Gives you a before/after table for the demo slides.
"""
import argparse
import json
from pathlib import Path

from core.search import search_policy

MODES = ["keyword", "vector", "hybrid", "hybrid_semantic"]


def rank_key(mode: str):
    return (lambda h: h.get("reranker_score") or 0) if mode == "hybrid_semantic" else (lambda h: h.get("score") or 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    tests = json.loads((Path(__file__).parent / "testset.json").read_text(encoding="utf-8"))

    print(f"{len(tests)} questions, k={args.k}\n")
    print(f"{'mode':<17}{'hit@k':>8}{'recall@k':>10}{'MRR':>8}")
    for mode in MODES:
        hits = recall = mrr = 0.0
        for t in tests:
            results = sorted(search_policy(t["question"], t.get("policy_type"), mode, top=50),
                             key=rank_key(mode), reverse=True)[:args.k]
            ids = [r["clause_id"] for r in results]
            expected = set(t["expected_clauses"])
            found = expected & set(ids)
            hits += bool(found)
            recall += len(found) / len(expected)
            first = next((i for i, c in enumerate(ids) if c in expected), None)
            mrr += 1 / (first + 1) if first is not None else 0
        n = len(tests)
        print(f"{mode:<17}{hits / n:>8.2f}{recall / n:>10.2f}{mrr / n:>8.2f}")


if __name__ == "__main__":
    main()
