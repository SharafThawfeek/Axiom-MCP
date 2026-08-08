"""add suggested_patch to analyses

Revision ID: 381f16155fa3
Revises: 5954adf2c9e6
Create Date: 2026-08-08

suggested_patch: a unified diff the agent proposes, only ever populated when
the request included real file_context — never fabricated against a file
it hasn't seen. Backs Review/Automatic modes' patch delivery.

Note: autogenerate also flagged ix_oss_incidents_embedding and
ix_oss_incidents_search_text for removal — a false positive, since those
were created via raw op.execute() SQL (HNSW/GIN aren't expressible through
SQLAlchemy's Index()), so autogenerate can't see they belong to the model.
Deliberately not touching either index here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '381f16155fa3'
down_revision: Union[str, Sequence[str], None] = '5954adf2c9e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('analyses', sa.Column('suggested_patch', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('analyses', 'suggested_patch')
