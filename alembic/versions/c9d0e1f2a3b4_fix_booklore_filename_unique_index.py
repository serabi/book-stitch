"""fix stale unique index on booklore_books.filename

Some databases still carry a unique single-column index on
booklore_books.filename. That index causes inserts to fail for multi-source
Booklore caches even though the intended uniqueness is (filename, source).

Revision ID: c9d0e1f2a3b4
Revises: b2c3d4e5f6a7
Create Date: 2026-03-06
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: str | Sequence[str] | None = 'b2c3d4e5f6a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_filename_only_index(index_def: dict) -> bool:
    return (index_def.get('column_names') or []) == ['filename']


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'booklore_books' not in inspector.get_table_names():
        return

    # Remove any stale UNIQUE index on filename only.
    for index_def in inspector.get_indexes('booklore_books'):
        if index_def.get('unique') and _is_filename_only_index(index_def):
            op.drop_index(index_def['name'], table_name='booklore_books')

    # Ensure a regular (non-unique) filename index exists for lookups.
    inspector.clear_cache()
    refreshed = sa.inspect(bind)
    has_non_unique_filename_index = any(
        _is_filename_only_index(index_def) and not index_def.get('unique')
        for index_def in refreshed.get_indexes('booklore_books')
    )
    if not has_non_unique_filename_index:
        op.create_index(op.f('ix_booklore_books_filename'), 'booklore_books', ['filename'], unique=False)


def downgrade() -> None:
    # Intentionally non-reversible: restoring a UNIQUE filename-only index
    # would fail for valid multi-source datasets with duplicate filenames.
    pass
