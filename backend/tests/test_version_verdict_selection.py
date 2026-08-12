"""The response carries one version_verdict, but the agent may legitimately
check several packages. Picking the wrong one is quietly misleading — a
reader sees a verdict about numpy under an analysis that blames pandas — so
the choice needs to be deliberate rather than "whatever came last".
"""
from axiom_debug.agent.loop import _pick_version_verdict
from axiom_debug.schemas.analysis import VersionVerdict

PANDAS = VersionVerdict(package="pandas", installed_version="1.5.3", verdict="behind")
NUMPY = VersionVerdict(package="numpy", installed_version="1.24.0", verdict="latest")


def test_no_verdicts_gives_none():
    assert _pick_version_verdict([], "pandas") is None


def test_prefers_the_verdict_for_the_blamed_library():
    assert _pick_version_verdict([NUMPY, PANDAS], "pandas") is PANDAS


def test_falls_back_to_first_when_blamed_library_was_not_checked():
    # Better to report a real verdict the agent actually gathered than to
    # silently drop the information because it doesn't match.
    assert _pick_version_verdict([NUMPY], "pandas") is NUMPY


def test_falls_back_to_first_when_no_library_was_blamed():
    assert _pick_version_verdict([NUMPY, PANDAS], None) is NUMPY


def test_library_matching_ignores_case_and_surrounding_whitespace():
    assert _pick_version_verdict([NUMPY, PANDAS], "  PANDAS ") is PANDAS
