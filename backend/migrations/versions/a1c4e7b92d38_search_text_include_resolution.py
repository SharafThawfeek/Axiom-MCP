"""include resolution_summary in search_text

Revision ID: a1c4e7b92d38
Revises: 381f16155fa3
Create Date: 2026-08-08

The sparse half of hybrid retrieval existed to catch exact technical tokens
the dense/vector pass smooths over — but search_text only covered
issue_title + problem_summary, so it was blind to how a bug was actually
FIXED. Confirmed live against real crawled pandas data: keyword-searching
distinctive identifiers that appear only in resolution text
(`_from_sequence`, `ExtensionArray`) matched zero rows, meaning half the
hybrid search contributed nothing for exactly the queries it was meant to
handle best.

A generated column's expression can't be altered in place, so this drops
and recreates both the column and its GIN index. Postgres recomputes the
tsvector for every existing row automatically — no re-embedding and no
re-crawl needed, since this only touches the keyword index, not the vector.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4e7b92d38'
down_revision: Union[str, Sequence[str], None] = '381f16155fa3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = (
    "to_tsvector('english', coalesce(issue_title, '') || ' ' || "
    "coalesce(problem_summary, ''))"
)
_NEW = (
    "to_tsvector('english', coalesce(issue_title, '') || ' ' || "
    "coalesce(problem_summary, '') || ' ' || coalesce(resolution_summary, ''))"
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
