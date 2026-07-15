"""Behavior-characterization tests for BookRepository getters.

These lock in the filtering, ordering, limit, empty-result, and detachment
behavior of the four getter methods consolidated onto ``_query_and_expunge``
in the Stage 16 backend cleanup:

- ``search_books()``
- ``get_latest_job()``
- ``get_books_with_recent_activity()``
- ``get_failed_jobs()``

They also lock in the bulk getter consolidated onto ``_query_and_expunge`` in
the Stage 18 backend cleanup:

- ``get_latest_jobs_bulk()``
"""

import tempfile
from pathlib import Path

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book, Job, State


@pytest.fixture
def db_service():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        service = DatabaseService(str(db_path))
        try:
            yield service
        finally:
            service.db_manager.close()


def _save_book(db_service, abs_id, title, status="active"):
    return db_service.save_book(
        Book(
            abs_id=abs_id,
            title=title,
            ebook_filename=f"{abs_id}.epub",
            kosync_doc_id=f"doc-{abs_id}",
            status=status,
        )
    )


def _save_job(db_service, book, last_attempt, last_error=None):
    return db_service.save_job(
        Job(
            abs_id=book.abs_id,
            book_id=book.id,
            last_attempt=last_attempt,
            last_error=last_error,
        )
    )


def _save_state(db_service, book, client_name, last_updated):
    return db_service.save_state(
        State(
            abs_id=book.abs_id,
            book_id=book.id,
            client_name=client_name,
            last_updated=last_updated,
            percentage=0.5,
        )
    )


# ── search_books ──


def test_search_books_blank_query_returns_empty(db_service):
    _save_book(db_service, "abs-1", "The Hobbit")
    assert db_service.search_books("") == []
    assert db_service.search_books(None) == []
    assert db_service.search_books("   ") == []


def test_search_books_is_case_insensitive_substring(db_service):
    _save_book(db_service, "abs-1", "The Hobbit")
    _save_book(db_service, "abs-2", "Dune")
    results = db_service.search_books("hobb")
    assert {b.title for b in results} == {"The Hobbit"}
    results_upper = db_service.search_books("HOBB")
    assert {b.title for b in results_upper} == {"The Hobbit"}


def test_search_books_preserves_unstripped_query_in_filter(db_service):
    # The truthiness/strip guard does not strip the query passed to ILIKE, so an
    # interior space remains significant and a padded substring with no exact
    # spacing match returns nothing.
    _save_book(db_service, "abs-1", "The Hobbit")
    assert db_service.search_books("the hobbit") != []
    assert db_service.search_books("the  hobbit") == []


def test_search_books_honors_limit(db_service):
    for i in range(5):
        _save_book(db_service, f"abs-{i}", f"Book {i}")
    results = db_service.search_books("Book", limit=2)
    assert len(results) == 2


def test_search_books_no_match_returns_empty(db_service):
    _save_book(db_service, "abs-1", "The Hobbit")
    assert db_service.search_books("nonexistent") == []


def test_search_books_rows_detached_and_usable(db_service):
    _save_book(db_service, "abs-1", "The Hobbit")
    results = db_service.search_books("Hobbit")
    assert results[0].title == "The Hobbit"
    assert results[0].abs_id == "abs-1"


# ── get_latest_job ──


