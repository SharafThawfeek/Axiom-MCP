"""Reading and writing failure memory.

One implementation for both backends. The connection URL decides whether
this is a local SQLite file or the hosted Postgres instance; nothing below
branches on dialect, which is the whole reason the local install needs no
infrastructure.

Recall is two-tier by design:

  1. Exact match on (project_id, signature). An index hit, no embedding, no
     model load. Most repeat CI failures are byte-identical once the parser
     has normalised addresses, quoted strings and line numbers out of the
     message, so this is the common case and it costs about a millisecond.
  2. Only on a miss: embed the query and score it against the project's
     stored vectors. This is the path that costs a model load, so it is
     never taken when tier 1 already answered.

That ordering is the single biggest efficiency decision in the product. The
alternative — always embedding — would make the common case ~3 seconds cold
and hundreds of times slower warm, for an answer tier 1 already had.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from axiom_debug.memory import vectors
from axiom_debug.memory.models import FailureMemory, MemoryBase

# Scoring loads only (id, vector) pairs, never whole rows, so a large project
# costs bytes rather than megabytes here. Full rows are fetched for the
# handful that actually win. The cap is a backstop against a pathological
# project, not an expected limit.
MAX_SCORED_ROWS = 20_000

# Below this cosine similarity two signatures are different failures that
# happen to share vocabulary ("TypeError", "None"). Tuned to the same scale
# as RETRIEVAL_MAX_DISTANCE in config.py, which measured ~0.24 for the same
# exception on a different object and ~0.5 for unrelated infrastructure.
MIN_SIMILARITY = 0.75


@dataclass(frozen=True)
class Recalled:
    """A memory row plus why it was returned."""

    memory: FailureMemory
    similarity: float
    # True when this came from tier 1. An exact signature match is a
    # materially stronger claim than a near neighbour, and the caller should
    # be able to say so rather than presenting both as "similar".
    exact: bool


class MemoryStore:

    def __init__(self, url: str, echo: bool = False):
        self._engine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialise(self) -> None:
        """Create the memory table if it doesn't exist.

        Safe to call on every start. Only `failure_memories` is in this
        metadata — see models.py for why memory has its own declarative base
        — so this never attempts to create the Postgres-only tables.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(MemoryBase.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def _exact(
        self, session: AsyncSession, project_id: str, signature: str
    ) -> FailureMemory | None:
        result = await session.execute(
            select(FailureMemory).where(
                FailureMemory.project_id == project_id,
                FailureMemory.signature == signature,
            )
        )
        return result.scalar_one_or_none()

    async def recall(
        self,
        project_id: str,
        signature: str | None = None,
        query_text: str | None = None,
        limit: int = 5,
    ) -> list[Recalled]:
        """Find past failures in this project resembling the one described.

        `signature` drives tier 1; `query_text` drives tier 2 and falls back
        to the signature when not given separately.
        """
        async with self._session() as session:
            hits: list[Recalled] = []
            seen: set[str] = set()

            if signature:
                exact = await self._exact(session, project_id, signature)
                if exact is not None:
                    hits.append(Recalled(memory=exact, similarity=1.0, exact=True))
                    seen.add(exact.id)
                    if len(hits) >= limit:
                        return hits

            text = query_text or signature
            if not text:
                return hits

            # Ids and vectors only — see MAX_SCORED_ROWS.
            rows = (
                await session.execute(
                    select(FailureMemory.id, FailureMemory.signature_vec)
                    .where(
                        FailureMemory.project_id == project_id,
                        FailureMemory.signature_vec.is_not(None),
                    )
                    .limit(MAX_SCORED_ROWS)
                )
            ).all()

            candidates = [(row_id, blob) for row_id, blob in rows if row_id not in seen]
            if not candidates:
                return hits

            # Imported lazily: fastembed loads ~130MB of ONNX weights on first
            # use, and tier 1 answering means we never pay for it at all.
            from axiom_debug.services.embedding_service import EmbeddingService

            query_vec = await EmbeddingService.aembed_one(text)
            scores = vectors.cosine_scores(query_vec, [blob for _, blob in candidates])

            ranked = sorted(
                (
                    (score, row_id)
                    # strict: one score per candidate, always. A length
                    # mismatch here would silently drop or misalign results,
                    # attaching one row's score to another row's id.
                    for score, (row_id, _) in zip(scores, candidates, strict=True)
                    if score >= MIN_SIMILARITY
                ),
                reverse=True,
            )[: max(0, limit - len(hits))]

            if not ranked:
                return hits

            wanted = {row_id: float(score) for score, row_id in ranked}
            full = (
                await session.execute(
                    select(FailureMemory).where(FailureMemory.id.in_(wanted))
                )
            ).scalars().all()

            hits.extend(
                sorted(
                    (
                        Recalled(memory=m, similarity=wanted[m.id], exact=False)
                        for m in full
                    ),
                    key=lambda r: r.similarity,
                    reverse=True,
                )
            )
            return hits

    async def record(
        self,
        project_id: str,
        signature: str,
        resolution: str,
        language: str = "python",
        exception_type: str | None = None,
        description: str | None = None,
        resolved_by: str | None = None,
        source: str = "human",
    ) -> FailureMemory:
        """Store how a failure was resolved, or update what's already there.

        Upsert rather than insert: a recurring signature increments
        `occurrences` and refreshes the resolution. Without that, a flaky
        test firing repeatedly would fill recall with duplicates of itself
        and crowd out everything else.
        """
        from axiom_debug.services.embedding_service import EmbeddingService

        async with self._session() as session:
            existing = await self._exact(session, project_id, signature)

            if existing is not None:
                existing.resolution = resolution
                existing.occurrences += 1
                if exception_type:
                    existing.exception_type = exception_type
                if description:
                    existing.description = description
                if resolved_by:
                    existing.resolved_by = resolved_by
                existing.source = source
                await session.commit()
                return existing

            vector = await EmbeddingService.aembed_one(signature)
            row = FailureMemory(
                project_id=project_id,
                signature=signature,
                language=language,
                exception_type=exception_type,
                description=description,
                resolution=resolution,
                resolved_by=resolved_by,
                source=source,
                signature_vec=vectors.encode(vector),
            )
            session.add(row)
            await session.commit()
            return row

    async def count(self, project_id: str) -> int:
        """How many distinct failures this project has recorded."""
        async with self._session() as session:
            rows = await session.execute(
                select(FailureMemory.id).where(FailureMemory.project_id == project_id)
            )
            return len(rows.all())
