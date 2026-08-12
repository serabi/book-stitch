import json

import pytest

from src.db.grimmory_repository import GrimmoryRepository
from src.db.models import Base, DatabaseManager, GrimmoryBook


@pytest.fixture()
def repository(tmp_path):
    manager = DatabaseManager(str(tmp_path / "grimmory_repository.db"))
    Base.metadata.create_all(manager.engine)
    try:
        yield GrimmoryRepository(manager)
    finally:
        manager.close()


def test_save_grimmory_book_inserts_new_row(repository):
    saved = repository.save_grimmory_book(
        GrimmoryBook(
            filename="new.epub",
            title="New Title",
            authors="New Author",
            raw_metadata=json.dumps({"id": "1"}),
            server_id="test",
        )
    )

    assert saved.id is not None
    fetched = repository.get_grimmory_book("new.epub", server_id="test")
    assert fetched.title == "New Title"
    assert fetched.authors == "New Author"
    assert fetched.raw_metadata_dict["id"] == "1"


def test_save_grimmory_book_updates_existing_identity(repository):
    repository.save_grimmory_book(
        GrimmoryBook(
            filename="same.epub",
            title="Original Title",
            authors="Original Author",
            raw_metadata=json.dumps({"id": "orig"}),
            server_id="test",
        )
    )

    updated = repository.save_grimmory_book(
        GrimmoryBook(
            filename="same.epub",
            title="Updated Title",
            authors="Updated Author",
            raw_metadata=json.dumps({"id": "updated"}),
            server_id="test",
        )
    )

    assert updated.title == "Updated Title"
    assert updated.authors == "Updated Author"
    assert updated.raw_metadata_dict["id"] == "updated"


def test_save_grimmory_book_no_duplicate_on_repeated_save(repository):
    for _ in range(3):
        repository.save_grimmory_book(
            GrimmoryBook(
                filename="repeat.epub",
                title="Repeat Title",
                authors="Repeat Author",
                raw_metadata=json.dumps({"id": "repeat"}),
                server_id="test",
            )
        )

    rows = repository.get_all_grimmory_books(server_id="test")
    matching = [r for r in rows if r.filename == "repeat.epub"]
    assert len(matching) == 1


def test_get_all_grimmory_books_none_returns_all_servers(repository):
    repository.save_grimmory_book(
        GrimmoryBook(filename="a.epub", title="A", server_id="server-a")
    )
    repository.save_grimmory_book(
        GrimmoryBook(filename="b.epub", title="B", server_id="server-b")
    )

    rows = repository.get_all_grimmory_books()

    assert sorted((r.filename, r.server_id) for r in rows) == [
        ("a.epub", "server-a"),
        ("b.epub", "server-b"),
    ]


def test_get_all_grimmory_books_filters_by_server_id(repository):
    repository.save_grimmory_book(
        GrimmoryBook(filename="a.epub", title="A", server_id="server-a")
    )
    repository.save_grimmory_book(
        GrimmoryBook(filename="b.epub", title="B", server_id="server-b")
    )

    rows = repository.get_all_grimmory_books(server_id="server-a")

    assert [r.filename for r in rows] == ["a.epub"]
    assert all(r.server_id == "server-a" for r in rows)


def test_get_all_grimmory_books_empty_string_filters_not_returns_all(repository):
    repository.save_grimmory_book(
        GrimmoryBook(filename="empty.epub", title="Empty", server_id="")
    )
    repository.save_grimmory_book(
        GrimmoryBook(filename="named.epub", title="Named", server_id="server-a")
    )

    rows = repository.get_all_grimmory_books(server_id="")

    assert [r.filename for r in rows] == ["empty.epub"]
    assert all(r.server_id == "" for r in rows)


def test_get_all_grimmory_books_missing_server_returns_empty(repository):
    repository.save_grimmory_book(
        GrimmoryBook(filename="a.epub", title="A", server_id="server-a")
    )

    assert repository.get_all_grimmory_books(server_id="absent") == []


def test_get_all_grimmory_books_rows_detached_after_session_close(repository):
    repository.save_grimmory_book(
        GrimmoryBook(
            filename="detached.epub",
            title="Detached Title",
            authors="Detached Author",
            raw_metadata=json.dumps({"id": "detached"}),
            server_id="server-a",
        )
    )

    rows = repository.get_all_grimmory_books(server_id="server-a")

    assert len(rows) == 1
    row = rows[0]
    assert row.title == "Detached Title"
    assert row.authors == "Detached Author"
    assert row.raw_metadata_dict["id"] == "detached"


def test_save_grimmory_book_distinguishes_by_server_id(repository):
    repository.save_grimmory_book(
        GrimmoryBook(filename="shared.epub", title="Server A", server_id="a")
    )
    repository.save_grimmory_book(
        GrimmoryBook(filename="shared.epub", title="Server B", server_id="b")
    )

    assert repository.get_grimmory_book("shared.epub", server_id="a").title == "Server A"
    assert repository.get_grimmory_book("shared.epub", server_id="b").title == "Server B"


def test_save_grimmory_book_updates_preexisting_row(repository):
    """A row already committed by another writer is updated in place rather
    than duplicated when save sees the same identity."""
    with repository.get_session() as session:
        session.add(
            GrimmoryBook(
                filename="preexisting.epub",
                title="Concurrent Title",
                authors="Concurrent Author",
                raw_metadata=json.dumps({"id": "concurrent"}),
                server_id="test",
            )
        )

    saved = repository.save_grimmory_book(
        GrimmoryBook(
            filename="preexisting.epub",
            title="Winner Title",
            authors="Winner Author",
            raw_metadata=json.dumps({"id": "winner"}),
            server_id="test",
        )
    )

    assert saved.title == "Winner Title"
    rows = repository.get_all_grimmory_books(server_id="test")
    matching = [r for r in rows if r.filename == "preexisting.epub"]
    assert len(matching) == 1
    assert matching[0].title == "Winner Title"


def test_save_grimmory_book_recovers_from_integrity_race(repository, monkeypatch):
    """If a unique-constraint conflict fires on insert (a writer committed the
    same identity after our lookup), save must roll back, re-find the row, and
    update it without raising or duplicating."""
    inserted_concurrently = {"done": False}

    def insert_conflicting_row():
        with repository.get_session() as session:
            session.add(
                GrimmoryBook(
                    filename="integrity.epub",
                    title="Sneaky Title",
                    authors="Sneaky Author",
                    raw_metadata=json.dumps({"id": "sneaky"}),
                    server_id="test",
                )
            )

    # Patch Query.first so the first lookup returns None (row not yet visible),
    # then commit a conflicting row so the subsequent insert raises IntegrityError.
    from sqlalchemy.orm import Query

    original_first = Query.first
    call_state = {"n": 0}

    def patched_first(self):
        call_state["n"] += 1
        if call_state["n"] == 1 and not inserted_concurrently["done"]:
            inserted_concurrently["done"] = True
            insert_conflicting_row()
            return None
        return original_first(self)

    monkeypatch.setattr(Query, "first", patched_first)

    saved = repository.save_grimmory_book(
        GrimmoryBook(
            filename="integrity.epub",
            title="Winner Title",
            authors="Winner Author",
            raw_metadata=json.dumps({"id": "winner"}),
            server_id="test",
        )
    )

    monkeypatch.undo()

    assert saved.title == "Winner Title"
    rows = repository.get_all_grimmory_books(server_id="test")
    matching = [r for r in rows if r.filename == "integrity.epub"]
    assert len(matching) == 1
    assert matching[0].title == "Winner Title"


def test_replace_grimmory_book_filename_in_one_repository_call(repository):
    repository.save_grimmory_book(
        GrimmoryBook(
            filename="Mixed_Case.epub",
            title="Mixed Case Book",
            authors="Author",
            raw_metadata=json.dumps({"id": "777", "fileName": "Mixed_Case.epub"}),
            server_id="test",
        )
    )

    saved = repository.replace_grimmory_book_filename(
        "Mixed_Case.epub",
        GrimmoryBook(
            filename="mixed_case.epub",
            title="Mixed Case Book",
            authors="Author",
            raw_metadata=json.dumps({"id": "777", "fileName": "Mixed_Case.epub"}),
            server_id="test",
        ),
    )

    assert saved.filename == "mixed_case.epub"
    assert repository.get_grimmory_book("Mixed_Case.epub", server_id="test") is None
    assert repository.get_grimmory_book("mixed_case.epub", server_id="test").title == "Mixed Case Book"


def test_replace_grimmory_book_filename_keeps_distinct_case_collision_rows(repository):
    repository.save_grimmory_book(
        GrimmoryBook(
            filename="Mixed_Case.epub",
            title="Legacy Title",
            authors="Old Author",
            raw_metadata=json.dumps({"id": "old"}),
            server_id="test",
        )
    )
    repository.save_grimmory_book(
        GrimmoryBook(
            filename="mixed_case.epub",
            title="Existing Title",
            authors="Existing Author",
            raw_metadata=json.dumps({"id": "existing"}),
            server_id="test",
        )
    )

    saved = repository.replace_grimmory_book_filename(
        "Mixed_Case.epub",
        GrimmoryBook(
            filename="mixed_case.epub",
            title="Fresh Title",
            authors="Fresh Author",
            raw_metadata=json.dumps({"id": "fresh"}),
            server_id="test",
        ),
    )

    assert saved.filename == "mixed_case.epub"
    legacy = repository.get_grimmory_book("Mixed_Case.epub", server_id="test")
    assert legacy.title == "Legacy Title"
    assert legacy.authors == "Old Author"
    assert legacy.raw_metadata_dict["id"] == "old"

    normalized = repository.get_grimmory_book("mixed_case.epub", server_id="test")
    assert normalized.title == "Existing Title"
    assert normalized.authors == "Existing Author"
    assert normalized.raw_metadata_dict["id"] == "existing"
