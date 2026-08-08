"""add dependencies fingerprint to analyses

Revision ID: 5954adf2c9e6
Revises: 5cdf4996c443
Create Date: 2026-08-07

The repeat-failure cache originally keyed only on failure_signature, which
served one caller's answer to another running different dependency versions
— the same traceback has a different correct answer when a method is
deprecated in one release and removed in the next. This column makes the
declared dependencies part of the cache key.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5954adf2c9e6'
down_revision: Union[str, Sequence[str], None] = '5cdf4996c443'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "analyses",
        sa.Column("dependencies_fingerprint", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_analyses_dependencies_fingerprint",
        "analyses",
        ["dependencies_fingerprint"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_analyses_dependencies_fingerprint", table_name="analyses")
    op.drop_column("analyses", "dependencies_fingerprint")
