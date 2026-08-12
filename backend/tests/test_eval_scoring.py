from axiom_debug.schemas.incident import Citation, MatchedIssue

from evals.retrieval import aggregate, recall_at_k, reciprocal_rank


def _result(url: str) -> MatchedIssue:
    return MatchedIssue(
        incident_id="x", library="pandas", title="t",
        problem_summary="p", resolution_summary="r", similarity=0.5,
        citation=Citation(issue_url=url, issue_title="t"),
    )


def test_recall_at_k_true_when_expected_is_within_top_k():
    results = [_result("a"), _result("b"), _result("expected")]
    assert recall_at_k(results, "expected", k=3) is True
    assert recall_at_k(results, "expected", k=2) is False


def test_reciprocal_rank_scores_first_position_as_one():
    results = [_result("expected"), _result("b")]
    assert reciprocal_rank(results, "expected") == 1.0


def test_reciprocal_rank_scores_third_position_as_one_third():
    results = [_result("a"), _result("b"), _result("expected")]
    assert reciprocal_rank(results, "expected") == 1 / 3


def test_reciprocal_rank_zero_when_absent():
    results = [_result("a"), _result("b")]
    assert reciprocal_rank(results, "expected") == 0.0


def test_aggregate_computes_mrr_and_recall():
    ranks = [1.0, 0.5, 0.0]
    hits = {1: [True, False, False], 3: [True, True, False]}

    result = aggregate(ranks, hits)

    assert result["mrr"] == 0.5
    assert result["recall_at"][1] == round(1 / 3, 4)
    assert result["recall_at"][3] == round(2 / 3, 4)
    assert result["n"] == 3


def test_aggregate_empty_is_zero_not_a_crash():
    assert aggregate([], {})["n"] == 0
