"""Two resilience properties of the indexer's per-library orchestration,
both found via real failures, not hypothesized:

1. A load() failure must not take down the whole multi-library crawl.
   Confirmed live: an uncaught error in load() (a NUL byte in extracted
   text) propagated all the way out of a real `python -m indexer` run and
   killed the process — losing every library after the one that failed.

2. Extracted incidents must be persisted incrementally, not held in memory
   until the whole library finishes. Confirmed live: under real rate-limit
   pressure a single library's crawl ran for 5 hours with the old
   gather-then-load design and had zero rows in the database the entire
   time — a kill or crash at any point would have lost everything.
"""
import pytest

from indexer import __main__ as indexer_main


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_load_failure_returns_zero_instead_of_propagating(monkeypatch):
    async def fake_fetch(owner, name, max_issues):
        return ["one-raw-issue"]

    async def fake_extract(raw):
        return "one-extracted-incident"

    async def fake_load(db, pypi_name, chunk, language="python"):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(indexer_main, "fetch_closed_issues", fake_fetch)
    monkeypatch.setattr(indexer_main, "extract", fake_extract)
    monkeypatch.setattr(indexer_main, "load", fake_load)
    monkeypatch.setattr(indexer_main, "SessionLocal", lambda: _FakeSession())

    result = await indexer_main.index_library("pandas", "pandas-dev/pandas")

    assert result == 0


@pytest.mark.asyncio
async def test_extractions_are_persisted_in_chunks_not_all_at_the_end(monkeypatch):
    # 25 issues with LOAD_CHUNK_SIZE=10 should flush at 10, at 20, and a
    # final partial chunk of 5 — three separate load() calls, proving
    # progress is saved well before the whole library finishes.
    raw_issues = list(range(25))
    load_calls: list[int] = []

    async def fake_fetch(owner, name, max_issues):
        return raw_issues

    async def fake_extract(raw):
        return f"incident-{raw}"

    async def fake_load(db, pypi_name, chunk, language="python"):
        load_calls.append(len(chunk))
        return len(chunk)

    monkeypatch.setattr(indexer_main, "fetch_closed_issues", fake_fetch)
    monkeypatch.setattr(indexer_main, "extract", fake_extract)
    monkeypatch.setattr(indexer_main, "load", fake_load)
    monkeypatch.setattr(indexer_main, "SessionLocal", lambda: _FakeSession())

    result = await indexer_main.index_library("pandas", "pandas-dev/pandas")

    assert len(load_calls) == 3
    assert sorted(load_calls) == [5, 10, 10]
    assert result == 25


@pytest.mark.asyncio
async def test_main_reaches_every_library_when_one_returns_zero(monkeypatch):
    # index_library's contract post-fix is "never raises, return 0 on
    # failure" — this confirms main()'s plain for-loop actually relies on
    # that contract to reach every library, not just the ones before a
    # failure.
    calls = []

    async def fake_index_library(pypi_name, repo, language="python"):
        calls.append(pypi_name)
        return 0 if pypi_name == "pandas" else 1

    monkeypatch.setattr(indexer_main, "index_library", fake_index_library)
    monkeypatch.setattr(
        indexer_main,
        "TARGET_LIBRARIES",
        [("pandas", "a/a", "python"), ("numpy", "b/b", "python")],
    )

    await indexer_main.main(only_library=None)

    assert calls == ["pandas", "numpy"]
