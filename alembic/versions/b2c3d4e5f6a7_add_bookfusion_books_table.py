"""add bookfusion_books table

Revision ID: b2c3d4e5f6a7
Revises: a8b9c0d1e2f3
Create Date: 2026-03-05
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: str | None = 'a8b9c0d1e2f3'
# Note: a8b9c0d1e2f3 = add_bookfusion_highlight_parsed_fields
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'bookfusion_books',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('bookfusion_id', sa.String(255), nullable=False),
        sa.Column('title', sa.String(500), nullable=True),
        sa.Column('authors', sa.String(500), nullable=True),
        sa.Column('filename', sa.String(500), nullable=True),
        sa.Column('frontmatter', sa.Text(), nullable=True),
        sa.Column('tags', sa.String(500), nullable=True),
        sa.Column('series', sa.String(500), nullable=True),
        sa.Column('highlight_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('matched_abs_id', sa.String(255), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.Column('last_updated', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bookfusion_id'),
    )


def downgrade() -> None:
    op.drop_table('bookfusion_books')
