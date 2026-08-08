"""add language to oss_incidents

Revision ID: d3b7f0a91c52
Revises: c8f2a15d4e60
Create Date: 2026-08-08

Multi-language support: with Python and JavaScript incidents in one index,
retrieval needs to filter by ecosystem. A Python TypeError and a JavaScript
TypeError describe genuinely different failures but read similarly enough to
clear the similarity threshold for each other — without this column a Node
failure can be "explained" by a pandas issue.

Existing rows are all from the Python crawl, so server_default='python'
backfills them correctly rather than needing a data migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3b7f0a91c52'
down_revision: Union[str, Sequence[str], None] = 'c8f2a15d4e60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "oss_incidents",
        sa.Column("language", sa.String(30), nullable=False, server_default="python"),
    )
    op.create_index("ix_oss_incidents_language", "oss_incidents", ["language"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_oss_incidents_language", table_name="oss_incidents")
    op.drop_column("oss_incidents", "language")
