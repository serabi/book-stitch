"""add detected processing lease

Revision ID: z7a8b9c0d1e2
Revises: y6z7a8b9c0d1
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "z7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "y6z7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("detected_books", sa.Column("processing_token", sa.String(length=64), nullable=True))
    op.add_column("detected_books", sa.Column("processing_started_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("detected_books", "processing_started_at")
    op.drop_column("detected_books", "processing_token")
