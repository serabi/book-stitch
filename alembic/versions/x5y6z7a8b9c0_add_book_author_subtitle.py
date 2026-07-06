"""add author and subtitle columns to books

Revision ID: x5y6z7a8b9c0
Revises: w3x4y5z6a7b8
Create Date: 2026-07-06
"""

from typing import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "x5y6z7a8b9c0"
down_revision: str | Sequence[str] | None = "w3x4y5z6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Guarded: existing installs may already have these columns because the
    # former runtime column patcher in DatabaseService added them.
    bind = op.get_bind()
    inspector = inspect(bind)
    book_cols = {col["name"] for col in inspector.get_columns("books")}

    if "author" not in book_cols:
        op.add_column("books", sa.Column("author", sa.String(500), nullable=True))
    if "subtitle" not in book_cols:
        op.add_column("books", sa.Column("subtitle", sa.String(500), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    book_cols = {col["name"] for col in inspector.get_columns("books")}

    if "subtitle" in book_cols:
        op.drop_column("books", "subtitle")
    if "author" in book_cols:
        op.drop_column("books", "author")
