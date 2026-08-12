from datetime import datetime

import pytest

from src.db.models import Base, Book, DatabaseManager, StorytellerSubmission
from src.db.storyteller_repository import StorytellerRepository


@pytest.fixture()
def repository(tmp_path):
    manager = DatabaseManager(str(tmp_path / "storyteller_repository.db"))
    Base.metadata.create_all(manager.engine)
    try:
        yield StorytellerRepository(manager)
    finally:
        manager.close()


def _make_book(repository, book_id):
    with repository.get_session() as session:
        book = Book(abs_id=f"abs-{book_id}", title=f"Book {book_id}")
        book.id = book_id
        session.add(book)


def _insert_submission(repository, book_id, status, submitted_at):
    with repository.get_session() as session:
        submission = StorytellerSubmission(abs_id=f"abs-{book_id}", status=status, book_id=book_id)
        submission.submitted_at = submitted_at
        session.add(submission)


def _ts(day):
    # SQLite stores naive datetimes; use naive values so equality checks hold
    # after the row round-trips through the database.
    return datetime(2026, 1, day)


def test_active_getter_returns_newest_queued_or_processing_row(repository):
    _make_book(repository, 1)
    _insert_submission(repository, 1, "queued", _ts(1))
    _insert_submission(repository, 1, "processing", _ts(3))
    _insert_submission(repository, 1, "queued", _ts(2))

    sub = repository.get_active_storyteller_submission_by_book_id(1)

    assert sub is not None
    assert sub.status == "processing"
    assert sub.submitted_at == _ts(3)


def test_active_getter_ignores_terminal_statuses(repository):
    _make_book(repository, 1)
    _insert_submission(repository, 1, "ready", _ts(5))
    _insert_submission(repository, 1, "failed", _ts(4))
    _insert_submission(repository, 1, "superseded", _ts(3))
    _insert_submission(repository, 1, "queued", _ts(1))

    sub = repository.get_active_storyteller_submission_by_book_id(1)

    assert sub is not None
    assert sub.status == "queued"


def test_active_getter_returns_none_when_only_terminal_rows_exist(repository):
    _make_book(repository, 1)
    _insert_submission(repository, 1, "ready", _ts(2))
    _insert_submission(repository, 1, "failed", _ts(1))

    assert repository.get_active_storyteller_submission_by_book_id(1) is None


def test_active_getter_returns_none_for_missing_book(repository):
    assert repository.get_active_storyteller_submission_by_book_id(999) is None


def test_latest_getter_returns_newest_row_regardless_of_status(repository):
    _make_book(repository, 1)
    _insert_submission(repository, 1, "queued", _ts(1))
    _insert_submission(repository, 1, "ready", _ts(4))
    _insert_submission(repository, 1, "processing", _ts(2))

    sub = repository.get_storyteller_submission_by_book_id(1)

    assert sub is not None
    assert sub.status == "ready"
    assert sub.submitted_at == _ts(4)


def test_latest_getter_returns_none_for_missing_book(repository):
    assert repository.get_storyteller_submission_by_book_id(999) is None


def test_getters_filter_by_book_id_exactly(repository):
    _make_book(repository, 1)
    _make_book(repository, 2)
    _insert_submission(repository, 1, "queued", _ts(1))
    _insert_submission(repository, 2, "processing", _ts(5))

    active = repository.get_active_storyteller_submission_by_book_id(1)
    latest = repository.get_storyteller_submission_by_book_id(1)

    assert active is not None and active.book_id == 1
    assert latest is not None and latest.book_id == 1


def test_returned_submission_is_detached_after_session_closes(repository):
    _make_book(repository, 1)
    _insert_submission(repository, 1, "processing", _ts(1))

    sub = repository.get_active_storyteller_submission_by_book_id(1)

    # Accessing attributes after the session is closed must not raise
    # (the row is expunged/detached, with values already loaded).
    assert sub.book_id == 1
    assert sub.status == "processing"


def test_bulk_latest_returns_empty_dict_when_no_submissions(repository):
    assert repository.get_all_storyteller_submissions_latest() == {}


def test_bulk_latest_returns_newest_submission_per_book(repository):
    _make_book(repository, 1)
    _make_book(repository, 2)
    _insert_submission(repository, 1, "queued", _ts(1))
    _insert_submission(repository, 1, "ready", _ts(4))
    _insert_submission(repository, 2, "processing", _ts(2))
    _insert_submission(repository, 2, "failed", _ts(3))

    latest = repository.get_all_storyteller_submissions_latest()

    assert set(latest) == {1, 2}
    assert latest[1].status == "ready"
    assert latest[1].submitted_at == _ts(4)
    assert latest[2].status == "failed"
    assert latest[2].submitted_at == _ts(3)


def test_bulk_latest_keeps_books_isolated(repository):
    _make_book(repository, 1)
    _make_book(repository, 2)
    _insert_submission(repository, 1, "ready", _ts(2))
    _insert_submission(repository, 2, "queued", _ts(5))

    latest = repository.get_all_storyteller_submissions_latest()

    assert latest[1].book_id == 1
    assert latest[1].status == "ready"
    assert latest[2].book_id == 2
    assert latest[2].status == "queued"


def test_bulk_latest_rows_are_detached_after_session_closes(repository):
    _make_book(repository, 1)
    _insert_submission(repository, 1, "processing", _ts(1))

    latest = repository.get_all_storyteller_submissions_latest()

    # Accessing attributes after the session is closed must not raise
    # (the rows are expunged/detached, with values already loaded).
    assert latest[1].book_id == 1
    assert latest[1].status == "processing"
