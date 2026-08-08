from indexer.load import _clean


def test_strips_embedded_nul_bytes():
    assert _clean("bad\x00value") == "badvalue"


def test_passes_through_clean_text_unchanged():
    assert _clean("nothing wrong here") == "nothing wrong here"


def test_handles_none():
    assert _clean(None) is None


def test_truncates_to_max_length():
    assert _clean("x" * 600, max_length=500) == "x" * 500


def test_leaves_short_text_alone_even_with_max_length():
    assert _clean("short", max_length=500) == "short"


def test_strips_nul_bytes_before_truncating():
    # 600 'a's interleaved with NULs — post-strip length (600) still exceeds
    # max_length, proving the strip happens before the truncation check.
    assert _clean("a\x00" * 600, max_length=500) == "a" * 500
