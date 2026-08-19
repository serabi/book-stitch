"""Tests for src/services/kobo_service.py — ingestion, matching, progress."""

import os
import sqlite3
import tempfile
from pathlib import Path

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.kobo_service import KoboService


def _create_kobo_db(path, *, dune_percent=47, dune_status=1):
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
            (6, "cid-dune", "9780441172719", "Dune", "Frank Herbert",
             "application/x-kobo-epub+zip", 3600, dune_percent, dune_status, "2026-08-01T12:30:00", "user-1"),
            (6, "cid-unmatched", None, "Totally Unknown Book", "Someone",
             "application/epub+zip", 0, 3, 1, "2026-08-02T10:00:00", "user-1"),
        ],
    )
    conn.execute(
        "INSERT INTO Bookmark VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("bm-1", "cid-dune", "The sleeper must awaken.", "", "2026-07-28T21:15:00", None, 0.42),
    )
    conn.commit()
    conn.close()


def _make_service(temp_dir, kobo_dir=None):
    db_path = Path(temp_dir) / "db" / "test.db"
    db_service = DatabaseService(str(db_path))
    data_dir = Path(temp_dir) / "data"
    data_dir.mkdir(exist_ok=True)
    service = KoboService(db_service, data_dir=data_dir)
    if kobo_dir:
        os.environ["KOBO_DB_DIR"] = str(kobo_dir)
    return db_service, service


def test_is_configured_requires_enabled_and_files():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        db_service, service = _make_service(temp_dir, kobo_dir)

        assert not service.is_configured()  # no files yet

        _create_kobo_db(kobo_dir / "KoboReader.sqlite")
        assert service.is_configured()

        os.environ["KOBO_ENABLED"] = "false"
        assert not service.is_configured()

        db_service.db_manager.close()


def test_refresh_ingests_books_and_bookmarks_and_auto_matches():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        _create_kobo_db(kobo_dir / "KoboReader.sqlite")
        db_service, service = _make_service(temp_dir, kobo_dir)

        book = db_service.save_book(Book(abs_id="abs-dune", title="Dune", status="active"))

        assert service.refresh_if_changed()

        # second scan: same file signature, no-op
        assert not service.refresh_if_changed()

        dune = db_service.get_kobo_book("cid-dune")
        assert dune is not None
        assert dune.matched_book_id == book.id  # auto-matched by normalized title
        assert dune.percent == 47

        unmatched = db_service.get_kobo_book("cid-unmatched")
        assert unmatched.matched_book_id is None

        # bookmarks linked to the same library book
        bookmarks = db_service.get_kobo_bookmarks_for_book_by_book_id(book.id)
        assert len(bookmarks) == 1
        assert bookmarks[0].text == "The sleeper must awaken."

        db_service.db_manager.close()


def test_refresh_reingests_when_file_changes():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        db_file = kobo_dir / "KoboReader.sqlite"
        _create_kobo_db(db_file)
        db_service, service = _make_service(temp_dir, kobo_dir)

        assert service.refresh_if_changed()
        assert db_service.get_kobo_book("cid-dune").percent == 47

        _create_kobo_db(db_file, dune_percent=62)
        assert service.refresh_if_changed()
        assert db_service.get_kobo_book("cid-dune").percent == 62

        db_service.db_manager.close()


def test_progress_for_book():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        _create_kobo_db(kobo_dir / "KoboReader.sqlite")
        db_service, service = _make_service(temp_dir, kobo_dir)

        book = db_service.save_book(Book(abs_id="abs-dune", title="Dune", status="active"))
        other = db_service.save_book(Book(abs_id="abs-other", title="Other Book", status="active"))

        assert service.progress_for_book(book.id) == 0.47
        assert service.progress_for_book(other.id) is None

        # ReadStatus=2 means finished -> 1.0 regardless of recorded percent
        kobo_dir2 = Path(temp_dir) / "kobo2"
        kobo_dir2.mkdir()
        _create_kobo_db(kobo_dir2 / "KoboReader.sqlite", dune_percent=90, dune_status=2)
        os.environ["KOBO_DB_DIR"] = str(kobo_dir2)
        assert service.progress_for_book(book.id) == 1.0

        db_service.db_manager.close()


def test_bookmarks_merge_across_database_copies():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        fresh = kobo_dir / "new.sqlite"
        stale = kobo_dir / "old.sqlite"
        _create_kobo_db(fresh)
        _create_kobo_db(stale)

        # the deleted-highlight case: add an extra bookmark only to the older copy
        conn = sqlite3.connect(stale)
        conn.execute(
            "INSERT INTO Bookmark VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("bm-deleted", "cid-dune", "highlight from a deleted book copy", "", "2026-06-01T10:00:00", None, None),
        )
        conn.commit()
        conn.close()
        # make sure "fresh" is actually the newest by mtime
        os.utime(fresh, (fresh.stat().st_atime, fresh.stat().st_mtime + 100))

        db_service, service = _make_service(temp_dir, kobo_dir)
        service.refresh_if_changed()

        ids = {b.bookmark_id for b in db_service.get_kobo_bookmarks()}
        assert ids == {"bm-1", "bm-deleted"}

        db_service.db_manager.close()


def test_link_and_unlink():
    with tempfile.TemporaryDirectory() as temp_dir:
        kobo_dir = Path(temp_dir) / "kobo"
        kobo_dir.mkdir()
        _create_kobo_db(kobo_dir / "KoboReader.sqlite")
        db_service, service = _make_service(temp_dir, kobo_dir)
        service.refresh_if_changed()

        manual = db_service.save_book(Book(abs_id="abs-manual", title="Totally Unknown Book!", status="active"))
        service.link_book("cid-unmatched", manual.id)

        assert db_service.get_kobo_book("cid-unmatched").matched_book_id == manual.id

        service.unlink_book("cid-unmatched")
        assert db_service.get_kobo_book("cid-unmatched").matched_book_id is None

        db_service.db_manager.close()
