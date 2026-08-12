from pydantic import BaseModel, ConfigDict


class Citation(BaseModel):
    """A link back to the original evidence — the whole point of grounding."""

    issue_url: str
    issue_title: str
    fixing_commit_url: str | None = None


class MatchedIssue(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    incident_id: str
    library: str
    title: str
    problem_summary: str
    resolution_summary: str
    citation: Citation

    # How semantically close this is to the query: 1 - cosine distance.
    # 1.0 identical, ~0.95 the same failure reworded, ~0.5 unrelated.
    # This is the number to judge match quality by.
    similarity: float = 0.0

    # Reciprocal Rank Fusion score across the dense and sparse passes.
    # Orders results; does NOT indicate quality — a result topping both
    # lists scores the same whether it's a perfect match or the least-bad
    # row available. See retrieval_service for why both numbers exist.
    rank_score: float = 0.0
