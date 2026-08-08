import uuid

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class Analysis(Base):
    """One completed analysis — the team-level memory the doc talks about.

    Future analyses can be matched against this table the same way they're
    matched against oss_incidents, so the system gets sharper on a team's
    own recurring failures, not just the public index.
    """

    __tablename__ = "analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    # Truncated for storage; the full log isn't kept.
    log_excerpt: Mapped[str] = mapped_column(Text)
    failure_signature: Mapped[str | None] = mapped_column(String(500), index=True)

    # Hash of the caller's declared dependencies. The same traceback can have
    # a different correct answer on a different version (a method deprecated
    # in one release and removed in the next), so the repeat-failure cache
    # keys on signature AND this — otherwise one caller's answer gets served
    # to another running different versions.
    dependencies_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)

    summary: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(10))
    suspected_library: Mapped[str | None] = mapped_column(String(100))
    next_steps: Mapped[list[str]] = mapped_column(ARRAY(String))

    # A unified diff, only ever populated when the request included real
    # file_context — never fabricated against a file the agent hasn't seen.
    # Served on a direct GET /analyze/{id} lookup; deliberately NOT served on
    # a cache hit (see AnalysisService.analyse) since a patch is only safe
    # against the exact file content it was generated from.
    suggested_patch: Mapped[str | None] = mapped_column(Text)

    matched_incident_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        default=list,
    )

    # Raw tool-call trace from the agent loop — useful for debugging why it
    # answered the way it did, without re-running the whole thing.
    agent_trace: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )
