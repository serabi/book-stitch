"""Tests for src/utils/kobo_reader.py — parsing KoboReader.sqlite copies."""

import sqlite3

from src.utils import kobo_reader
from src.utils.kobo_reader import iter_bookmarks, iter_books


def _create_kobo_db(path):
    """Build a minimal KoboReader.sqlite fixture with the pages we read."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE content (
            ContentType INTEGER,
            ContentID TEXT,
            ISBN TEXT,
            Title TEXT,
            Attribution TEXT,
            MimeType TEXT,
            TimeSpentReading INTEGER,
            ___PercentRead INTEGER,
            ReadStatus INTEGER,
            DateLastRead TEXT,
            ___UserID TEXT
        );
        CREATE TABLE Bookmark (
            BookmarkID TEXT,
            VolumeID TEXT,
            Text TEXT,
            Annotation TEXT,
            DateCreated TEXT,
            DateModified TEXT,
            ChapterProgress FLOAT
        );
        """
    )
    conn.executemany(
        "INSERT INTO content VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # an in-progress kepub
            (6, "file:///mnt/onboard/books/dune.kepub", "9780441172719", "Dune", "Frank Herbert",
             "application/x-kobo-epub+zip", 3600, 47, 1, "2026-08-01T12:30:00", "kobo-user-1"),
            # a finished book
            (6, "uuid-store-book-1", None, "The Left Hand of Darkness", "Ursula K. Le Guin",
             "application/epub+zip", 7200, 100, 2, "2026-08-10T09:00:00Z", "kobo-user-1"),
            # Pocket article — must be skipped
            (6, "pocket-article-1", None, "Some Article", "Pocket",
             "application/x-kobo-html+pocket", 0, 0, 0, None, "kobo-user-1"),
            # ad row with empty user — must be skipped
            (6, "ad-1", None, "Recommended For You", "",
             "application/epub+zip", 0, 0, 0, None, ""),
        ],
    )
    conn.executemany(
        "INSERT INTO Bookmark VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("bm-uuid-1", "file:///mnt/onboard/books/dune.kepub",
             "The sleeper must awaken.", "", "2026-07-28T21:15:00", None, 0.42),
            # annotation-with-note; DateCreated missing -> falls back to DateModified
            ("bm-uuid-2", "file:///mnt/onboard/books/dune.kepub",
             "Fear is the mind-killer.", "litany against fear", None, "2026-07-29T08:00:00", 0.5),
            # plain bookmark (no text)
            ("bm-uuid-3", "uuid-store-book-1", None, "", "2026-08-10T09:00:00", None, None),
        ],
    )
    conn.commit()
    conn.close()


def test_iter_books_reads_progress_and_filters_non_books(tmp_path):
    db_file = tmp_path / "KoboReader.sqlite"
    _create_kobo_db(db_file)

    books = list(iter_books(db_file))

    assert len(books) == 2
    by_title = {b.title: b for b in books}

    dune = by_title["Dune"]
    assert dune.percent == 47
    assert dune.read_status == 1
    assert dune.isbn == "9780441172719"
    assert dune.time_spent_seconds == 3600
    assert dune.date_last_read is not None
    assert dune.date_last_read.year == 2026
    assert dune.date_last_read.tzinfo is not None

    lhd = by_title["The Left Hand of Darkness"]
    assert lhd.percent == 100
    assert lhd.read_status == 2
    assert lhd.isbn is None
    # trailing Z timestamp must parse
    assert lhd.date_last_read is not None


def test_iter_bookmarks_kinds_and_date_fallback(tmp_path):
    db_file = tmp_path / "KoboReader.sqlite"
    _create_kobo_db(db_file)

    bookmarks = {b.bookmark_id: b for b in iter_bookmarks(db_file)}

    assert set(bookmarks) == {"bm-uuid-1", "bm-uuid-2", "bm-uuid-3"}

    assert bookmarks["bm-uuid-1"].kind == "highlight"
    assert bookmarks["bm-uuid-1"].text == "The sleeper must awaken."
    assert bookmarks["bm-uuid-1"].chapter_progress == 0.42

    note = bookmarks["bm-uuid-2"]
    assert note.kind == "annotation"
    assert note.annotation == "litany against fear"
    # DateCreated was NULL so creation falls back to DateModified
    assert note.created is not None
    assert note.created.day == 29

    assert bookmarks["bm-uuid-3"].kind == "bookmark"
    assert bookmarks["bm-uuid-3"].chapter_progress is None


def test_parse_kobo_datetime_handles_old_and_new_formats():
    dt = kobo_reader._parse_kobo_datetime("2026-08-01T12:30:00.123456")
    assert dt is not None and dt.microsecond == 0

    assert kobo_reader._parse_kobo_datetime("2026-08-01T12:30:00Z") is not None
    assert kobo_reader._parse_kobo_datetime(None) is None
    assert kobo_reader._parse_kobo_datetime("") is None
    assert kobo_reader._parse_kobo_datetime("not-a-date") is None


def test_missing_database_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        list(iter_books(tmp_path / "nope.sqlite"))


def test_empty_database_yields_nothing(tmp_path):
    db_file = tmp_path / "empty.sqlite"
    sqlite3.connect(db_file).close()

    assert list(iter_books(db_file)) == []
    assert list(iter_bookmarks(db_file)) == []
