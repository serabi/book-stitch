"""add bookfusion highlights table

Revision ID: f1a2b3c4d5e6
Revises: d4e5f6a7b8c9
Create Date: 2026-03-05
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: str | Sequence[str] | None = 'd4e5f6a7b8c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'bookfusion_highlights' in inspector.get_table_names():
        return

    op.create_table(
        'bookfusion_highlights',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('bookfusion_book_id', sa.String(255), nullable=False),
        sa.Column('highlight_id', sa.String(255), nullable=False),
        sa.Column('book_title', sa.String(500), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('chapter_heading', sa.String(500), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('highlight_id'),
    )


def downgrade() -> None:
    op.drop_table('bookfusion_highlights')
