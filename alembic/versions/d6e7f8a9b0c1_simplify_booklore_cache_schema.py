"""simplify booklore cache schema

Revision ID: d6e7f8a9b0c1
Revises: b3c4d5e6f7a8
Create Date: 2026-03-07
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd6e7f8a9b0c1'
down_revision: str | Sequence[str] | None = 'b3c4d5e6f7a8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _dedupe_rows(rows):
    best_by_filename: dict[str, sa.Row] = {}
    for row in rows:
        filename = row.filename
        if not filename:
            continue

        current = best_by_filename.get(filename)
        if current is None:
            best_by_filename[filename] = row
            continue

        current_ts = current.last_updated or datetime.min
        row_ts = row.last_updated or datetime.min
        if row_ts > current_ts or (row_ts == current_ts and row.id > current.id):
            best_by_filename[filename] = row

    return list(best_by_filename.values())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    table_names = set(inspector.get_table_names())
    if 'booklore_books' not in table_names:
        return

    if 'booklore_books_new' in table_names:
        op.drop_table('booklore_books_new')
        inspector = sa.inspect(bind)

    columns = {col['name'] for col in inspector.get_columns('booklore_books')}
    if 'source' not in columns:
        return

    rows = bind.execute(sa.text(
        """
        SELECT id, filename, title, authors, raw_metadata, last_updated
        FROM booklore_books
        ORDER BY filename, last_updated DESC, id DESC
        """
    )).fetchall()
    deduped_rows = _dedupe_rows(rows)

    op.create_table(
        'booklore_books_new',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('filename', sa.String(length=500), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=True),
        sa.Column('authors', sa.String(length=500), nullable=True),
        sa.Column('raw_metadata', sa.Text(), nullable=True),
        sa.Column('last_updated', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('filename', name='uq_booklore_books_filename'),
    )

    if deduped_rows:
        insert_sql = sa.text(
            """
            INSERT INTO booklore_books_new (id, filename, title, authors, raw_metadata, last_updated)
            VALUES (:id, :filename, :title, :authors, :raw_metadata, :last_updated)
            """
        )
        bind.execute(
            insert_sql,
            [
                {
                    'id': row.id,
                    'filename': row.filename,
                    'title': row.title,
                    'authors': row.authors,
                    'raw_metadata': row.raw_metadata,
                    'last_updated': row.last_updated,
                }
                for row in deduped_rows
            ],
        )

    op.drop_table('booklore_books')
    op.rename_table('booklore_books_new', 'booklore_books')


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    table_names = set(inspector.get_table_names())
    if 'booklore_books' not in table_names:
        return

    if 'booklore_books_old' in table_names:
        op.drop_table('booklore_books_old')
        inspector = sa.inspect(bind)

    columns = {col['name'] for col in inspector.get_columns('booklore_books')}
    if 'source' in columns:
        return

    rows = bind.execute(sa.text(
        """
        SELECT id, filename, title, authors, raw_metadata, last_updated
        FROM booklore_books
        ORDER BY id
        """
    )).fetchall()

    op.create_table(
        'booklore_books_old',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column('filename', sa.String(length=500), nullable=False),
        sa.Column('source', sa.String(length=50), nullable=True, server_default='booklore'),
        sa.Column('title', sa.String(length=500), nullable=True),
        sa.Column('authors', sa.String(length=500), nullable=True),
        sa.Column('raw_metadata', sa.Text(), nullable=True),
        sa.Column('last_updated', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('filename', 'source', name='uq_booklore_books_filename_source'),
    )

    if rows:
        insert_sql = sa.text(
            """
            INSERT INTO booklore_books_old (id, filename, source, title, authors, raw_metadata, last_updated)
            VALUES (:id, :filename, :source, :title, :authors, :raw_metadata, :last_updated)
            """
        )
        bind.execute(
            insert_sql,
            [
                {
                    'id': row.id,
                    'filename': row.filename,
                    'source': 'booklore',
                    'title': row.title,
                    'authors': row.authors,
                    'raw_metadata': row.raw_metadata,
                    'last_updated': row.last_updated,
                }
                for row in rows
            ],
        )

    op.drop_table('booklore_books')
    op.rename_table('booklore_books_old', 'booklore_books')
