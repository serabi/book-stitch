"""Tests for Kobo bookmarks -> reading journal import."""

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.kobo_service import KoboService

BOOKMARK_ROWS = [
    # highlight with note
    ("bm-note", "cid-1", "Fear is the mind-killer.", "litany against fear", "2026-07-29T08:00:00", None, 0.5),
    # plain highlight
    ("bm-hl", "cid-1", "The sleeper must awaken.", "", "2026-07-28T21:15:00", None, 0.42),
    # position bookmark without text — must not become a journal entry
    ("bm-bare", "cid-1", None, "", "2026-07-30T10:00:00", None, None),
]


def _create_kobo_db(path):
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
        (6, "cid-1", None, "Dune", "Frank Herbert", "application/epub+zip", 0, 50, 1,
         "2026-08-01T12:30:00", "user-1"),
    )
    conn.executemany("INSERT INTO Bookmark VALUES (?, ?, ?, ?, ?, ?, ?)", BOOKMARK_ROWS)
    conn.commit()
    conn.close()


def _make_service(kobo_dir):
    db_service = DatabaseService(str(kobo_dir / "db" / "test.db"))
    service = KoboService(db_service, data_dir=kobo_dir / "data")
    import os

    os.environ["KOBO_DB_DIR"] = str(kobo_dir)
    _create_kobo_db(kobo_dir / "KoboReader.sqlite")
    return db_service, service


def test_save_bookmarks_to_journal_imports_highlights_and_notes():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir)
        db_service, service = _make_service(kobo_dir)
        book = db_service.save_book(Book(abs_id="abs-dune", title="Dune", status="active"))
        service.refresh_if_changed()

        result = service.save_bookmarks_to_journal(book.id)
        assert result == {"saved": 2, "skipped": 0}

        entries = db_service.get_reading_journal_entries_for_book(book.id, "highlight")
        assert len(entries) == 2

        by_text = {e.entry.split("\n")[0]: e for e in entries}

        note_entry = by_text["Fear is the mind-killer."]
        assert "\n> litany against fear" in note_entry.entry
        assert note_entry.created_at is not None
        assert note_entry.created_at.day == 29

        plain = by_text["The sleeper must awaken."]
        assert plain.entry == "The sleeper must awaken."

        # the text-less bookmark was not imported
        assert all("must not" not in e.entry for e in entries)

        # original highlight timestamps preserved (stored naive, parsed from Kobo's UTC)
        assert plain.created_at == datetime(2026, 7, 28, 21, 15)

        db_service.db_manager.close()


def test_save_bookmarks_to_journal_is_idempotent():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir)
        db_service, service = _make_service(kobo_dir)
        book = db_service.save_book(Book(abs_id="abs-dune", title="Dune", status="active"))
        service.refresh_if_changed()

        first = service.save_bookmarks_to_journal(book.id)
        second = service.save_bookmarks_to_journal(book.id)

        assert first["saved"] == 2
        assert second["saved"] == 0
        assert second["skipped"] == 2
        assert len(db_service.get_reading_journal_entries_for_book(book.id, "highlight")) == 2

        db_service.db_manager.close()


def test_save_bookmarks_to_journal_dedupes_across_sources():
    """A passage already journaled (e.g. via BookFusion) is not duplicated."""
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir)
        db_service, service = _make_service(kobo_dir)
        book = db_service.save_book(Book(abs_id="abs-dune", title="Dune", status="active"))
        service.refresh_if_changed()

        # Simulate an earlier BookFusion-style import of the same passage
        db_service.add_reading_journal(
            book.id, "highlight", entry="The sleeper must awaken.\n— *Chapter 3*", abs_id="abs-dune"
        )

        result = service.save_bookmarks_to_journal(book.id)

        assert result["saved"] == 1  # only the note highlight is new
        assert result["skipped"] == 1
        entries = db_service.get_reading_journal_entries_for_book(book.id, "highlight")
        assert len(entries) == 2

        db_service.db_manager.close()


def test_save_bookmarks_to_journal_unmatched_book_saves_nothing():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir)
        db_service, service = _make_service(kobo_dir)
        # library book with a different title: nothing matches
        book = db_service.save_book(Book(abs_id="abs-other", title="Different Book", status="active"))

        result = service.save_bookmarks_to_journal(book.id)
        assert result == {"saved": 0, "skipped": 0}

        missing = service.save_bookmarks_to_journal(99999)
        assert missing.get("error") == "book not found"

        db_service.db_manager.close()