def test_get_latest_job_returns_newest_by_last_attempt_desc(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    _save_job(db_service, book, last_attempt=100.0)
    _save_job(db_service, book, last_attempt=300.0)
    _save_job(db_service, book, last_attempt=200.0)
    latest = db_service.get_latest_job(book.id)
    assert latest.last_attempt == 300.0


def test_get_latest_job_ignores_other_books(db_service):
    book_a = _save_book(db_service, "abs-a", "Book A")
    book_b = _save_book(db_service, "abs-b", "Book B")
    _save_job(db_service, book_a, last_attempt=100.0)
    _save_job(db_service, book_b, last_attempt=500.0)
    latest = db_service.get_latest_job(book_a.id)
    assert latest.book_id == book_a.id
    assert latest.last_attempt == 100.0


def test_get_latest_job_none_when_no_jobs(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    assert db_service.get_latest_job(book.id) is None


def test_get_latest_job_row_detached_and_usable(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    _save_job(db_service, book, last_attempt=100.0)
    latest = db_service.get_latest_job(book.id)
    assert latest.last_attempt == 100.0
    assert latest.book_id == book.id


# ── get_latest_jobs_bulk ──


def test_get_latest_jobs_bulk_empty_input_returns_empty_dict(db_service):
    assert db_service.get_latest_jobs_bulk([]) == {}
    assert db_service.get_latest_jobs_bulk(None) == {}


def test_get_latest_jobs_bulk_returns_latest_per_book_by_max_last_attempt(db_service):
    book_a = _save_book(db_service, "abs-a", "Book A")
    book_b = _save_book(db_service, "abs-b", "Book B")
    _save_job(db_service, book_a, last_attempt=100.0)
    _save_job(db_service, book_a, last_attempt=300.0)
    _save_job(db_service, book_b, last_attempt=200.0)
    result = db_service.get_latest_jobs_bulk([book_a.id, book_b.id])
    assert set(result.keys()) == {book_a.id, book_b.id}
    assert result[book_a.id].last_attempt == 300.0
    assert result[book_b.id].last_attempt == 200.0


def test_get_latest_jobs_bulk_ignores_books_not_requested(db_service):
    book_a = _save_book(db_service, "abs-a", "Book A")
    book_b = _save_book(db_service, "abs-b", "Book B")
    _save_job(db_service, book_a, last_attempt=100.0)
    _save_job(db_service, book_b, last_attempt=500.0)
    result = db_service.get_latest_jobs_bulk([book_a.id])
    assert set(result.keys()) == {book_a.id}
    assert result[book_a.id].last_attempt == 100.0


def test_get_latest_jobs_bulk_empty_when_requested_books_have_no_jobs(db_service):
    book_a = _save_book(db_service, "abs-a", "Book A")
    book_b = _save_book(db_service, "abs-b", "Book B")
    assert db_service.get_latest_jobs_bulk([book_a.id, book_b.id]) == {}


def test_get_latest_jobs_bulk_handles_single_null_last_attempt(db_service):
    # With one job whose last_attempt is NULL, the coalesce join matches the
    # NULL row against its own grouped max (also NULL via coalesce to the
    # epoch sentinel), so that job is returned for the book.
    book = _save_book(db_service, "abs-1", "Book")
    _save_job(db_service, book, last_attempt=None)
    result = db_service.get_latest_jobs_bulk([book.id])
    assert set(result.keys()) == {book.id}
    assert result[book.id].book_id == book.id
    assert result[book.id].last_attempt is None


def test_get_latest_jobs_bulk_rows_detached_and_usable(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    _save_job(db_service, book, last_attempt=100.0)
    result = db_service.get_latest_jobs_bulk([book.id])
    job = result[book.id]
    assert job.book_id == book.id
    assert job.last_attempt == 100.0
    assert job.abs_id == "abs-1"


# ── get_books_with_recent_activity ──


def test_recent_activity_orders_by_latest_state_desc(db_service):
    book_old = _save_book(db_service, "abs-old", "Old Book")
    book_new = _save_book(db_service, "abs-new", "New Book")
    _save_state(db_service, book_old, "koreader", last_updated=100.0)
    _save_state(db_service, book_new, "koreader", last_updated=300.0)
    books = db_service.get_books_with_recent_activity()
    assert [b.title for b in books] == ["New Book", "Old Book"]


def test_recent_activity_uses_max_state_per_book(db_service):
    book_a = _save_book(db_service, "abs-a", "Book A")
    book_b = _save_book(db_service, "abs-b", "Book B")
    # Book A's most-recent state beats Book B's single state.
    _save_state(db_service, book_a, "koreader", last_updated=100.0)
    _save_state(db_service, book_a, "calibre", last_updated=400.0)
    _save_state(db_service, book_b, "koreader", last_updated=300.0)
    books = db_service.get_books_with_recent_activity()
    assert [b.title for b in books] == ["Book A", "Book B"]


def test_recent_activity_honors_limit(db_service):
    for i in range(5):
        book = _save_book(db_service, f"abs-{i}", f"Book {i}")
        _save_state(db_service, book, "koreader", last_updated=float(i))
    books = db_service.get_books_with_recent_activity(limit=2)
    assert len(books) == 2


def test_recent_activity_empty_when_no_states(db_service):
    _save_book(db_service, "abs-1", "Book")
    assert db_service.get_books_with_recent_activity() == []


def test_recent_activity_rows_detached_and_usable(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    _save_state(db_service, book, "koreader", last_updated=100.0)
    books = db_service.get_books_with_recent_activity()
    assert books[0].title == "Book"
    assert books[0].abs_id == "abs-1"


# ── get_failed_jobs ──


def test_get_failed_jobs_filters_non_null_error(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    _save_job(db_service, book, last_attempt=100.0, last_error=None)
    _save_job(db_service, book, last_attempt=200.0, last_error="boom")
    failed = db_service.get_failed_jobs()
    assert {j.last_error for j in failed} == {"boom"}


def test_get_failed_jobs_orders_by_last_attempt_desc(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    _save_job(db_service, book, last_attempt=100.0, last_error="first")
    _save_job(db_service, book, last_attempt=300.0, last_error="third")
    _save_job(db_service, book, last_attempt=200.0, last_error="second")
    failed = db_service.get_failed_jobs()
    assert [j.last_error for j in failed] == ["third", "second", "first"]


def test_get_failed_jobs_honors_limit(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    for i in range(5):
        _save_job(db_service, book, last_attempt=float(i), last_error=f"err-{i}")
    failed = db_service.get_failed_jobs(limit=2)
    assert len(failed) == 2


def test_get_failed_jobs_empty_when_no_failures(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    _save_job(db_service, book, last_attempt=100.0, last_error=None)
    assert db_service.get_failed_jobs() == []


def test_get_failed_jobs_rows_detached_and_usable(db_service):
    book = _save_book(db_service, "abs-1", "Book")
    _save_job(db_service, book, last_attempt=100.0, last_error="boom")
    failed = db_service.get_failed_jobs()
    assert failed[0].last_error == "boom"
    assert failed[0].book_id == book.id
