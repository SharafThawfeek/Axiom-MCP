import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import String, Text, Integer, DateTime, Computed
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.config import settings
from app.models.base import Base


class OSSIncident(Base):
    """One solved open-source issue, pre-indexed by the offline indexer.

    `search_text` is a generated column, not populated in Python — Postgres
    derives it from title + problem_summary on every insert/update, so
    keyword search never drifts out of sync with the text it indexes.
    """

    __tablename__ = "oss_incidents"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    library: Mapped[str] = mapped_column(String(100), index=True)

    issue_number: Mapped[int] = mapped_column(Integer)
    issue_url: Mapped[str] = mapped_column(String(500), unique=True)
    issue_title: Mapped[str] = mapped_column(String(500))

    fixing_commit_url: Mapped[str | None] = mapped_column(String(500))

    # What the reporter described, and how it was actually resolved —
    # both extracted from the issue thread by the indexer.
    problem_summary: Mapped[str] = mapped_column(Text)
    resolution_summary: Mapped[str] = mapped_column(Text)

    # The exception signature, if this issue was clearly about one
    # (e.g. "AttributeError: ... @ append"). Null for non-exception issues.
    error_signature: Mapped[str | None] = mapped_column(String(500), index=True)

    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.EMBEDDING_DIM)
    )

    # Includes resolution_summary deliberately: the sparse half of hybrid
    # search exists to catch exact technical tokens the dense pass smooths
    # over, and distinctive identifiers (a method name, an internal API like
    # `_from_sequence`) very often appear only in how the bug was FIXED, not
    # in how it was reported. Confirmed live: with resolution text excluded,
    # keyword-searching real fix identifiers matched nothing at all.
    search_text: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(issue_title, '') || ' ' || "
            "coalesce(problem_summary, '') || ' ' || coalesce(resolution_summary, ''))",
            persisted=True,
        ),
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )
