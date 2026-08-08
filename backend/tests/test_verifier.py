from app.agent.verifier import filter_valid_citations
from app.schemas.incident import Citation, MatchedIssue


def _issue(incident_id: str) -> MatchedIssue:
    return MatchedIssue(
        incident_id=incident_id,
        library="pandas",
        title="DataFrame.append is deprecated",
        problem_summary="...",
        resolution_summary="Use pd.concat instead.",
        similarity=0.9,
        citation=Citation(issue_url="https://x", issue_title="x"),
    )


def test_keeps_citations_that_were_actually_retrieved():
    seen = {"abc": _issue("abc")}

    result = filter_valid_citations(["abc"], seen)

    assert len(result) == 1
    assert result[0].incident_id == "abc"


def test_drops_a_fabricated_citation_the_agent_never_retrieved():
    seen = {"abc": _issue("abc")}

    # "xyz" was never looked up via any tool — must not survive.
    result = filter_valid_citations(["abc", "xyz"], seen)

    assert len(result) == 1
    assert result[0].incident_id == "abc"


def test_no_citations_cited_is_not_an_error():
    assert filter_valid_citations([], {}) == []


def test_all_citations_fabricated_returns_empty_not_a_crash():
    assert filter_valid_citations(["made-up-1", "made-up-2"], {}) == []
