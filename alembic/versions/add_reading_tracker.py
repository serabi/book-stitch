"""add reading tracker tables and book reading fields

Revision ID: c3d4e5f6a7b8
Revises: b8c9d0e1f2a3
Create Date: 2026-03-04
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: str | Sequence[str] | None = 'b8c9d0e1f2a3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Add reading fields to books table
    if 'books' in inspector.get_table_names():
        columns = {c['name'] for c in inspector.get_columns('books')}

        if 'started_at' not in columns:
            op.add_column('books', sa.Column('started_at', sa.String(10), nullable=True))
        if 'finished_at' not in columns:
            op.add_column('books', sa.Column('finished_at', sa.String(10), nullable=True))
        if 'rating' not in columns:
            op.add_column('books', sa.Column('rating', sa.Float(), nullable=True))
        if 'read_count' not in columns:
            op.add_column('books', sa.Column('read_count', sa.Integer(), server_default='1'))

    # 2. Create reading_journals table
    if 'reading_journals' not in inspector.get_table_names():
        op.create_table(
            'reading_journals',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('abs_id', sa.String(255), nullable=False),
            sa.Column('event', sa.String(20), nullable=False),
            sa.Column('entry', sa.Text(), nullable=True),
            sa.Column('percentage', sa.Float(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['abs_id'], ['books.abs_id'], ondelete='CASCADE'),
        )
        op.create_index('ix_reading_journals_abs_id', 'reading_journals', ['abs_id'])

    # 3. Create reading_goals table
    if 'reading_goals' not in inspector.get_table_names():
        op.create_table(
            'reading_goals',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('year', sa.Integer(), nullable=False, unique=True),
            sa.Column('target_books', sa.Integer(), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'reading_goals' in inspector.get_table_names():
        op.drop_table('reading_goals')

    if 'reading_journals' in inspector.get_table_names():
        op.drop_table('reading_journals')

    if 'books' in inspector.get_table_names():
        columns = {c['name'] for c in inspector.get_columns('books')}
        for col in ('started_at', 'finished_at', 'rating', 'read_count'):
            if col in columns:
                op.drop_column('books', col)
