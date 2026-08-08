"""Finds solved OSS issues matching a failure.

Pure vector search blurs exact-match signals — an exception type or a
library name is exactly the kind of token dense embeddings can smear across
neighbours. So this runs two searches and fuses them:

  - dense:  pgvector cosine similarity over `embedding`
  - sparse: Postgres full-text search over the generated `search_text` column

Reciprocal Rank Fusion (RRF) combines the two rankings without needing the
scores to be on comparable scales, which cosine distance and ts_rank are not.

Two things the caller needs to understand about the numbers coming back:

`rank_score` is the RRF value. It encodes *rank position across the two
lists*, not match quality — a result that tops both lists scores ~0.033
whether it's a perfect match or the least-bad row in an index full of
unrelated issues. It orders results; it cannot judge them.

`similarity` is 1 - cosine distance against the query, so it does judge
them: 1.0 is identical, and on real error text ~0.95 is the same failure
reworded while ~0.5 is unrelated infrastructure. That's the number to
threshold on, and the number the agent is shown.
"""


from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.incident import OSSIncident
from app.schemas.incident import Citation, MatchedIssue
from app.services.embedding_service import EmbeddingService

RRF_K = 60  # standard RRF damping constant
DENSE_LIMIT = 20
SPARSE_LIMIT = 20


class RetrievalService:

    @staticmethod
    async def _dense_search(
        db: AsyncSession, query_vector: list[float], library: str | None,
        language: str | None = None,
    ) -> list[tuple[OSSIncident, float]]:
        distance = OSSIncident.embedding.cosine_distance(query_vector)

        stmt = select(OSSIncident, distance.label("distance"))
        if library:
            stmt = stmt.where(OSSIncident.library == library)
        if language:
            stmt = stmt.where(OSSIncident.language == language)
        stmt = stmt.order_by(distance).limit(DENSE_LIMIT)

        result = await db.execute(stmt)
        return [(row[0], float(row[1])) for row in result.all()]

    @staticmethod
    async def _sparse_search(
        db: AsyncSession, query_text: str, query_vector: list[float], library: str | None,
        language: str | None = None,
    ) -> list[tuple[OSSIncident, float]]:
        """Keyword search, but still reporting cosine distance.

        A row can rank well on keywords and still be semantically wrong, so
        the distance is computed here too — otherwise sparse-only hits would
        bypass the quality threshold entirely.
        """
        tsquery = func.plainto_tsquery("english", query_text)
        distance = OSSIncident.embedding.cosine_distance(query_vector)

        stmt = select(OSSIncident, distance.label("distance")).where(
            OSSIncident.search_text.bool_op("@@")(tsquery)
        )
        if library:
            stmt = stmt.where(OSSIncident.library == library)
        if language:
            stmt = stmt.where(OSSIncident.language == language)
        stmt = stmt.order_by(func.ts_rank(OSSIncident.search_text, tsquery).desc())
        stmt = stmt.limit(SPARSE_LIMIT)

        result = await db.execute(stmt)
        return [(row[0], float(row[1])) for row in result.all()]

    @staticmethod
    def _fuse(
        dense: list[tuple[OSSIncident, float]],
        sparse: list[tuple[OSSIncident, float]],
    ) -> list[tuple[OSSIncident, float, float]]:
        """Returns (incident, rrf_score, distance), best rank first."""
        scores: dict = {}
        rows: dict = {}
        distances: dict = {}

        for source in (dense, sparse):
            for rank, (incident, distance) in enumerate(source):
                scores[incident.id] = scores.get(incident.id, 0.0) + 1.0 / (RRF_K + rank + 1)
                rows[incident.id] = incident
                distances[incident.id] = distance

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [
            (rows[incident_id], score, distances[incident_id])
            for incident_id, score in ranked
        ]

    @staticmethod
    async def search(
        db: AsyncSession,
        query_text: str,
        library: str | None = None,
        top_k: int = 5,
        language: str | None = None,
    ) -> list[MatchedIssue]:
        query_vector = await EmbeddingService.aembed_one(query_text)

        # Sequential on purpose: both share one AsyncSession, which is not
        # safe for concurrent use. Do not wrap these in asyncio.gather.
        dense = await RetrievalService._dense_search(db, query_vector, library, language)
        sparse = await RetrievalService._sparse_search(
            db, query_text, query_vector, library, language
        )

        fused = RetrievalService._fuse(dense, sparse)

        # Drop anything semantically unrelated before it can be cited. Without
        # this, an unmatched query still returns the least-bad rows in the
        # index and they look citable.
        kept = [
            (incident, score, distance)
            for incident, score, distance in fused
            if distance <= settings.RETRIEVAL_MAX_DISTANCE
        ][:top_k]

        return [
            MatchedIssue(
                incident_id=str(incident.id),
                library=incident.library,
                title=incident.issue_title,
                problem_summary=incident.problem_summary,
                resolution_summary=incident.resolution_summary,
                rank_score=round(score, 4),
                similarity=round(1.0 - distance, 4),
                citation=Citation(
                    issue_url=incident.issue_url,
                    issue_title=incident.issue_title,
                    fixing_commit_url=incident.fixing_commit_url,
                ),
            )
            for incident, score, distance in kept
        ]
