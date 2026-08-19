"""add kobo books and bookmarks tables

Revision ID: f0a71f696d5c
Revises: bb9c0d1e2f3a
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f0a71f696d5c"
down_revision: str | Sequence[str] | None = "bb9c0d1e2f3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kobo_books",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("content_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("author", sa.String(length=500), nullable=True),
        sa.Column("isbn", sa.String(length=64), nullable=True),
        sa.Column("percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("read_status", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("date_last_read", sa.DateTime(), nullable=True),
        sa.Column("time_spent_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_book_id", sa.Integer(), nullable=True),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["matched_book_id"], ["books.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_id"),
    )
    op.create_index("ix_kobo_books_matched_book_id", "kobo_books", ["matched_book_id"], unique=False)

    op.create_table(
        "kobo_bookmarks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bookmark_id", sa.String(length=255), nullable=False),
        sa.Column("content_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="highlight"),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("annotation", sa.Text(), nullable=True),
        sa.Column("chapter_progress", sa.Float(), nullable=True),
        sa.Column("highlighted_at", sa.DateTime(), nullable=True),
        sa.Column("matched_book_id", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["matched_book_id"], ["books.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bookmark_id"),
    )
    op.create_index("ix_kobo_bookmarks_content_id", "kobo_bookmarks", ["content_id"], unique=False)
    op.create_index("ix_kobo_bookmarks_highlighted_at", "kobo_bookmarks", ["highlighted_at"], unique=False)
    op.create_index("ix_kobo_bookmarks_matched_book_id", "kobo_bookmarks", ["matched_book_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_kobo_bookmarks_matched_book_id", table_name="kobo_bookmarks")
    op.drop_index("ix_kobo_bookmarks_highlighted_at", table_name="kobo_bookmarks")
    op.drop_index("ix_kobo_bookmarks_content_id", table_name="kobo_bookmarks")
    op.drop_table("kobo_bookmarks")
    op.drop_index("ix_kobo_books_matched_book_id", table_name="kobo_books")
    op.drop_table("kobo_books")
