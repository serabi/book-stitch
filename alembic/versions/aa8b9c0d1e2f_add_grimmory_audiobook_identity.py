"""add Grimmory audiobook identity

Revision ID: aa8b9c0d1e2f
Revises: z7a8b9c0d1e2
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "aa8b9c0d1e2f"
down_revision: str | Sequence[str] | None = "z7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("books", sa.Column("grimmory_audio_source_id", sa.String(length=255), nullable=True))
    op.create_index(
        "ix_books_grimmory_audio_source_id",
        "books",
        ["grimmory_audio_source_id"],
        unique=True,
    )
    op.add_column(
        "detected_books",
        sa.Column("media_format", sa.String(length=20), nullable=False, server_default="ebook"),
    )
    op.execute("UPDATE detected_books SET media_format = 'audiobook' WHERE source = 'abs'")


def downgrade() -> None:
    op.drop_column("detected_books", "media_format")
    op.drop_index("ix_books_grimmory_audio_source_id", table_name="books")
    op.drop_column("books", "grimmory_audio_source_id")
