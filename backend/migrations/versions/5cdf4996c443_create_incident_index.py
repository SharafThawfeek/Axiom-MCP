"""create incident index

Revision ID: 5cdf4996c443
Revises:
Create Date: 2026-08-07 12:07:02.284511

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import pgvector.sqlalchemy


# revision identifiers, used by Alembic.
revision: str = '5cdf4996c443'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 384


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "oss_incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library", sa.String(100), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("issue_url", sa.String(500), nullable=False, unique=True),
        sa.Column("issue_title", sa.String(500), nullable=False),
        sa.Column("fixing_commit_url", sa.String(500), nullable=True),
        sa.Column("problem_summary", sa.Text(), nullable=False),
        sa.Column("resolution_summary", sa.Text(), nullable=False),
        sa.Column("error_signature", sa.String(500), nullable=True),
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.Vector(EMBEDDING_DIM),
            nullable=False,
        ),
        sa.Column(
            "search_text",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(issue_title, '') || ' ' || coalesce(problem_summary, ''))",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_oss_incidents_library", "oss_incidents", ["library"]
    )
    op.create_index(
        "ix_oss_incidents_error_signature", "oss_incidents", ["error_signature"]
    )

    # GIN index for keyword search over search_text.
    op.execute(
        "CREATE INDEX ix_oss_incidents_search_text "
        "ON oss_incidents USING gin(search_text)"
    )

    # HNSW over cosine distance — the metric retrieval_service will query
    # with. No training data required upfront, unlike ivfflat.
    op.execute(
        "CREATE INDEX ix_oss_incidents_embedding "
        "ON oss_incidents USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "analyses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("log_excerpt", sa.Text(), nullable=False),
        sa.Column("failure_signature", sa.String(500), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(10), nullable=False),
        sa.Column("suspected_library", sa.String(100), nullable=True),
        sa.Column(
            "next_steps",
            postgresql.ARRAY(sa.String()),
            nullable=False,
        ),
        sa.Column(
            "matched_incident_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("agent_trace", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_analyses_failure_signature", "analyses", ["failure_signature"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("analyses")
    op.execute("DROP INDEX IF EXISTS ix_oss_incidents_embedding")
    op.execute("DROP INDEX IF EXISTS ix_oss_incidents_search_text")
    op.drop_index("ix_oss_incidents_error_signature", table_name="oss_incidents")
    op.drop_index("ix_oss_incidents_library", table_name="oss_incidents")
    op.drop_table("oss_incidents")
