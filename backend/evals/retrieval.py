"""Scoring for retrieval quality — the only way to know a change to
RetrievalService actually helped, instead of just feeling like it should.

Both metrics are computed from a single ranked result list, so they're pure
and testable without touching the database or an LLM.
"""

from app.schemas.incident import MatchedIssue


def recall_at_k(results: list[MatchedIssue], expected_url: str, k: int) -> bool:
    """Did the expected issue appear anywhere in the top k results?"""
    top_k = results[:k]
    return any(r.citation.issue_url == expected_url for r in top_k)


def reciprocal_rank(results: list[MatchedIssue], expected_url: str) -> float:
    """1/rank of the expected issue (1-indexed), or 0 if it's not in the results at all."""
    for i, r in enumerate(results):
        if r.citation.issue_url == expected_url:
            return 1.0 / (i + 1)
    return 0.0


def aggregate(
    per_case_ranks: list[float],
    per_case_hits_at: dict[int, list[bool]],
) -> dict:
    """Mean reciprocal rank plus recall@k for each k that was measured."""
    n = len(per_case_ranks)
    if n == 0:
        return {"mrr": 0.0, "recall_at": {}, "n": 0}

    return {
        "mrr": round(sum(per_case_ranks) / n, 4),
        "recall_at": {
            k: round(sum(hits) / n, 4) for k, hits in per_case_hits_at.items()
        },
        "n": n,
    }
