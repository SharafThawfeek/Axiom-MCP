from axiom_debug.agent.tools import SEARCH_SUMMARY_CHARS, _truncate


def test_short_text_is_unchanged():
    assert _truncate("a short summary") == "a short summary"


def test_text_at_exactly_the_limit_is_unchanged():
    text = "a" * SEARCH_SUMMARY_CHARS
    assert _truncate(text) == text


def test_long_text_gets_cut_and_marked():
    text = "a" * (SEARCH_SUMMARY_CHARS + 100)

    result = _truncate(text)

    assert result.endswith("…")
    assert len(result) == SEARCH_SUMMARY_CHARS + 1  # + the ellipsis marker
