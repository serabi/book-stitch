"""Tests for src/db/kobo_repository.py via DatabaseService delegation."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from src.db.database_service import DatabaseService
from src.db.models import Book


def _make_db(temp_dir):
    return DatabaseService(str(Path(temp_dir) / "test.db"))


def test_save_and_update_kobo_books():
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _make_db(temp_dir)

        result = db.save_kobo_books(
            [
                {
                    "content_id": "cid-1",
                    "title": "Dune",
                    "author": "Frank Herbert",
                    "isbn": "9780441172719",
                    "percent": 12,
                    "read_status": 1,
                }
            ]
        )
        assert result == {"saved": 1, "updated": 0}

        # upsert updates progress in place
        result = db.save_kobo_books([{"content_id": "cid-1", "percent": 58}])
        assert result == {"saved": 0, "updated": 1}

        book = db.get_kobo_book("cid-1")
        assert book.percent == 58
        assert book.title == "Dune"  # earlier fields preserved
        assert book.matched_book_id is None

        db.db_manager.close()


def test_kobo_book_hidden_filter():
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _make_db(temp_dir)
        db.save_kobo_books([{"content_id": "cid-1", "title": "A"}, {"content_id": "cid-2", "title": "B"}])

        db.set_kobo_books_hidden(["cid-2"], True)

        assert [b.content_id for b in db.get_kobo_books()] == ["cid-1"]
        assert len(db.get_kobo_books(include_hidden=True)) == 2

        db.db_manager.close()


def test_kobo_book_matching_and_unlink_cascades_to_bookmarks():
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _make_db(temp_dir)
        book = db.save_book(Book(abs_id="abs-1", title="Dune", status="active"))
        db.save_kobo_books([{"content_id": "cid-1", "title": "Dune"}])
        db.save_kobo_bookmarks(
            [
                {"bookmark_id": "bm-1", "content_id": "cid-1", "kind": "highlight", "text": "quote"},
                {"bookmark_id": "bm-2", "content_id": "other", "kind": "highlight", "text": "unrelated"},
            ]
        )

        db.set_kobo_book_match("cid-1", book.id)
        db.link_kobo_bookmarks_by_content_id("cid-1", book.id)

        assert db.get_kobo_linked_book_ids() == {book.id}
        assert len(db.get_kobo_bookmarks_for_book_by_book_id(book.id)) == 1

        db.unlink_kobo_book("cid-1")

        assert db.get_kobo_book("cid-1").matched_book_id is None
        assert db.get_kobo_bookmarks_for_book_by_book_id(book.id) == []
        # unrelated bookmark untouched
        unrelated = [b for b in db.get_kobo_bookmarks() if b.bookmark_id == "bm-2"]
        assert unrelated and unrelated[0].matched_book_id is None

        db.db_manager.close()


def test_kobo_bookmark_dedupe_and_update():
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _make_db(temp_dir)

        first = db.save_kobo_bookmarks(
            [{"bookmark_id": "bm-1", "content_id": "cid-1", "kind": "highlight", "text": "v1", "annotation": ""}]
        )
        assert first["saved"] == 1

        # same id re-ingested with an added note -> updates, doesn't duplicate
        second = db.save_kobo_bookmarks(
            [
                {"bookmark_id": "bm-1", "content_id": "cid-1", "kind": "annotation", "text": "v1", "annotation": "thought"},
                {"bookmark_id": "bm-1", "content_id": "cid-1", "kind": "highlight", "text": "dup-batch"},
            ]
        )
        assert second["saved"] == 0

        bookmarks = db.get_kobo_bookmarks_for_content_id("cid-1")
        assert len(bookmarks) == 1
        assert bookmarks[0].annotation == "thought"
        assert bookmarks[0].kind == "annotation"

        db.db_manager.close()


def test_kobo_bookmark_highlighted_at_persists():
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _make_db(temp_dir)
        when = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
        db.save_kobo_bookmarks(
            [{"bookmark_id": "bm-1", "content_id": "cid-1", "kind": "highlight", "text": "q", "highlighted_at": when}]
        )

        bm = db.get_kobo_bookmarks_for_content_id("cid-1")[0]
        assert bm.highlighted_at is not None
        assert bm.highlighted_at.year == 2026

        db.db_manager.close()
