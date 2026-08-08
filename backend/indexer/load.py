"""Embeds extracted incidents and loads them into the index.

Skips issues already indexed (by issue_url) so re-running the indexer after
adding a library, or after a crawl was interrupted, doesn't duplicate rows.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger import logger
from app.models.incident import OSSIncident
from app.services.embedding_service import EmbeddingService
from indexer.extract import ExtractedIncident

BATCH_SIZE = 32  # embedding batch — bounds peak memory, not a correctness constraint


def _clean(text: str | None, max_length: int | None = None) -> str | None:
    # Postgres text columns reject NUL bytes outright (CharacterNotInRepertoireError).
    # Scraped issue bodies/comments occasionally carry one — strip before it can
    # poison a whole insert batch. max_length guards error_signature, the one
    # extracted field with no length bound anywhere upstream (unlike
    # issue_title/issue_url, which inherit GitHub's own limits) — its column
    # is String(500), and an oversized value would crash the batch the same
    # way a NUL byte does.
    if not text:
        return text
    cleaned = text.replace("\x00", "")
    if max_length is not None and len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


async def _already_indexed(db: AsyncSession, urls: list[str]) -> set[str]:
    if not urls:
        return set()
    result = await db.execute(
        select(OSSIncident.issue_url).where(OSSIncident.issue_url.in_(urls))
    )
    return {row[0] for row in result.all()}


async def load(db: AsyncSession, library: str, incidents: list[ExtractedIncident]) -> int:
    """Embed and insert `incidents` for `library`. Returns how many were new."""
    if not incidents:
        return 0

    urls = [inc.raw.url for inc in incidents]
    existing = await _already_indexed(db, urls)
    fresh = [inc for inc in incidents if inc.raw.url not in existing]

    if not fresh:
        logger.info("%s: all %d incidents already indexed", library, len(incidents))
        return 0

    inserted = 0
    for i in range(0, len(fresh), BATCH_SIZE):
        batch = fresh[i : i + BATCH_SIZE]
        vectors = EmbeddingService.embed_many([inc.problem_summary for inc in batch])

        for incident, vector in zip(batch, vectors):
            db.add(
                OSSIncident(
                    library=library,
                    issue_number=incident.raw.number,
                    issue_url=incident.raw.url,
                    issue_title=_clean(incident.raw.title),
                    fixing_commit_url=incident.raw.closer_url,
                    problem_summary=_clean(incident.problem_summary),
                    resolution_summary=_clean(incident.resolution_summary),
                    error_signature=_clean(incident.error_signature, max_length=500),
                    embedding=vector,
                )
            )
            inserted += 1

        await db.commit()
        logger.info("%s: indexed %d/%d", library, inserted, len(fresh))

    return inserted
