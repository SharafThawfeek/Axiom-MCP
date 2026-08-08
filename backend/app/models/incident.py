import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, Integer, String, Text
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

    # Covers every text field deliberately. resolution_summary: the sparse
    # half of hybrid search exists to catch exact technical tokens the dense
    # pass smooths over, and distinctive identifiers (an internal API like
    # `_from_sequence`) very often appear only in how the bug was FIXED —
    # confirmed live, excluding it made those queries match nothing at all.
    # error_signature: an exception type is precisely the kind of exact token
    # keyword search beats embeddings on, and including it here is what makes
    # the column useful at all rather than write-only.
    search_text: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(issue_title, '') || ' ' || "
            "coalesce(problem_summary, '') || ' ' || coalesce(resolution_summary, '') "
            "|| ' ' || coalesce(error_signature, ''))",
            persisted=True,
        ),
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )
