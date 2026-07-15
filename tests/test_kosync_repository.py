import pytest

from src.db.kosync_repository import KoSyncRepository
from src.db.models import Base, Book, DatabaseManager, KosyncDocument


@pytest.fixture()
def repository(tmp_path):
    manager = DatabaseManager(str(tmp_path / "kosync_repository.db"))
    Base.metadata.create_all(manager.engine)
    try:
        yield KoSyncRepository(manager)
    finally:
        manager.close()


def _make_book(repository, book_id, kosync_doc_id):
    with repository.get_session() as session:
        book = Book(abs_id=f"abs-{book_id}", title=f"Book {book_id}", kosync_doc_id=kosync_doc_id)
        book.id = book_id
        session.add(book)


def _make_document(repository, document_hash):
    with repository.get_session() as session:
        session.add(KosyncDocument(document_hash=document_hash))


def test_book_with_null_kosync_doc_id_is_excluded(repository):
    _make_book(repository, 1, None)

    assert repository.get_orphaned_kosync_books() == []


def test_book_with_matching_document_is_excluded(repository):
    _make_document(repository, "hash-1")
    _make_book(repository, 1, "hash-1")

    assert repository.get_orphaned_kosync_books() == []


def test_book_without_matching_document_is_included(repository):
    _make_book(repository, 1, "hash-orphan")

    orphaned = repository.get_orphaned_kosync_books()

    assert [b.id for b in orphaned] == [1]
    assert orphaned[0].kosync_doc_id == "hash-orphan"


def test_only_orphaned_books_returned_among_mixed_rows(repository):
    _make_document(repository, "hash-linked")
    _make_book(repository, 1, None)
    _make_book(repository, 2, "hash-linked")
    _make_book(repository, 3, "hash-orphan-a")
    _make_book(repository, 4, "hash-orphan-b")

    orphaned = repository.get_orphaned_kosync_books()

    assert sorted(b.id for b in orphaned) == [3, 4]


def test_no_orphaned_books_returns_empty_list(repository):
    _make_document(repository, "hash-1")
    _make_book(repository, 1, "hash-1")
    _make_book(repository, 2, None)

    assert repository.get_orphaned_kosync_books() == []


def test_returned_books_are_detached_after_session_closes(repository):
    _make_book(repository, 1, "hash-orphan")

    orphaned = repository.get_orphaned_kosync_books()

    # Accessing attributes after the session is closed must not raise
    # (the rows are expunged/detached, with values already loaded).
    assert orphaned[0].id == 1
    assert orphaned[0].kosync_doc_id == "hash-orphan"
