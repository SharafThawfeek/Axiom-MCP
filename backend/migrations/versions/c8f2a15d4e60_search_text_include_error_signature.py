"""include error_signature in search_text

Revision ID: c8f2a15d4e60
Revises: a1c4e7b92d38
Create Date: 2026-08-08

error_signature was write-only: extracted by the indexer's LLM pass,
sanitized, stored, and given its own btree index — but no query anywhere
ever read it. Including it in search_text makes it earn its keep with no
new query path, since an exception type ("AttributeError: ...") is exactly
the kind of exact token the keyword pass handles better than embeddings.

Honest caveat on its value: 0 of the 10 real crawled pandas incidents have
a non-null error_signature, because real GitHub bug reports are often
behavioural rather than exception-shaped. That's a small and library-biased
sample, so this keeps the field (cheap, and genuinely useful whenever a
crawled issue *is* exception-shaped) rather than dropping it on thin
evidence. If a much larger crawl still shows it near-empty, dropping the
column and its extraction cost becomes the right call.

Same drop/recreate approach as a1c4e7b92d38 — a generated column's
expression can't be altered in place. Postgres recomputes every row
automatically; no re-crawl or re-embedding needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8f2a15d4e60'
down_revision: Union[str, Sequence[str], None] = 'a1c4e7b92d38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = (
    "to_tsvector('english', coalesce(issue_title, '') || ' ' || "
    "coalesce(problem_summary, '') || ' ' || coalesce(resolution_summary, ''))"
)
_NEW = (
    "to_tsvector('english', coalesce(issue_title, '') || ' ' || "
    "coalesce(problem_summary, '') || ' ' || coalesce(resolution_summary, '') "
    "|| ' ' || coalesce(error_signature, ''))"
)


def _rebuild(expression: str) -> None:
    op.execute("DROP INDEX IF EXISTS ix_oss_incidents_search_text")
    op.drop_column("oss_incidents", "search_text")
    op.add_column(
        "oss_incidents",
        sa.Column(
            "search_text",
            sa.dialects.postgresql.TSVECTOR(),
            sa.Computed(expression, persisted=True),
            nullable=False,
        ),
    )
    op.execute(
        "CREATE INDEX ix_oss_incidents_search_text "
        "ON oss_incidents USING gin(search_text)"
    )


def upgrade() -> None:
    """Upgrade schema."""
    _rebuild(_NEW)


def downgrade() -> None:
    """Downgrade schema."""
    _rebuild(_OLD)
