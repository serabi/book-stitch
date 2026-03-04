"""add activity_flag column to books

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-03-02
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: str | Sequence[str] | None = 'a7b8c9d0e1f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'books' not in inspector.get_table_names():
        return

    columns = {c['name'] for c in inspector.get_columns('books')}

    if 'activity_flag' not in columns:
        op.add_column('books', sa.Column('activity_flag', sa.Boolean(), server_default=sa.false()))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'books' not in inspector.get_table_names():
        return

    columns = {c['name'] for c in inspector.get_columns('books')}
    if 'activity_flag' in columns:
        op.drop_column('books', 'activity_flag')
