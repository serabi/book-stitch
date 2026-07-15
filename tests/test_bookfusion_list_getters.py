"""Behavior-characterization tests for BookFusion list getters.

These lock in the filtering, ordering, empty-result, and detachment behavior of
the four list-query methods consolidated onto ``_query_and_expunge`` in the
Stage 13 backend cleanup:

- ``get_bookfusion_highlights()``
- ``get_unmatched_bookfusion_highlights()``
- ``get_bookfusion_highlights_for_book_by_book_id()``
- ``get_bookfusion_books_by_book_id()``
"""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book, BookfusionBook


@pytest.fixture
def db_service():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        service = DatabaseService(str(db_path))
        try:
            yield service
        finally:
            service.db_manager.close()


def _save_book(db_service, abs_id, title):
    return db_service.save_book(
        Book(
            abs_id=abs_id,
            title=title,
            ebook_filename=f"{abs_id}.epub",
            kosync_doc_id=f"doc-{abs_id}",
            status="active",
        )
    )


def _highlight(highlight_id, book_title, bookfusion_book_id="bf-1", highlighted_at=None):
    return {
        "bookfusion_book_id": bookfusion_book_id,
        "highlight_id": highlight_id,
        "content": f"content-{highlight_id}",
        "quote_text": f"quote-{highlight_id}",
        "book_title": book_title,
        "chapter_heading": "Chapter 1",
        "highlighted_at": highlighted_at,
    }


# ── get_bookfusion_highlights ──


def test_get_bookfusion_highlights_returns_all(db_service):
    db_service.save_bookfusion_highlights(
        [
            _highlight("hl-1", "Zebra"),
            _highlight("hl-2", "Apple"),
        ]
    )
    highlights = db_service.get_bookfusion_highlights()
    assert {h.highlight_id for h in highlights} == {"hl-1", "hl-2"}


def test_get_bookfusion_highlights_orders_by_book_title_then_id(db_service):
    # Two share a title to force the secondary id ordering to matter.
    db_service.save_bookfusion_highlights(
        [
            _highlight("hl-1", "Mango"),
            _highlight("hl-2", "Apple"),
            _highlight("hl-3", "Apple"),
        ]
    )
    highlights = db_service.get_bookfusion_highlights()
    titles = [h.book_title for h in highlights]
    assert titles == ["Apple", "Apple", "Mango"]
    apple_ids = [h.id for h in highlights if h.book_title == "Apple"]
    assert apple_ids == sorted(apple_ids)


def test_get_bookfusion_highlights_empty(db_service):
    assert db_service.get_bookfusion_highlights() == []


def test_get_bookfusion_highlights_rows_detached_and_usable(db_service):
    db_service.save_bookfusion_highlights([_highlight("hl-1", "Apple")])
    highlights = db_service.get_bookfusion_highlights()
    # Accessing attributes after the session closed must not raise
    # DetachedInstanceError; rows are expunged, so their loaded column state
    # remains readable.
    assert highlights[0].book_title == "Apple"
    assert highlights[0].content == "content-hl-1"


# ── get_unmatched_bookfusion_highlights ──


def test_get_unmatched_bookfusion_highlights_filters_null_match(db_service):
    book = _save_book(db_service, "abs-1", "Linked Book")
    db_service.save_bookfusion_highlights(
        [
            _highlight("hl-1", "Apple", bookfusion_book_id="bf-1"),
            _highlight("hl-2", "Banana", bookfusion_book_id="bf-2"),
        ]
    )
    db_service.link_bookfusion_highlights_by_book_id("bf-1", book.id)

    unmatched = db_service.get_unmatched_bookfusion_highlights()
    assert {h.highlight_id for h in unmatched} == {"hl-2"}
    assert all(h.matched_book_id is None for h in unmatched)


def test_get_unmatched_bookfusion_highlights_orders_by_book_title_then_id(db_service):
    db_service.save_bookfusion_highlights(
        [
            _highlight("hl-1", "Mango"),
            _highlight("hl-2", "Apple"),
            _highlight("hl-3", "Apple"),
        ]
    )
    unmatched = db_service.get_unmatched_bookfusion_highlights()
    titles = [h.book_title for h in unmatched]
    assert titles == ["Apple", "Apple", "Mango"]
    apple_ids = [h.id for h in unmatched if h.book_title == "Apple"]
    assert apple_ids == sorted(apple_ids)


def test_get_unmatched_bookfusion_highlights_empty(db_service):
    assert db_service.get_unmatched_bookfusion_highlights() == []


