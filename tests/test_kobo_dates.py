"""Tests for Kobo Event-table parsing and device-derived reading dates:

- parse_qvariant_map + iter_open_events (kobo_reader)
- first-open ingestion + reading_dates_for (kobo_service)
"""

import os
import sqlite3
import struct
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.kobo_service import KoboService
from src.utils.kobo_reader import iter_open_events, parse_qvariant_map

# ── Qt QVariantMap writer (test fixture; mirrors kobo_reader's reader) ──


def _qt_byte_array(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _qt_string(s: str) -> bytes:
    return _qt_byte_array(s.encode("utf-16-be"))


def _qt_variant_int(v: int) -> bytes:
    return struct.pack(">IBi", 2, 0, v)  # type=Int, is_null=0, payload


def _qt_variant_list_ints(vals) -> bytes:
    return struct.pack(">IBI", 9, 0, len(vals)) + b"".join(_qt_variant_int(v) for v in vals)


def _qvariant_blob(mapping: dict) -> bytes:
    blob = struct.pack(">I", len(mapping))
    for key, value in mapping.items():
        blob += _qt_string(key)
        blob += _qt_variant_list_ints(value)
    return blob


TS_EARLY = 1753000000  # 2025-07-20T09:06:40Z
TS_LATE = 1753600000  # 2025-07-27T07:46:40Z


def _create_kobo_db(path, *, use_events=True, event_blob=None, analytics=True):
    Path(path).unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE content (
            ContentType INTEGER, ContentID TEXT, ISBN TEXT, Title TEXT, Attribution TEXT,
            MimeType TEXT, TimeSpentReading INTEGER, ___PercentRead INTEGER, ReadStatus INTEGER,
            DateLastRead TEXT, ___UserID TEXT
        );
        CREATE TABLE Bookmark (
            BookmarkID TEXT, VolumeID TEXT, Text TEXT, Annotation TEXT,
            DateCreated TEXT, DateModified TEXT, ChapterProgress FLOAT
        );
        """
    )
    conn.execute(
        "INSERT INTO content VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (6, "cid-dune", None, "Dune", "Frank Herbert",
         "application/x-kobo-epub+zip", 3600, 47, 1, "2026-08-01T12:30:00", "user-1"),
    )
    if use_events:
        conn.execute(
            "CREATE TABLE Event (EventType INTEGER, EventCount INTEGER, LastOccurrence TEXT,"
            " ContentID TEXT, Checksum TEXT, ExtraData BLOB)"
        )
        blob = event_blob if event_blob is not None else _qvariant_blob({"eventTimestamps": [TS_LATE, TS_EARLY]})
        conn.execute("INSERT INTO Event VALUES (?, ?, ?, ?, ?, ?)", (3, 2, "2025-07-27T07:46:40", "cid-dune", "cksum", blob))
        # Noise row of an unrelated type — must be ignored.
        conn.execute("INSERT INTO Event VALUES (?, ?, ?, ?, ?, ?)", (46, 5, "2025-07-27T07:46:40", "cid-dune", "cksum2", blob))
    if analytics:
        conn.execute("CREATE TABLE AnalyticsEvents (Id TEXT, Timestamp TEXT, Type TEXT, Attributes TEXT, Metrics TEXT)")
        conn.execute(
            "INSERT INTO AnalyticsEvents VALUES (?, ?, ?, ?, ?)",
            ("a1", "2025-07-22T10:00:00", "StartReadingBook", '{"volumeid": "cid-analytics"}', "{}"),
        )
        conn.execute(
            "INSERT INTO AnalyticsEvents VALUES (?, ?, ?, ?, ?)",
            ("a2", "2025-07-23T10:00:00", "LeaveContent", '{"volumeid": "cid-dune"}', "{}"),
        )
    conn.commit()
    conn.close()


# ── parse_qvariant_map ──


def test_parse_qvariant_map_roundtrip():
    blob = _qvariant_blob({"eventTimestamps": [TS_LATE, TS_EARLY]})
    assert parse_qvariant_map(blob) == {"eventTimestamps": [TS_LATE, TS_EARLY]}


def test_parse_qvariant_map_rejects_garbage():
    with pytest.raises((ValueError, struct.error)):
        parse_qvariant_map(b"\x00\x00")
    with pytest.raises((ValueError, struct.error)):
        parse_qvariant_map(b"garbage-blob")
    # Trailing bytes are not a valid single map
    with pytest.raises(ValueError):
        parse_qvariant_map(_qvariant_blob({"a": [1]}) + b"\x00")


# ── iter_open_events ──


def test_iter_open_events_reads_event_table_and_analytics():
    with tempfile.TemporaryDirectory() as tmp:
        db_file = Path(tmp) / "KoboReader.sqlite"
        _create_kobo_db(db_file)

        events = list(iter_open_events(db_file))

    by_cid = {}
    for ev in events:
        by_cid.setdefault(ev.content_id, []).append(ev.occurred_at)

    # Type-3 row yields both accumulated timestamps for cid-dune
    assert set(by_cid["cid-dune"]) == {
        datetime.fromtimestamp(TS_LATE, tz=UTC),
        datetime.fromtimestamp(TS_EARLY, tz=UTC),
    }
    # Analytics StartReadingBook contributes its timestamp
    assert by_cid["cid-analytics"] == [datetime(2025, 7, 22, 10, 0, tzinfo=UTC)]


def test_iter_open_events_skips_bad_blob():
    with tempfile.TemporaryDirectory() as tmp:
        db_file = Path(tmp) / "KoboReader.sqlite"
        _create_kobo_db(db_file, event_blob=b"not-a-qvariant")

        events = list(iter_open_events(db_file))

    content_ids = {ev.content_id for ev in events}
    assert "cid-dune" not in content_ids
    assert content_ids == {"cid-analytics"}


def test_iter_open_events_handles_missing_tables():
    with tempfile.TemporaryDirectory() as tmp:
        db_file = Path(tmp) / "KoboReader.sqlite"
        _create_kobo_db(db_file, use_events=False, analytics=False)

        assert list(iter_open_events(db_file)) == []


# ── Service ingestion ──


def _make_service(temp_dir, kobo_dir):
    db_path = Path(temp_dir) / "db" / "test.db"
    db_service = DatabaseService(str(db_path))
    service = KoboService(db_service, data_dir=Path(temp_dir) / "data")
    os.environ["KOBO_DB_DIR"] = str(kobo_dir)
    return db_service, service


def test_first_open_merges_across_copies_earliest_wins():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        fresh = kobo_dir / "new.sqlite"
        stale = kobo_dir / "old.sqlite"
        _create_kobo_db(fresh, analytics=False)  # blob has TS_LATE + TS_EARLY
        _create_kobo_db(stale, event_blob=_qvariant_blob({"eventTimestamps": [TS_EARLY - 100000]}), analytics=False)
        os.utime(fresh, (fresh.stat().st_atime, fresh.stat().st_mtime + 100))

        db_service, service = _make_service(temp_dir, kobo_dir)
        assert service.refresh_if_changed()

        dune = db_service.get_kobo_book("cid-dune")
        assert dune is not None
        assert dune.first_opened_at is not None
        # Earliest open across both copies wins
        expected = datetime.fromtimestamp(TS_EARLY - 100000, tz=UTC).replace(tzinfo=None)
        assert dune.first_opened_at == expected

        db_service.db_manager.close()


def test_first_open_never_moves_later_on_reingest():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        db_file = kobo_dir / "KoboReader.sqlite"
        _create_kobo_db(db_file, event_blob=_qvariant_blob({"eventTimestamps": [TS_EARLY]}), analytics=False)

        db_service, service = _make_service(temp_dir, kobo_dir)
        service.refresh_if_changed()
        assert db_service.get_kobo_book("cid-dune").first_opened_at == datetime.fromtimestamp(
            TS_EARLY, tz=UTC
        ).replace(tzinfo=None)

        # New copy with only a later open: stored value must stay put
        _create_kobo_db(db_file, event_blob=_qvariant_blob({"eventTimestamps": [TS_LATE]}), analytics=False)
        service.refresh_if_changed()
        assert db_service.get_kobo_book("cid-dune").first_opened_at == datetime.fromtimestamp(
            TS_EARLY, tz=UTC
        ).replace(tzinfo=None)

        db_service.db_manager.close()


def test_events_for_unknown_content_ids_are_skipped():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        _create_kobo_db(kobo_dir / "KoboReader.sqlite", analytics=True)  # cid-analytics is not in content table

        db_service, service = _make_service(temp_dir, kobo_dir)
        service.refresh_if_changed()

        assert db_service.get_kobo_book("cid-analytics") is None
        assert db_service.get_kobo_book("cid-dune").first_opened_at is not None

        db_service.db_manager.close()


# ── reading_dates_for ──


def test_reading_dates_for_matched_book():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        _create_kobo_db(kobo_dir / "KoboReader.sqlite", analytics=False)

        db_service, service = _make_service(temp_dir, kobo_dir)
        book = db_service.save_book(Book(abs_id="abs-dune", title="Dune", status="active"))
        service.refresh_if_changed()

        dune = db_service.get_kobo_book("cid-dune")
        assert dune.matched_book_id == book.id

        dates = service.reading_dates_for(book.id)
        assert dates["started_at"] == datetime.fromtimestamp(TS_EARLY, tz=UTC).date().isoformat()
        # read_status=1 (still reading) → no finished date
        assert "finished_at" not in dates

        db_service.db_manager.close()


def test_reading_dates_finished_uses_date_last_read():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        _create_kobo_db(kobo_dir / "KoboReader.sqlite", analytics=False)

        db_service, service = _make_service(temp_dir, kobo_dir)
        book = db_service.save_book(Book(abs_id="abs-dune", title="Dune", status="active"))
        service.refresh_if_changed()
        db_service.save_kobo_books(
            [{"content_id": "cid-dune", "read_status": 2, "percent": 100, "date_last_read": datetime(2026, 8, 1, 12, 30, tzinfo=UTC)}]
        )

        dates = service.reading_dates_for(book.id)
        assert dates["finished_at"] == "2026-08-01"

        db_service.db_manager.close()


def test_reading_dates_for_unmatched_book_is_empty():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        _create_kobo_db(kobo_dir / "KoboReader.sqlite", analytics=False)

        db_service, service = _make_service(temp_dir, kobo_dir)
        service.refresh_if_changed()

        assert service.reading_dates_for(999) == {}

        db_service.db_manager.close()
