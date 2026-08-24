"""add libby_psn_key to books

Revision ID: c8d9e0f1a2b3
Revises: bb9c0d1e2f3a
Create Date: 2026-08-24
"""

from typing import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d9e0f1a2b3"
down_revision: str | Sequence[str] | None = "bb9c0d1e2f3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    book_cols = {col["name"] for col in inspector.get_columns("books")}

    if "libby_psn_key" not in book_cols:
        op.add_column("books", sa.Column("libby_psn_key", sa.String(255), nullable=True))
        op.create_index("ix_books_libby_psn_key", "books", ["libby_psn_key"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    book_cols = {col["name"] for col in inspector.get_columns("books")}

    if "libby_psn_key" in book_cols:
        op.drop_index("ix_books_libby_psn_key", table_name="books")
        op.drop_column("books", "libby_psn_key")
