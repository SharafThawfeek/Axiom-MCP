from app.services.analysis_service import AnalysisService, _clean

fingerprint = AnalysisService._fingerprint


def test_no_dependencies_has_no_fingerprint():
    assert fingerprint(None) is None
    assert fingerprint("") is None
    assert fingerprint("   \n  ") is None


def test_different_versions_of_the_same_package_do_not_collide():
    # The whole point: same traceback, different version, different answer.
    assert fingerprint("pandas==2.1.0") != fingerprint("pandas==1.5.3")


def test_declaring_nothing_is_not_the_same_as_declaring_something():
    assert fingerprint("pandas==2.1.0") != fingerprint(None)


def test_cosmetic_differences_do_not_split_the_cache():
    base = fingerprint("pandas==2.1.0\nrequests==2.31.0")

    assert fingerprint("requests==2.31.0\npandas==2.1.0") == base   # order
    assert fingerprint("pandas == 2.1.0\nrequests==2.31.0") == base  # spacing
    assert fingerprint("PANDAS==2.1.0\nrequests==2.31.0") == base    # casing
    assert fingerprint("pandas==2.1.0\n\n\nrequests==2.31.0\n") == base  # blank lines
    assert fingerprint("# deps\npandas==2.1.0\nrequests==2.31.0") == base  # comments


def test_a_genuinely_extra_package_changes_the_fingerprint():
    assert fingerprint("pandas==2.1.0") != fingerprint("pandas==2.1.0\nnumpy==1.26.0")


def test_fingerprint_fits_the_column():
    # Column is String(64); sha256 hex is exactly 64 chars.
    assert len(fingerprint("pandas==2.1.0")) == 64


def test_clean_strips_embedded_nul_bytes():
    # Postgres text columns reject these outright — a raw pasted CI log is
    # exactly the kind of input that can carry one.
    assert _clean("bad\x00value") == "badvalue"


def test_clean_passes_through_normal_text():
    assert _clean("nothing wrong here") == "nothing wrong here"


def test_clean_handles_none_and_empty():
    assert _clean(None) is None
    assert _clean("") == ""
