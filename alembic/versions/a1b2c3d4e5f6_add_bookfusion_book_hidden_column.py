"""add bookfusion_books hidden column

Revision ID: a1b2c3d4e5f6
Revises: c9d0e1f2a3b4
Create Date: 2026-03-06
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | Sequence[str] | None = 'c9d0e1f2a3b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('bookfusion_books', sa.Column('hidden', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('bookfusion_books', 'hidden')
