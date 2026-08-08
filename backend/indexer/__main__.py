"""Offline batch indexer — run once, before launch, not part of the served app.

    python -m indexer                  # crawl every library in libraries.py
    python -m indexer --library pandas # crawl just one, for testing

Pulls closed issues -> extracts (problem, resolution) pairs with a cheap
model -> embeds -> loads into pgvector. Needs GITHUB_TOKEN and
GROQ_API_KEY in backend/.env.
"""

import argparse
import asyncio

from app.core.logger import logger
from app.database import SessionLocal
from indexer.extract import extract
from indexer.github import fetch_closed_issues
from indexer.libraries import TARGET_LIBRARIES
from indexer.load import load

# Groq's free tier for the small model is 8000 tokens/minute; a single
# extraction call runs 1500-3500 tokens. High concurrency here doesn't
# raise throughput — the TPM budget is the real ceiling — it just means
# more calls piling up waiting on the same retry-after window at once.
# Confirmed live: concurrency=5 produced a 429 on every single request.
EXTRACTION_CONCURRENCY = 2
MAX_ISSUES_PER_LIBRARY = 300

# Persist as extractions complete, not only after the whole library finishes.
# Confirmed live: under real rate-limit pressure a single library's crawl can
# take hours, and asyncio.gather-then-load held every completed extraction
# only in memory until the very last one finished — a kill, crash, or
# rate-limit-induced hang at any point lost all of it, hours included, with
# nothing ever reaching the DB. A small chunk size means progress is safe
# within minutes, not hours.
LOAD_CHUNK_SIZE = 10


async def _load_chunk(pypi_name: str, chunk: list) -> int:
    if not chunk:
        return 0
    try:
        async with SessionLocal() as db:
            return await load(db, pypi_name, chunk)
    except Exception as exc:
        # load() commits per batch of BATCH_SIZE within a chunk, so any batch
        # that already committed is safe either way — this only stops one bad
        # chunk (a row load.py's sanitization didn't anticipate, a DB blip)
        # from taking down the rest of the crawl. Confirmed live: an uncaught
        # error here previously killed the whole multi-library run, not just
        # the chunk that failed.
        logger.error("Failed to load a chunk of extracted incidents for %s: %s", pypi_name, exc)
        return 0


async def index_library(pypi_name: str, repo: str) -> int:
    logger.info("=== %s (%s) ===", pypi_name, repo)

    try:
        owner, name = repo.split("/")
        raw_issues = await fetch_closed_issues(owner, name, max_issues=MAX_ISSUES_PER_LIBRARY)
    except Exception as exc:
        logger.error("Failed to fetch issues for %s: %s", repo, exc)
        return 0

    if not raw_issues:
        return 0

    semaphore = asyncio.Semaphore(EXTRACTION_CONCURRENCY)

    async def bounded(raw):
        async with semaphore:
            return await extract(raw)

    total_inserted = 0
    useful_count = 0
    buffer: list = []

    for coro in asyncio.as_completed([bounded(raw) for raw in raw_issues]):
        result = await coro
        if result is None:
            continue

        useful_count += 1
        buffer.append(result)

        if len(buffer) >= LOAD_CHUNK_SIZE:
            total_inserted += await _load_chunk(pypi_name, buffer)
            buffer = []

    total_inserted += await _load_chunk(pypi_name, buffer)

    logger.info(
        "%s: %d/%d issues were genuine bugs with a real fix, %d newly indexed",
        pypi_name, useful_count, len(raw_issues), total_inserted,
    )

    return total_inserted


async def main(only_library: str | None) -> None:
    targets = TARGET_LIBRARIES
    if only_library:
        targets = [t for t in TARGET_LIBRARIES if t[0] == only_library]
        if not targets:
            logger.error("Unknown library: %s", only_library)
            return

    total = 0
    for pypi_name, repo in targets:
        total += await index_library(pypi_name, repo)

    logger.info("Done. %d new incidents indexed across %d libraries.", total, len(targets))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Axiom Debug offline indexer")
    parser.add_argument("--library", help="Index only this one library (by pypi name)")
    args = parser.parse_args()

    asyncio.run(main(args.library))
