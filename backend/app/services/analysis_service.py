import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import run_agent
from app.config import settings
from app.core.logger import logger
from app.models.analysis import Analysis as AnalysisRow
from app.models.incident import OSSIncident
from app.parsers import implicated_library, parse
from app.schemas.analysis import Analysis, AnalysisResponse
from app.schemas.incident import Citation, MatchedIssue


def _clean(text: str | None) -> str | None:
    # Postgres text columns reject NUL bytes outright
    # (CharacterNotInRepertoireError) — log_excerpt is raw user-pasted CI
    # output and can carry one; the same failure mode hit the indexer's
    # load() path on real GitHub issue text. Strip before it can crash the
    # commit on the live /analyze path.
    return text.replace("\x00", "") if text else text


class AnalysisService:
    """Entry point for a single failure analysis.

    Parses the log, checks whether this exact failure was already solved
    recently, and only if not, hands it to the agent loop — the agent
    decides for itself whether/how to retrieve, check versions, etc.
    Persists the result as team-level memory once it's done.
    """

    @staticmethod
    def _fingerprint(dependencies: str | None) -> str | None:
        """Stable hash of a declared dependency set, or None if none was given.

        Normalised so cosmetic differences — ordering, blank lines, comments,
        casing, `==` vs `>=` spacing — don't split the cache. Two callers who
        genuinely declared the same environment should share an answer; two
        who didn't, shouldn't.
        """
        if not dependencies or not dependencies.strip():
            return None

        pins = []
        for line in dependencies.splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if line:
                pins.append(re.sub(r"\s+", "", line))

        if not pins:
            return None

        return hashlib.sha256("\n".join(sorted(pins)).encode()).hexdigest()

    @staticmethod
    async def _find_cached(
        db: AsyncSession,
        failure_signature: str,
        dependencies_fingerprint: str | None,
    ) -> AnalysisRow | None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.CACHE_TTL_DAYS)
        result = await db.execute(
            select(AnalysisRow)
            .where(
                AnalysisRow.failure_signature == failure_signature,
                # Must match exactly, including both being NULL. A caller who
                # declared versions and one who didn't are not interchangeable.
                AnalysisRow.dependencies_fingerprint.is_not_distinct_from(
                    dependencies_fingerprint
                ),
                AnalysisRow.created_at >= cutoff,
            )
            .order_by(AnalysisRow.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _row_to_response(db: AsyncSession, row: AnalysisRow, from_cache: bool) -> AnalysisResponse:
        """Reconstruct the API response shape from a stored row.

        Shared between a cache hit here and GET /analyze/{id} — the row only
        stores incident *ids*, so citation details (url, commit) always need
        re-fetching from oss_incidents either way.
        """
        matched_issues: list[MatchedIssue] = []
        if row.matched_incident_ids:
            incidents = await db.execute(
                select(OSSIncident).where(OSSIncident.id.in_(row.matched_incident_ids))
            )
            for incident in incidents.scalars().all():
                matched_issues.append(
                    MatchedIssue(
                        incident_id=str(incident.id),
                        library=incident.library,
                        title=incident.issue_title,
                        problem_summary=incident.problem_summary,
                        resolution_summary=incident.resolution_summary,
                        # Not recomputed on read — this is a stored record,
                        # not a live ranking against a new query.
                        similarity=0.0,
                        rank_score=0.0,
                        citation=Citation(
                            issue_url=incident.issue_url,
                            issue_title=incident.issue_title,
                            fixing_commit_url=incident.fixing_commit_url,
                        ),
                    )
                )

        return AnalysisResponse(
            id=str(row.id),
            failure=None,  # the parsed failure object itself isn't persisted, only its signature
            analysis=Analysis(
                summary=row.summary,
                root_cause=row.root_cause,
                explanation=row.explanation,
                confidence=row.confidence,
                suspected_library=row.suspected_library,
                next_steps=row.next_steps,
                suggested_patch=row.suggested_patch,
            ),
            matched_issues=matched_issues,
            agent_trace=row.agent_trace,
            from_cache=from_cache,
        )

    @staticmethod
    async def analyse(
        db: AsyncSession,
        log: str,
        dependencies: str | None = None,
        file_context: str | None = None,
    ) -> AnalysisResponse:
        failure = parse(log)
        library_hint = implicated_library(failure) if failure else None
        fingerprint = AnalysisService._fingerprint(dependencies)

        if failure:
            logger.info("Parsed failure: %s", failure.signature)

            cached = await AnalysisService._find_cached(db, failure.signature, fingerprint)
            if cached is not None:
                logger.info(
                    "Cache hit for %s (analysis %s, %s old) — skipping the agent",
                    failure.signature, cached.id,
                    datetime.now(timezone.utc) - cached.created_at,
                )
                response = await AnalysisService._row_to_response(db, cached, from_cache=True)
                # A patch is only ever safe against the exact file content it
                # was generated from. The cached explanation still holds (same
                # underlying failure), but the file may have changed since —
                # never hand a stale patch to something that might auto-apply
                # it. Fresh runs always regenerate their own.
                response.analysis.suggested_patch = None
                return response
        else:
            logger.info("No traceback parsed; agent works from raw log")

        result = await run_agent(
            db=db,
            log=log,
            failure=failure,
            library_hint=library_hint,
            dependencies_text=dependencies,
            file_context=file_context,
        )

        row = AnalysisRow(
            id=uuid.uuid4(),
            log_excerpt=_clean(log[-5000:]),
            failure_signature=failure.signature if failure else None,
            dependencies_fingerprint=fingerprint,
            summary=_clean(result.analysis.summary),
            root_cause=_clean(result.analysis.root_cause),
            explanation=_clean(result.analysis.explanation),
            confidence=result.analysis.confidence,
            suspected_library=_clean(result.analysis.suspected_library),
            next_steps=[_clean(step) for step in result.analysis.next_steps],
            suggested_patch=_clean(result.analysis.suggested_patch),
            matched_incident_ids=[
                uuid.UUID(m.incident_id) for m in result.matched_issues
            ],
            agent_trace=result.agent_trace,
        )
        db.add(row)
        await db.commit()

        result.id = str(row.id)
        result.from_cache = False
        return result
