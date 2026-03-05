"""fix booklore_books filename unique constraint

The migration a7b8c9d0e1f2 intended to replace the single-column unique
constraint on filename with a composite (filename, source) constraint,
but the old constraint may still be present in existing databases.
This migration force-recreates the table with only the composite constraint.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-05
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: str | Sequence[str] | None = 'c3d4e5f6a7b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'booklore_books' not in inspector.get_table_names():
        return

    # Force-recreate the table, replacing any stale single-column unique
    # constraint with the correct composite (filename, source) constraint.
    with op.batch_alter_table(
        'booklore_books',
        recreate='always',
        table_args=[
            sa.UniqueConstraint('filename', 'source', name='uq_booklore_books_filename_source'),
        ],
    ):
        # Explicitly drop the old single-column constraint if it exists.
        # batch_alter_table in recreate='always' mode will rebuild the table
        # using only the constraints specified in table_args above.
        pass


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'booklore_books' not in inspector.get_table_names():
        return

    # Check for duplicate filenames that would violate the old single-column constraint
    result = bind.execute(
        sa.text("SELECT filename, COUNT(*) AS cnt FROM booklore_books GROUP BY filename HAVING cnt > 1")
    )
    dupes = result.fetchall()
    if dupes:
        filenames = [row[0] for row in dupes]
        raise RuntimeError(
            f"Cannot downgrade: duplicate filenames exist that would violate "
            f"the old single-column unique constraint: {filenames}"
        )

    with op.batch_alter_table(
        'booklore_books',
        recreate='always',
        table_args=[
            sa.UniqueConstraint('filename', name='uq_booklore_books_filename'),
        ],
    ):
        pass
