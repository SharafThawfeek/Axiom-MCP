"""Deliberately broken. Proves the CI -> Axiom Debug loop end-to-end on a
real PR against a real, deployed backend. Delete this file once verified.

Second attempt: re-testing after fixing report-failure's log-fetch step.
"""


def test_demo_deliberate_failure():
    data = {"count": 3}
    assert data.count == 3
