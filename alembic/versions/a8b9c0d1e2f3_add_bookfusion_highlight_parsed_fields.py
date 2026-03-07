"""add parsed fields to bookfusion_highlights

Revision ID: a8b9c0d1e2f3
Revises: f1a2b3c4d5e6
Create Date: 2026-03-05
"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

BATCH_SIZE = 500

# revision identifiers, used by Alembic.
revision: str = 'a8b9c0d1e2f3'
down_revision: str | Sequence[str] | None = 'f1a2b3c4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _parse_date(content: str) -> datetime | None:
    m = re.search(r'\*\*Date Created\*\*:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*UTC', content)
    if not m:
        return None
    return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)


def _parse_quote(content: str) -> str | None:
    lines = content.split('\n')
    quote_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('>'):
            txt = stripped.lstrip('>').strip()
            if txt:
                quote_lines.append(txt)
    return ' '.join(quote_lines) if quote_lines else None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'bookfusion_highlights' not in inspector.get_table_names():
        return

    existing_cols = {c['name'] for c in inspector.get_columns('bookfusion_highlights')}

    if 'highlighted_at' not in existing_cols:
        op.add_column('bookfusion_highlights',
                      sa.Column('highlighted_at', sa.DateTime(), nullable=True))
    if 'quote_text' not in existing_cols:
        op.add_column('bookfusion_highlights',
                      sa.Column('quote_text', sa.Text(), nullable=True))
    if 'matched_abs_id' not in existing_cols:
        op.add_column('bookfusion_highlights',
                      sa.Column('matched_abs_id', sa.String(500), nullable=True))

    # Backfill existing rows
    conn = bind
    offset = 0
    while True:
        rows = conn.execute(
            text('SELECT id, content FROM bookfusion_highlights LIMIT :limit OFFSET :offset'),
            {'limit': BATCH_SIZE, 'offset': offset},
        ).fetchall()
        if not rows:
            break

        for row in rows:
            hl_id, content = row
            highlighted_at = _parse_date(content or '')
            quote = _parse_quote(content or '')
            params = {'id': hl_id, 'quote': quote}
            set_parts = ['quote_text = :quote']
            if highlighted_at:
                params['date'] = highlighted_at
                set_parts.append('highlighted_at = :date')
            conn.execute(
                text(f"UPDATE bookfusion_highlights SET {', '.join(set_parts)} WHERE id = :id"),
                params,
            )

        offset += BATCH_SIZE


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'bookfusion_highlights' not in inspector.get_table_names():
        return

    existing_cols = {c['name'] for c in inspector.get_columns('bookfusion_highlights')}
    if 'matched_abs_id' in existing_cols:
        op.drop_column('bookfusion_highlights', 'matched_abs_id')
    if 'quote_text' in existing_cols:
        op.drop_column('bookfusion_highlights', 'quote_text')
    if 'highlighted_at' in existing_cols:
        op.drop_column('bookfusion_highlights', 'highlighted_at')
