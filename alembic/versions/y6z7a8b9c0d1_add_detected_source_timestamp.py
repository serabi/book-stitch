"""add detected source timestamp

Revision ID: y6z7a8b9c0d1
Revises: x5y6z7a8b9c0
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "y6z7a8b9c0d1"
down_revision: str | Sequence[str] | None = "x5y6z7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("detected_books", sa.Column("source_updated_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE detected_books SET source_id = 'default:' || source_id "
        "WHERE source = 'grimmory'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE detected_books SET source_id = substr(source_id, 9) "
        "WHERE source = 'grimmory' AND source_id LIKE 'default:%'"
    )
    op.drop_column("detected_books", "source_updated_at")
