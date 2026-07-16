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
    # Qualified identities collapse back to the legacy filename key. When both
    # servers contain the same filename, keep the default-server row; otherwise
    # keep the lowest row id. This makes the lossy downgrade deterministic.
    op.execute(
        """
        DELETE FROM detected_books
        WHERE id IN (
            SELECT id FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY CASE
                            WHEN source_id LIKE 'default:%' THEN substr(source_id, 9)
                            WHEN source_id LIKE '2:%' THEN substr(source_id, 3)
                            ELSE source_id
                        END
                        ORDER BY CASE
                            WHEN source_id LIKE 'default:%' THEN 0
                            WHEN source_id NOT LIKE '2:%' THEN 1
                            ELSE 2
                        END, id
                    ) AS row_num
                FROM detected_books
                WHERE source = 'grimmory'
            ) ranked
            WHERE row_num > 1
        )
        """
    )
    op.execute(
        "UPDATE detected_books SET source_id = CASE "
        "WHEN source_id LIKE 'default:%' THEN substr(source_id, 9) "
        "WHEN source_id LIKE '2:%' THEN substr(source_id, 3) "
        "ELSE source_id END "
        "WHERE source = 'grimmory'"
    )
    op.drop_column("detected_books", "source_updated_at")
