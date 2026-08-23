"""use Grimmory remote file identity

Revision ID: bb9c0d1e2f3a
Revises: aa8b9c0d1e2f
Create Date: 2026-07-16
"""

import json
from collections import Counter
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "bb9c0d1e2f3a"
down_revision: str | Sequence[str] | None = "aa8b9c0d1e2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("grimmory_books") as batch_op:
        batch_op.drop_constraint("uq_grimmory_server_filename", type_="unique")
        batch_op.add_column(sa.Column("remote_book_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("remote_file_id", sa.String(length=255), nullable=True))

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, server_id, raw_metadata FROM grimmory_books")).mappings().all()
    parsed = []
    for row in rows:
        try:
            metadata = json.loads(row["raw_metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        book_id = metadata.get("id")
        file_id = metadata.get("bookFileId")
        if book_id is not None and file_id is not None:
            parsed.append((row["id"], str(row["server_id"]), str(book_id), str(file_id)))

    counts = Counter((server_id, book_id, file_id) for _, server_id, book_id, file_id in parsed)
    for row_id, server_id, book_id, file_id in parsed:
        if counts[(server_id, book_id, file_id)] == 1:
            connection.execute(
                sa.text(
                    "UPDATE grimmory_books SET remote_book_id = :book_id, remote_file_id = :file_id WHERE id = :row_id"
                ),
                {"book_id": book_id, "file_id": file_id, "row_id": row_id},
            )

    op.create_index(
        "uq_grimmory_remote_file",
        "grimmory_books",
        ["server_id", "remote_book_id", "remote_file_id"],
        unique=True,
        sqlite_where=sa.text("remote_book_id IS NOT NULL AND remote_file_id IS NOT NULL"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT server_id, filename FROM grimmory_books "
            "GROUP BY server_id, filename HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError("Cannot downgrade while distinct Grimmory remote files share a filename")

    op.drop_index("uq_grimmory_remote_file", table_name="grimmory_books")
    with op.batch_alter_table("grimmory_books") as batch_op:
        batch_op.drop_column("remote_file_id")
        batch_op.drop_column("remote_book_id")
        batch_op.create_unique_constraint("uq_grimmory_server_filename", ["server_id", "filename"])