# ── get_bookfusion_highlights_for_book_by_book_id ──


def test_get_highlights_for_book_filters_by_matched_book_id(db_service):
    book_a = _save_book(db_service, "abs-a", "Book A")
    book_b = _save_book(db_service, "abs-b", "Book B")
    db_service.save_bookfusion_highlights(
        [
            _highlight("hl-1", "Apple", bookfusion_book_id="bf-a"),
            _highlight("hl-2", "Banana", bookfusion_book_id="bf-b"),
        ]
    )
    db_service.link_bookfusion_highlights_by_book_id("bf-a", book_a.id)
    db_service.link_bookfusion_highlights_by_book_id("bf-b", book_b.id)

    for_a = db_service.get_bookfusion_highlights_for_book_by_book_id(book_a.id)
    assert {h.highlight_id for h in for_a} == {"hl-1"}
    assert all(h.matched_book_id == book_a.id for h in for_a)


def test_get_highlights_for_book_orders_by_highlighted_at_desc_nulls_last_then_id(db_service):
    book = _save_book(db_service, "abs-a", "Book A")
    db_service.save_bookfusion_highlights(
        [
            _highlight("hl-old", "Apple", bookfusion_book_id="bf-a", highlighted_at=datetime(2020, 1, 1)),
            _highlight("hl-new", "Apple", bookfusion_book_id="bf-a", highlighted_at=datetime(2024, 1, 1)),
            _highlight("hl-null-1", "Apple", bookfusion_book_id="bf-a", highlighted_at=None),
            _highlight("hl-null-2", "Apple", bookfusion_book_id="bf-a", highlighted_at=None),
        ]
    )
    db_service.link_bookfusion_highlights_by_book_id("bf-a", book.id)

    result = db_service.get_bookfusion_highlights_for_book_by_book_id(book.id)
    order = [h.highlight_id for h in result]
    # Newest first, then older dated, then the null-dated rows last in id order.
    assert order[0] == "hl-new"
    assert order[1] == "hl-old"
    null_tail = order[2:]
    assert set(null_tail) == {"hl-null-1", "hl-null-2"}
    null_ids = [h.id for h in result if h.highlighted_at is None]
    assert null_ids == sorted(null_ids)


def test_get_highlights_for_book_empty(db_service):
    book = _save_book(db_service, "abs-a", "Book A")
    assert db_service.get_bookfusion_highlights_for_book_by_book_id(book.id) == []


# ── get_bookfusion_books_by_book_id ──


def test_get_bookfusion_books_by_book_id_filters_by_matched_book_id(db_service):
    book_a = _save_book(db_service, "abs-a", "Book A")
    book_b = _save_book(db_service, "abs-b", "Book B")
    db_service.save_bookfusion_book(
        BookfusionBook(bookfusion_id="bf-a", title="Catalog A", matched_book_id=book_a.id)
    )
    db_service.save_bookfusion_book(
        BookfusionBook(bookfusion_id="bf-b", title="Catalog B", matched_book_id=book_b.id)
    )

    for_a = db_service.get_bookfusion_books_by_book_id(book_a.id)
    assert {b.bookfusion_id for b in for_a} == {"bf-a"}
    assert all(b.matched_book_id == book_a.id for b in for_a)


def test_get_bookfusion_books_by_book_id_returns_all_matches(db_service):
    book = _save_book(db_service, "abs-a", "Book A")
    db_service.save_bookfusion_book(
        BookfusionBook(bookfusion_id="bf-a1", title="Catalog A1", matched_book_id=book.id)
    )
    db_service.save_bookfusion_book(
        BookfusionBook(bookfusion_id="bf-a2", title="Catalog A2", matched_book_id=book.id)
    )
    result = db_service.get_bookfusion_books_by_book_id(book.id)
    assert {b.bookfusion_id for b in result} == {"bf-a1", "bf-a2"}


def test_get_bookfusion_books_by_book_id_empty(db_service):
    book = _save_book(db_service, "abs-a", "Book A")
    assert db_service.get_bookfusion_books_by_book_id(book.id) == []


def test_get_bookfusion_books_by_book_id_rows_detached_and_usable(db_service):
    book = _save_book(db_service, "abs-a", "Book A")
    db_service.save_bookfusion_book(
        BookfusionBook(bookfusion_id="bf-a", title="Catalog A", matched_book_id=book.id)
    )
    result = db_service.get_bookfusion_books_by_book_id(book.id)
    assert result[0].title == "Catalog A"
