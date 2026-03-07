"""add hardcover_cover_url and custom_cover_url columns

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-03-06
"""

from typing import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: str | Sequence[str] | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    hardcover_cols = {col['name'] for col in inspector.get_columns('hardcover_details')}
    if 'hardcover_cover_url' not in hardcover_cols:
        op.add_column('hardcover_details', sa.Column('hardcover_cover_url', sa.String(500), nullable=True))

    book_cols = {col['name'] for col in inspector.get_columns('books')}
    if 'custom_cover_url' not in book_cols:
        op.add_column('books', sa.Column('custom_cover_url', sa.String(500), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    hardcover_cols = {col['name'] for col in inspector.get_columns('hardcover_details')}
    if 'hardcover_cover_url' in hardcover_cols:
        op.drop_column('hardcover_details', 'hardcover_cover_url')

    book_cols = {col['name'] for col in inspector.get_columns('books')}
    if 'custom_cover_url' in book_cols:
        op.drop_column('books', 'custom_cover_url')
