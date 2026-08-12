"""The failure-memory schema.

Separate from `axiom_debug.models.base.Base` on purpose. That metadata holds
`oss_incidents` and `analyses`, which use pgvector, ARRAY and JSONB columns
and therefore cannot be created on SQLite at all. Binding memory to its own
declarative base means `MemoryBase.metadata.create_all()` on a local SQLite
file produces exactly one table and nothing else — no Postgres dependency
leaks in through shared metadata.

Why this is not the `analyses` table
------------------------------------
`analyses` records what the agent *guessed*: a generated root cause and a
list of suggested next steps, produced before anyone confirmed whether they
were right. Memory records what actually *fixed* the failure. Storing them
in one table would let every wrong diagnosis harden into permanent "team
knowledge", which is worse than having no memory at all — a confidently
wrong recall is more damaging than an empty one.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class MemoryBase(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    # Not `func.now()`: that resolves server-side, and SQLite's CURRENT_TIMESTAMP
    # is naive UTC while Postgres returns tz-aware. Generating it in Python
    # keeps both backends returning the same aware datetime.
    return datetime.now(UTC)


class FailureMemory(MemoryBase):
    """One failure signature this project has seen, and how it was resolved."""

    __tablename__ = "failure_memories"

    __table_args__ = (
        # One row per (project, signature). A recurring failure increments
        # `occurrences` rather than inserting duplicates — otherwise a flaky
        # test that fires 200 times would drown out everything else in recall.
        UniqueConstraint("project_id", "signature", name="uq_project_signature"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Tenant boundary. Never accepted as a tool argument — always taken from
    # the authenticated context (hosted) or derived from the local git remote
    # (stdio). See axiom_debug/memory/project.py for why.
    project_id: Mapped[str] = mapped_column(String(64), index=True)

    # The normalised traceback signature, e.g.
    # "AttributeError: 'DataFrame' object has no attribute '<str>' @ run".
    # Exact match on this is the fast path in recall — most repeat CI
    # failures are byte-identical once normalised.
    signature: Mapped[str] = mapped_column(String(500), index=True)

    language: Mapped[str] = mapped_column(String(30), default="python")
    exception_type: Mapped[str | None] = mapped_column(String(200))

    # Free text describing the failure as it appeared. Not the full log —
    # logs are unbounded and can carry secrets.
    description: Mapped[str | None] = mapped_column(Text)

    # What actually fixed it. This is the payload the whole product exists to
    # return, so it is required: a memory row with no resolution is noise.
    resolution: Mapped[str] = mapped_column(Text)

    # Provenance, when the caller can supply it — a commit SHA, PR URL, or
    # ticket. Optional because a fix is often just "bumped the pin".
    resolved_by: Mapped[str | None] = mapped_column(String(500))

    # agent | human | ci — where the resolution came from. Lets a consumer
    # weight a human-confirmed fix above an agent-proposed one.
    source: Mapped[str] = mapped_column(String(20), default="human")

    # Embedding of the signature, packed float32. See vectors.py for why this
    # is a BLOB and not a pgvector column.
    signature_vec: Mapped[bytes | None] = mapped_column(LargeBinary)

    occurrences: Mapped[int] = mapped_column(Integer, default=1)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
