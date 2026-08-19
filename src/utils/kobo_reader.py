"""Minimal parser for Kobo's KoboReader.sqlite database.

Reads reading progress (content table) and annotations (Bookmark table) from a
stock-firmware Kobo e-reader database copy.

Derived from karlicoss/kobuddy (https://github.com/karlicoss/kobuddy), MIT
licensed, with gratitude. Only the content/Bookmark reads and timestamp
parsing are borrowed; PageKeeper wraps them in its own service layer. See
kobuddy for the full reverse-engineered event tables.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
class KoboOpenEvent:
    """The device opened a book at this moment — the best "started reading"
    signal stock firmware records (kobuddy uses the same two sources)."""

    content_id: str
    occurred_at: datetime


# Event table rows with EventType 3 accumulate one timestamp per reading
# occurrence in their ExtraData blob; the earliest is the book's first open.
_EVENT_TYPE_OPEN = 3
# AnalyticsEvents rows are plain JSON; StartReadingBook fires on book open.
_ANALYTICS_TYPE_START = "StartReadingBook"


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


# ── Qt QVariantMap parsing (Event table ExtraData blobs) ──
# Ported from karlicoss/kobuddy (https://github.com/karlicoss/kobuddy), MIT
# licensed. Kobo serializes event metadata as a Qt QDataStream QVariantMap:
# big-endian, UTF-16-BE string keys, type-tagged values.

_QT_INVALID = 0
_QT_BOOL = 1
_QT_INT = 2
_QT_UINT = 3
_QT_VARIANT_MAP = 8
_QT_VARIANT_LIST = 9
_QT_STRING = 10
_QT_BYTE_ARRAY = 12


class _QVariantReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _read(self, fmt: str) -> Any:
        fmt = ">" + fmt
        [value] = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += struct.calcsize(fmt)
        return value

    def _read_byte_array(self) -> bytes | None:
        size = self._read("I")
        if size == 0xFFFFFFFF:
            return None
        end = self.pos + size
        value = self.data[self.pos:end]
        if len(value) != size:
            raise ValueError(f"truncated byte array at {self.pos}: want {size}, got {len(value)}")
        self.pos = end
        return value

    def _read_string(self) -> str | None:
        value = self._read_byte_array()
        if value is None:
            return None
        return value.decode("utf-16-be")

    def _read_variant(self) -> Any:
        type_id = self._read("I")
        is_null = self._read("B") != 0

        if type_id == _QT_INVALID:
            if not is_null:
                raise ValueError(f"invalid QVariant with is_null=0 at {self.pos}")
            # Pre-Qt-5 streams store an empty QString after an invalid QVariant.
            if self._read_byte_array() is not None:
                raise ValueError(f"invalid QVariant followed by data at {self.pos}")
            return None
        if is_null:
            return None
        if type_id == _QT_BOOL:
            return self._read("B") != 0
        if type_id == _QT_INT:
            return self._read("i")
        if type_id == _QT_UINT:
            return self._read("I")
        if type_id == _QT_VARIANT_MAP:
            return self._read_variant_map()
        if type_id == _QT_VARIANT_LIST:
            return [self._read_variant() for _ in range(self._read("I"))]
        if type_id == _QT_STRING:
            return self._read_string()
        if type_id == _QT_BYTE_ARRAY:
            return self._read_byte_array()
        raise ValueError(f"unknown QVariant type {type_id} at {self.pos}")

    def _read_variant_map(self) -> dict:
        result = {}
        for _ in range(self._read("I")):
            key = self._read_string()
            if key is None:
                raise ValueError(f"null map key at {self.pos}")
            result[key] = self._read_variant()
        return result


def parse_qvariant_map(data: bytes) -> dict:
    """Parse one Qt QDataStream-serialized QVariantMap (big-endian)."""
    reader = _QVariantReader(data)
    result = reader._read_variant_map()
    if reader.pos != len(data):
        raise ValueError(f"trailing bytes after QVariantMap: {len(data) - reader.pos}")
    return result


def _open_events_from_event_table(conn: sqlite3.Connection, db_path: Path) -> Iterator[KoboOpenEvent]:
    cols = _table_columns(conn, "Event")
    if "EventType" not in cols or "ExtraData" not in cols:
        return
    rows = conn.execute(
        "SELECT ContentID, CAST(ExtraData AS BLOB) AS ExtraData FROM Event WHERE EventType = ?",
        (_EVENT_TYPE_OPEN,),
    )
    for row in rows:
        content_id = row["ContentID"]
        blob = row["ExtraData"]
        if not content_id or not blob:
            continue
        try:
            parsed = parse_qvariant_map(blob)
        except (ValueError, struct.error, UnicodeDecodeError) as e:
            logger.debug("%s: skipping unparseable event blob: %s", db_path, e)
            continue
        timestamps = parsed.get("eventTimestamps", [])
        if not isinstance(timestamps, list):
            continue
        for ts in timestamps:
            if isinstance(ts, int) and ts > 0:
                yield KoboOpenEvent(content_id=content_id, occurred_at=datetime.fromtimestamp(ts, tz=UTC))


def _open_events_from_analytics(conn: sqlite3.Connection, db_path: Path) -> Iterator[KoboOpenEvent]:
    cols = _table_columns(conn, "AnalyticsEvents")
    if "Timestamp" not in cols or "Attributes" not in cols:
        return
    rows = conn.execute(
        "SELECT Timestamp, Attributes FROM AnalyticsEvents WHERE Type = ?",
        (_ANALYTICS_TYPE_START,),
    )
    for row in rows:
        occurred_at = _parse_kobo_datetime(row["Timestamp"])
        if not occurred_at:
            continue
        try:
            attrs = json.loads(row["Attributes"] or "{}")
        except ValueError:
            continue
        content_id = attrs.get("volumeid")
        if content_id:
            yield KoboOpenEvent(content_id=content_id, occurred_at=occurred_at)


def iter_open_events(db_path: Path) -> Iterator[KoboOpenEvent]:
    """Yield first-open event candidates from one KoboReader.sqlite.

    Sources (kobuddy): type-3 rows of the Event table (one timestamp per
    reading occurrence) and StartReadingBook rows of AnalyticsEvents. Missing
    tables or malformed blobs are skipped — both tables vary by firmware.
    """
    with _connect_immutable(db_path) as conn:
        yield from _open_events_from_event_table(conn, db_path)
        yield from _open_events_from_analytics(conn, db_path)


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
