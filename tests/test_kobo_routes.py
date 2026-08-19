"""Tests for the Kobo blueprint: device-book APIs, hide endpoint, and the
/kobo-books management page (plus its settings tab presence)."""

from pathlib import Path
from unittest.mock import Mock


def _make_kobo_book(
    content_id="cid-1",
    title="Dune",
    author="Frank Herbert",
    percent=47,
    read_status=1,
    date_last_read=None,
    time_spent_seconds=3600,
    matched_book_id=None,
    hidden=False,
):
    b = Mock()
    b.content_id = content_id
    b.title = title
    b.author = author
    b.isbn = None
    b.percent = percent
    b.read_status = read_status
    b.date_last_read = date_last_read
    b.time_spent_seconds = time_spent_seconds
    b.matched_book_id = matched_book_id
    b.hidden = hidden
    return b


def _make_library_book(book_id=5, title="Dune"):
    book = Mock()
    book.id = book_id
    book.title = title
    return book


def _configure_kobo(mock_container, kobo_books, library_books=None, counts=None):
    mock_container.mock_kobo_service.is_configured.return_value = True
    mock_container.mock_database_service.get_kobo_books.return_value = list(kobo_books)
    mock_container.mock_database_service.get_kobo_bookmark_counts_by_content_id.return_value = counts or {}
    mock_container.mock_database_service.get_all_books.return_value = list(library_books or [])


def test_list_device_books_includes_match_title_and_bookmark_count(client, mock_container):
    kobo_books = [
        _make_kobo_book("cid-1", matched_book_id=5),
        _make_kobo_book("cid-2", title="Unknown"),
    ]
    _configure_kobo(
        mock_container,
        kobo_books,
        library_books=[_make_library_book(5, "Dune (Library)")],
        counts={"cid-1": 3},
    )

    resp = client.get("/api/kobo/books")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["configured"] is True

    by_id = {b["content_id"]: b for b in payload["books"]}
    assert by_id["cid-1"]["matched_book_title"] == "Dune (Library)"
    assert by_id["cid-1"]["bookmark_count"] == 3
    assert by_id["cid-2"]["matched_book_title"] is None
    assert by_id["cid-2"]["bookmark_count"] == 0


def test_kobo_books_page_renders_with_injected_books(client, mock_container):
    mock_container.mock_kobo_service.database_copies.return_value = [Path("/kobo/KoboReader.sqlite")]
    _configure_kobo(
        mock_container,
        [_make_kobo_book("cid-1", matched_book_id=5), _make_kobo_book("cid-2", title="Unknown")],
        library_books=[_make_library_book(5, "Dune (Library)")],
    )

    resp = client.get("/kobo-books")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Device Books" in html
    assert "Dune" in html
    assert "Dune (Library)" in html
    assert "/static/js/kobo-books.js" in html


def test_hide_device_book_success(client, mock_container):
    mock_container.mock_database_service.get_kobo_book.return_value = _make_kobo_book()

    resp = client.post("/api/kobo/hide", json={"content_id": "cid-1", "hidden": True})
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True, "hidden": True}
    mock_container.mock_database_service.set_kobo_books_hidden.assert_called_once_with(["cid-1"], True)


def test_hide_device_book_unhide(client, mock_container):
    mock_container.mock_database_service.get_kobo_book.return_value = _make_kobo_book(hidden=True)

    resp = client.post("/api/kobo/hide", json={"content_id": "cid-1", "hidden": False})
    assert resp.status_code == 200
    mock_container.mock_database_service.set_kobo_books_hidden.assert_called_once_with(["cid-1"], False)


def test_hide_device_book_requires_content_id(client):
    resp = client.post("/api/kobo/hide", json={})
    assert resp.status_code == 400


def test_hide_device_book_unknown_content_id(client, mock_container):
    mock_container.mock_database_service.get_kobo_book.return_value = None

    resp = client.post("/api/kobo/hide", json={"content_id": "nope"})
    assert resp.status_code == 404


def test_settings_page_has_kobo_tab(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="panel-kobo"' in html
    assert 'name="KOBO_ENABLED"' in html
    assert "/kobo-books" in html
