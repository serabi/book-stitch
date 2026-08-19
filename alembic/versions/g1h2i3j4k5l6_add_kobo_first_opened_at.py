"""add kobo_books first_opened_at

Revision ID: g1h2i3j4k5l6
Revises: f0a71f696d5c
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g1h2i3j4k5l6"
down_revision: str | Sequence[str] | None = "f0a71f696d5c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("kobo_books", sa.Column("first_opened_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("kobo_books", "first_opened_at")
