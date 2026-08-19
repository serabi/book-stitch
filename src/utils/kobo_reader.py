"""Minimal parser for Kobo's KoboReader.sqlite database.

Reads reading progress (content table) and annotations (Bookmark table) from a
stock-firmware Kobo e-reader database copy.

Derived from karlicoss/kobuddy (https://github.com/karlicoss/kobuddy), MIT
licensed, with gratitude. Only the content/Bookmark reads and timestamp
parsing are borrowed; PageKeeper wraps them in its own service layer. See
kobuddy for the full reverse-engineered event tables.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Kobo ReadStatus values (content table)
READ_STATUS_UNREAD = 0
READ_STATUS_READING = 1
READ_STATUS_FINISHED = 2

# Non-book mimetypes kobuddy skips (Pocket articles, embedded images)
_SKIP_MIMETYPES = {"application/x-kobo-html+pocket", "image/png"}


@dataclass(frozen=True)
class KoboBookSnapshot:
    """One row of the Kobo content table (a book on the device)."""

    content_id: str
    title: str
    author: str
    isbn: str | None
    percent: int  # 0-100
    read_status: int  # 0 unread, 1 reading, 2 finished
    date_last_read: datetime | None
    time_spent_seconds: int


@dataclass(frozen=True)
class KoboBookmarkSnapshot:
    """One row of the Kobo Bookmark table (bookmark, highlight, or note)."""

    bookmark_id: str
    volume_id: str  # matches content.ContentID / KoboBookSnapshot.content_id
    text: str | None  # highlighted passage (None = plain bookmark)
    annotation: str  # user's note ('' when absent)
    created: datetime | None
    chapter_progress: float | None  # 0-1 within chapter, when the firmware records it

    @property
    def kind(self) -> str:
        if self.text is None:
            return "bookmark"
        return "annotation" if self.annotation else "highlight"


def _parse_kobo_datetime(s: str | None) -> datetime | None:
    """Parse Kobo's UTC timestamps. From kobuddy: formats vary by firmware era
    (microseconds only in older exports) and a trailing 'Z' appears sometimes."""
    if s is None or s == "":
        return None
    s = s.removesuffix("Z")
    res = None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            res = datetime.strptime(s, fmt)
            # kobuddy normalizes away microseconds since newer firmware omits them
            res = res.replace(microsecond=0)
            break
        except ValueError:
            continue
    if res is None:
        logger.debug("Unparseable Kobo timestamp: %r", s)
        return None
    if res.tzinfo is None:
        res = res.replace(tzinfo=UTC)
    return res


@contextmanager
def _connect_immutable(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a Kobo sqlite read-only. The file may keep syncing while we read,
    so immutable mode (no journaling, no locks) matches kobuddy's approach."""
    dbp = Path(db_path)
    if not dbp.exists():
        raise FileNotFoundError(dbp)
    conn = sqlite3.connect(f"file:{dbp}?immutable=1", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def iter_books(db_path: Path) -> Iterator[KoboBookSnapshot]:
    """Yield books from the content table of one KoboReader.sqlite.

    Filters follow kobuddy: ContentType=6 keeps books; Pocket articles and
    images are skipped; rows without a ___UserID are storefront ads, not
    books the user actually loaded.
    """
    with _connect_immutable(db_path) as conn:
        cols = _table_columns(conn, "content")
        if "ContentID" not in cols:
            logger.warning("%s: no content table", db_path)
            return
        has_time_spent = "TimeSpentReading" in cols
        for row in conn.execute("SELECT * FROM content WHERE ContentType=6"):
            mimetype = row["MimeType"]
            if mimetype in _SKIP_MIMETYPES:
                continue
            if not row["___UserID"]:
                continue
            title = row["Title"]
            yield KoboBookSnapshot(
                content_id=row["ContentID"],
                title=(title or "").strip(),
                author=row["Attribution"] or "",
                isbn=row["ISBN"] or None,
                percent=int(row["___PercentRead"] or 0),
                read_status=int(row["ReadStatus"] or 0),
                date_last_read=_parse_kobo_datetime(row["DateLastRead"]),
                time_spent_seconds=int(row["TimeSpentReading"] or 0) if has_time_spent else 0,
            )


def iter_bookmarks(db_path: Path) -> Iterator[KoboBookmarkSnapshot]:
    """Yield bookmarks/highlights/notes from the Bookmark table.

    DateCreated can be missing on some devices (kobuddy issue #1), so creation
    time falls back to DateModified. ChapterProgress is best-effort: older
    firmware doesn't record it.
    """
    with _connect_immutable(db_path) as conn:
        cols = _table_columns(conn, "Bookmark")
        if "BookmarkID" not in cols:
            logger.debug("%s: no Bookmark table", db_path)
            return
        has_chapter_progress = "ChapterProgress" in cols
        for row in conn.execute("SELECT * FROM Bookmark"):
            created = _parse_kobo_datetime(row["DateCreated"]) or _parse_kobo_datetime(row["DateModified"])
            bookmark_id = row["BookmarkID"]
            if not bookmark_id:
                continue
            chapter_progress = row["ChapterProgress"] if has_chapter_progress else None
            yield KoboBookmarkSnapshot(
                bookmark_id=bookmark_id,
                volume_id=row["VolumeID"] or "",
                text=row["Text"],
                annotation=row["Annotation"] or "",
                created=created,
                chapter_progress=float(chapter_progress) if chapter_progress is not None else None,
            )
