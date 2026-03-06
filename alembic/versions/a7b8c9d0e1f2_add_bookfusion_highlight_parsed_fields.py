"""add parsed fields to bookfusion_highlights

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-03-05
"""

import re
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: str | Sequence[str] | None = 'f1a2b3c4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _parse_date(content: str) -> str | None:
    m = re.search(r'\*\*Date Created\*\*:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*UTC', content)
    return m.group(1) if m else None


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
    op.add_column('bookfusion_highlights',
                  sa.Column('highlighted_at', sa.DateTime(), nullable=True))
    op.add_column('bookfusion_highlights',
                  sa.Column('quote_text', sa.Text(), nullable=True))
    op.add_column('bookfusion_highlights',
                  sa.Column('matched_abs_id', sa.String(500), nullable=True))

    # Backfill existing rows
    conn = op.get_bind()
    rows = conn.execute(text('SELECT id, content FROM bookfusion_highlights')).fetchall()
    for row in rows:
        hl_id, content = row
        date_str = _parse_date(content or '')
        quote = _parse_quote(content or '')
        params = {'id': hl_id, 'quote': quote}
        set_parts = ['quote_text = :quote']
        if date_str:
            params['date'] = date_str
            set_parts.append('highlighted_at = :date')
        conn.execute(
            text(f"UPDATE bookfusion_highlights SET {', '.join(set_parts)} WHERE id = :id"),
            params,
        )


def downgrade() -> None:
    op.drop_column('bookfusion_highlights', 'matched_abs_id')
    op.drop_column('bookfusion_highlights', 'quote_text')
    op.drop_column('bookfusion_highlights', 'highlighted_at')
