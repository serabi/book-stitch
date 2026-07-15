"""Behavior-characterization tests for BookRepository object-return mutations.

These lock in the filtering, mutation, and detachment behavior of the two
mutation methods consolidated onto the private ``_mutate_first_and_detach``
helper in the Stage 17 backend cleanup:

- ``update_book_metadata_overrides()``
- ``update_latest_job()``
"""

import tempfile
from pathlib import Path

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Book, Job


@pytest.fixture
def db_service():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        service = DatabaseService(str(db_path))
        try:
            yield service
        finally:
            service.db_manager.close()


def _save_book(db_service, abs_id="book-1", title="Title", author="Author"):
    return db_service.save_book(Book(abs_id=abs_id, title=title, author=author))


def _save_job(db_service, book, last_attempt, **kwargs):
    return db_service.save_job(
        Job(abs_id=book.abs_id, book_id=book.id, last_attempt=last_attempt, **kwargs)
    )


# ── update_book_metadata_overrides ──


def test_updates_title_and_author_overrides_independently(db_service):
    book = _save_book(db_service)

    updated = db_service.update_book_metadata_overrides(
        book.id, title_override="Clean Title", author_override="Clean Author"
    )

    assert updated.title_override == "Clean Title"
    assert updated.author_override == "Clean Author"

    title_only = db_service.update_book_metadata_overrides(book.id, title_override="New Title")
    assert title_only.title_override == "New Title"
    assert title_only.author_override == "Clean Author"


def test_unset_preserves_existing_override(db_service):
    book = _save_book(db_service)
    db_service.update_book_metadata_overrides(
        book.id, title_override="Keep Title", author_override="Keep Author"
    )

    updated = db_service.update_book_metadata_overrides(book.id, author_override="Changed Author")

    assert updated.title_override == "Keep Title"
    assert updated.author_override == "Changed Author"


def test_none_clears_only_specified_override(db_service):
    book = _save_book(db_service)
    db_service.update_book_metadata_overrides(
        book.id, title_override="Title Override", author_override="Author Override"
    )

    updated = db_service.update_book_metadata_overrides(book.id, title_override=None)

    assert updated.title_override is None
    assert updated.author_override == "Author Override"


def test_empty_string_clears_only_specified_override(db_service):
    book = _save_book(db_service)
    db_service.update_book_metadata_overrides(
        book.id, title_override="Title Override", author_override="Author Override"
    )

    updated = db_service.update_book_metadata_overrides(book.id, author_override="")

    assert updated.title_override == "Title Override"
    assert updated.author_override is None


def test_missing_book_returns_none(db_service):
    assert db_service.update_book_metadata_overrides(99999, title_override="Missing") is None


def test_no_argument_call_returns_unchanged_detached_book(db_service):
    book = _save_book(db_service)
    db_service.update_book_metadata_overrides(book.id, title_override="Existing")

    updated = db_service.update_book_metadata_overrides(book.id)

    assert updated.id == book.id
    assert updated.title_override == "Existing"


def test_returned_book_is_detached_and_usable(db_service):
    book = _save_book(db_service, title="Imported")
    updated = db_service.update_book_metadata_overrides(book.id, title_override="Override")

    assert updated.title == "Imported"
    assert updated.title_override == "Override"
    assert updated.display_title == "Override"


# ── update_latest_job ──


def test_updates_only_newest_job_by_last_attempt_desc(db_service):
    book = _save_book(db_service)
    _save_job(db_service, book, last_attempt=100.0, progress=0.1)
    newest = _save_job(db_service, book, last_attempt=200.0, progress=0.2)

    updated = db_service.update_latest_job(book.id, progress=0.9)

    assert updated.id == newest.id
    assert updated.progress == 0.9

    older = db_service.get_jobs_for_book(book.id)
    older_by_ts = {job.last_attempt: job.progress for job in older}
    assert older_by_ts[100.0] == 0.1


def test_ignores_jobs_for_other_books(db_service):
    book = _save_book(db_service, abs_id="book-a")
    other = _save_book(db_service, abs_id="book-b")
    _save_job(db_service, other, last_attempt=500.0, progress=0.0)
    target = _save_job(db_service, book, last_attempt=100.0, progress=0.0)

    updated = db_service.update_latest_job(book.id, progress=0.5)

    assert updated.id == target.id
    assert updated.progress == 0.5


def test_writes_allowed_fields_including_none(db_service):
    book = _save_book(db_service)
    _save_job(db_service, book, last_attempt=100.0, last_error="boom")

    updated = db_service.update_latest_job(book.id, last_error=None, retry_count=3)

    assert updated.last_error is None
    assert updated.retry_count == 3


def test_unknown_kwargs_ignored_but_warning_logged(db_service, caplog):
    book = _save_book(db_service)
    job = _save_job(db_service, book, last_attempt=100.0, progress=0.0)

    with caplog.at_level("WARNING"):
        updated = db_service.update_latest_job(book.id, progress=0.7, bogus_field="x")

    assert updated.progress == 0.7
    assert not hasattr(updated, "bogus_field")
    assert f"update_latest_job: unknown attribute 'bogus_field' for job {job.id}" in caplog.text


def test_unknown_only_kwargs_return_unchanged_detached_latest_job(db_service):
    book = _save_book(db_service)
    _save_job(db_service, book, last_attempt=100.0, progress=0.42)

    updated = db_service.update_latest_job(book.id, nope="x")

    assert updated.progress == 0.42


def test_empty_kwargs_return_unchanged_detached_latest_job(db_service):
    book = _save_book(db_service)
    _save_job(db_service, book, last_attempt=100.0, progress=0.33)

    updated = db_service.update_latest_job(book.id)

    assert updated.progress == 0.33


def test_missing_job_returns_none(db_service):
    book = _save_book(db_service)

    assert db_service.update_latest_job(book.id, progress=0.5) is None


def test_returned_job_is_detached_and_usable(db_service):
    book = _save_book(db_service)
    _save_job(db_service, book, last_attempt=100.0, progress=0.0)

    updated = db_service.update_latest_job(book.id, progress=0.6)

    assert updated.book_id == book.id
    assert updated.progress == 0.6
