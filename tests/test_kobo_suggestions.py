"""Tests for Kobo suggestion candidates: unmatched in-progress device books
show up in the Currently Reading inbox, and matching settles their entries."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.kobo_service import KoboService
from src.services.suggestion_service import SuggestionService


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
    conn.executemany(
        "INSERT INTO content VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (6, "cid-reading", None, "Reading Book", "Some Author",
             "application/x-kobo-epub+zip", 1800, 40, 1, "2026-08-10T20:00:00", "user-1"),
            (6, "cid-nearly-done", None, "Nearly Done", "Author",
             "application/x-kobo-epub+zip", 500, 96, 1, "2026-08-11T20:00:00", "user-1"),
            (6, "cid-finished", None, "Finished Book", "Author",
             "application/x-kobo-epub+zip", 7200, 100, 2, "2026-08-01T20:00:00", "user-1"),
            (6, "cid-unread", None, "Unread Book", "Author",
             "application/x-kobo-epub+zip", 0, 0, 0, None, "user-1"),
        ],
    )
    conn.commit()
    conn.close()


def _make_stack(temp_dir, *, abs_client=None):
    kobo_dir = Path(temp_dir) / "kobo"
    kobo_dir.mkdir()
    _create_kobo_db(kobo_dir / "KoboReader.sqlite")

    db_service = DatabaseService(str(Path(temp_dir) / "db" / "test.db"))
    suggestions = SuggestionService(
        database_service=db_service,
        abs_client=abs_client,
        grimmory_client=None,
        storyteller_client=None,
        library_service=None,
        books_dir=Path(temp_dir) / "empty-books",
        ebook_parser=None,
    )
    service = KoboService(db_service, data_dir=Path(temp_dir) / "data", suggestion_service=suggestions)
    os.environ["KOBO_DB_DIR"] = str(kobo_dir)
    return db_service, service


def _detected(db_service, content_id):
    return db_service.get_detected_book(content_id, source="kobo")


def test_in_progress_unmatched_book_becomes_detected():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_service, service = _make_stack(temp_dir)
        service.refresh_if_changed()

        detected = _detected(db_service, "cid-reading")
        assert detected is not None
        assert detected.status == "detected"
        assert detected.title == "Reading Book"
        assert detected.author == "Some Author"
        assert detected.progress_percentage == 0.40
        assert detected.media_format == "ebook"
        assert detected.device == "Kobo"

        db_service.db_manager.close()


def test_out_of_window_books_are_not_detected():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_service, service = _make_stack(temp_dir)
        service.refresh_if_changed()

        # 96% and finished are above the detection window; unread is below
        assert _detected(db_service, "cid-nearly-done") is None
        assert _detected(db_service, "cid-finished") is None
        assert _detected(db_service, "cid-unread") is None

        db_service.db_manager.close()


def test_hidden_book_is_not_detected():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_service, service = _make_stack(temp_dir)
        # Hide the book up front (as if the user hid it on a previous scan);
        # ingestion must not surface it in the inbox on later scans.
        db_service.save_kobo_books(
            [{"content_id": "cid-reading", "title": "Reading Book", "author": "Some Author", "percent": 40, "read_status": 1}]
        )
        db_service.set_kobo_books_hidden(["cid-reading"], True)

        service.refresh_if_changed()
        assert _detected(db_service, "cid-reading") is None

        db_service.db_manager.close()


def test_abs_audiobook_match_and_cover_attached():
    with tempfile.TemporaryDirectory() as temp_dir:
        abs_client = Mock()
        abs_client.get_all_audiobooks.return_value = [
            {"id": "abs-9", "media": {"metadata": {"title": "Reading Book", "authorName": "Some Author"}}}
        ]
        db_service, service = _make_stack(temp_dir, abs_client=abs_client)
        service.refresh_if_changed()

        detected = _detected(db_service, "cid-reading")
        assert detected is not None
        matches = json.loads(detected.matches_json or "[]")
        assert matches and matches[0]["abs_id"] == "abs-9"
        assert detected.cover_url == "/api/cover-proxy/abs-9"

        db_service.db_manager.close()


def test_manual_link_resolves_detected_entry():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_service, service = _make_stack(temp_dir)
        service.refresh_if_changed()
        assert _detected(db_service, "cid-reading").status == "detected"

        book = db_service.save_book(Book(abs_id="abs-manual", title="Reading Book!", status="active"))
        service.link_book("cid-reading", book.id)

        assert _detected(db_service, "cid-reading").status == "resolved"

        db_service.db_manager.close()


def test_auto_match_resolves_detected_entry():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_service, service = _make_stack(temp_dir)
        service.refresh_if_changed()
        assert _detected(db_service, "cid-reading").status == "detected"

        # Library book appears later with the exact title → auto-match links it
        db_service.save_book(Book(abs_id="abs-1", title="Reading Book", status="active"))
        service2 = KoboService(
            db_service, data_dir=service.data_dir, suggestion_service=service.suggestion_service
        )
        service2.refresh_if_changed()

        assert db_service.get_kobo_book("cid-reading").matched_book_id is not None
        assert _detected(db_service, "cid-reading").status == "resolved"

        db_service.db_manager.close()


def test_dismissed_entry_stays_dismissed_on_rescan():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_service, service = _make_stack(temp_dir)
        service.refresh_if_changed()
        db_service.dismiss_detected_book("cid-reading", source="kobo")

        service2 = KoboService(
            db_service, data_dir=service.data_dir, suggestion_service=service.suggestion_service
        )
        service2.refresh_if_changed()
        assert _detected(db_service, "cid-reading").status == "dismissed"

        db_service.db_manager.close()
