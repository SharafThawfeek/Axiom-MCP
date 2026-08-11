"""Deliberately broken. Proves the CI -> Axiom Debug loop end-to-end on a
real PR against a real, deployed backend. Delete this file once verified.

Third attempt: re-testing after fixing the log-size/timeout issues that
made report-failure fail to post a comment (400 from an oversized
request, then a client-side timeout).
"""


def test_demo_deliberate_failure():
    data = {"count": 3}
    assert data.count == 3
