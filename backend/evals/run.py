"""Run the retrieval eval set against the live index.

    python -m evals.run
    python -m evals.run --k 1 3 5

Each case in cases.jsonl is a query and the issue URL that should match it.
This is the only honest way to know whether a retrieval change (a new
fusion weight, a different embedding model, a reranking step) actually
helped — "it looks better" is not a metric.
"""

import argparse
import asyncio
import json
from pathlib import Path

from axiom_debug.database import SessionLocal
from axiom_debug.services.retrieval_service import RetrievalService

from evals.retrieval import aggregate, recall_at_k, reciprocal_rank

CASES_PATH = Path(__file__).parent / "cases.jsonl"


def load_cases() -> list[dict]:
    cases = []
    with open(CASES_PATH, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


async def run(ks: list[int]) -> None:
    cases = load_cases()
    if not cases:
        print(f"No cases in {CASES_PATH}")
        return

    ranks: list[float] = []
    hits_at: dict[int, list[bool]] = {k: [] for k in ks}

    async with SessionLocal() as db:
        for case in cases:
            results = await RetrievalService.search(
                db, case["query"], library=case.get("library"), top_k=max(ks)
            )

            rank = reciprocal_rank(results, case["expected_issue_url"])
            ranks.append(rank)

            for k in ks:
                hits_at[k].append(recall_at_k(results, case["expected_issue_url"], k))

            status = "HIT " if rank > 0 else "MISS"
            print(f"[{status}] rr={rank:.3f}  {case['query'][:70]}")

    summary = aggregate(ranks, hits_at)
    print()
    print(f"n={summary['n']}  MRR={summary['mrr']}")
    for k, r in summary["recall_at"].items():
        print(f"  recall@{k} = {r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the retrieval eval set")
    parser.add_argument("--k", type=int, nargs="+", default=[1, 3, 5])
    args = parser.parse_args()

    asyncio.run(run(args.k))
