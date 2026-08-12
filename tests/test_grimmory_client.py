import json
import os

# Adjust path to import src
import sys
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.api.grimmory_client import GrimmoryClient
from src.db.models import GrimmoryBook


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def grimmory_client(mock_db):
    with patch.dict(
        os.environ,
        {
            "GRIMMORY_SERVER": "http://mock-grimmory",
            "GRIMMORY_USER": "testuser",
            "GRIMMORY_PASSWORD": "testpass",
            "DATA_DIR": "/tmp/data",
        },
    ):
        client = GrimmoryClient(database_service=mock_db)
        yield client


def test_init_loads_from_db(mock_db):
    # Setup mock DB return
    mock_book = MagicMock()
    mock_book.filename = "test_book.epub"
    mock_book.title = "Test Book"
    mock_book.authors = "Test Author"
    mock_book.raw_metadata_dict = {
        "id": "123",
        "fileName": "test_book.epub",
        "title": "Test Book",
        "authors": "Test Author",
    }

    mock_db.get_all_grimmory_books.return_value = [mock_book]

    with patch.dict(
        os.environ,
        {"GRIMMORY_SERVER": "http://mock", "GRIMMORY_USER": "u", "GRIMMORY_PASSWORD": "p", "DATA_DIR": "/tmp/data"},
    ):
        client = GrimmoryClient(database_service=mock_db)

        assert "test_book.epub" in client._book_cache
        assert client._book_cache["test_book.epub"]["id"] == "123"
        assert client._book_id_cache["123"]["title"] == "Test Book"


def test_mixed_case_filename_preserves_exact_key_and_resolves_case_insensitive_lookup(mock_db):
    """A mixed-case DB row keeps its exact key but still resolves lowercase lookups."""
    mock_book = MagicMock()
    mock_book.filename = "Mixed_Case.epub"
    mock_book.title = "Mixed Case Book"
    mock_book.authors = "Author"
    mock_book.raw_metadata_dict = {
        "id": "777",
        "fileName": "Mixed_Case.epub",
        "title": "Mixed Case Book",
        "authors": "Author",
    }
    mock_db.get_all_grimmory_books.return_value = [mock_book]

    with patch.dict(
        os.environ,
        {"GRIMMORY_SERVER": "http://mock", "GRIMMORY_USER": "u", "GRIMMORY_PASSWORD": "p", "DATA_DIR": "/tmp/data"},
    ):
        client = GrimmoryClient(database_service=mock_db)

    assert "Mixed_Case.epub" in client._book_cache
    assert "mixed_case.epub" not in client._book_cache
    assert client.find_book_by_filename("Mixed_Case.epub", allow_refresh=False)["id"] == "777"
    assert client.find_book_by_filename("mixed_case.epub", allow_refresh=False)["id"] == "777"

    mock_db.replace_grimmory_book_filename.assert_not_called()
    mock_db.delete_grimmory_book.assert_not_called()


def test_load_cache_keeps_case_distinct_filenames(mock_db):
    """Exact cache keys prevent Foo.epub and foo.epub from overwriting each other."""
    mock_db.get_all_grimmory_books.return_value = [
        _make_db_book("upper", "Foo.epub", "Upper Foo", "Author A"),
        _make_db_book("lower", "foo.epub", "Lower Foo", "Author B"),
    ]

    with patch.dict(
        os.environ,
        {"GRIMMORY_SERVER": "http://mock", "GRIMMORY_USER": "u", "GRIMMORY_PASSWORD": "p", "DATA_DIR": "/tmp/data"},
    ):
        client = GrimmoryClient(database_service=mock_db)

    assert set(client._book_cache) == {"Foo.epub", "foo.epub"}
    assert client._book_cache["Foo.epub"]["id"] == "upper"
    assert client._book_cache["foo.epub"]["id"] == "lower"
    assert client.find_book_by_filename("Foo.epub", allow_refresh=False)["id"] == "upper"
    assert client.find_book_by_filename("foo.epub", allow_refresh=False)["id"] == "lower"
    assert client.find_book_by_filename("FOO.epub", allow_refresh=False) is None
    mock_db.replace_grimmory_book_filename.assert_not_called()


def test_load_cache_populates_case_insensitive_index_without_full_rebuild(mock_db):
    mock_db.get_all_grimmory_books.return_value = [
        _make_db_book("first", "One.epub", "One", "Author A"),
        _make_db_book("second", "Two.epub", "Two", "Author B"),
    ]

    with patch.object(
        GrimmoryClient,
        "_rebuild_case_insensitive_cache",
        side_effect=AssertionError("full rebuild should not run during per-book cache insert"),
    ):
        with patch.dict(
            os.environ,
            {
                "GRIMMORY_SERVER": "http://mock",
                "GRIMMORY_USER": "u",
                "GRIMMORY_PASSWORD": "p",
                "DATA_DIR": "/tmp/data",
            },
        ):
            client = GrimmoryClient(database_service=mock_db)

    assert client.find_book_by_filename("one.epub", allow_refresh=False)["id"] == "first"
    assert client.find_book_by_filename("TWO.epub", allow_refresh=False)["id"] == "second"


def test_cache_book_info_updates_same_exact_filename_without_marking_ambiguous(mock_db):
    mock_db.get_all_grimmory_books.return_value = []

    with patch.dict(
        os.environ,
        {"GRIMMORY_SERVER": "http://mock", "GRIMMORY_USER": "u", "GRIMMORY_PASSWORD": "p", "DATA_DIR": "/tmp/data"},
    ):
        client = GrimmoryClient(database_service=mock_db)

    client._cache_book_info("Book.epub", {"id": "old", "fileName": "Book.epub", "title": "Old"})
    client._cache_book_info("Book.epub", {"id": "new", "fileName": "Book.epub", "title": "New"})

    assert client.find_book_by_filename("book.epub", allow_refresh=False)["id"] == "new"
    assert client.find_book_by_filename("BOOK.epub", allow_refresh=False)["title"] == "New"


def test_refresh_migrates_cached_book_id_to_live_filename_casing(mock_db):
    mock_db.get_all_grimmory_books.return_value = [_make_db_book("777", "book.epub", "Book", "Author")]

    with patch.dict(
        os.environ,
        {"GRIMMORY_SERVER": "http://mock", "GRIMMORY_USER": "u", "GRIMMORY_PASSWORD": "p", "DATA_DIR": "/tmp/data"},
    ):
        client = GrimmoryClient(database_service=mock_db)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.side_effect = [
        [
            {
                "id": "777",
                "title": "Book",
                "primaryFile": {"id": 42, "fileName": "Book.epub", "bookType": "EPUB"},
                "metadata": {"authors": ["Author"]},
            }
        ],
        [],
    ]
    client._get_fresh_token = MagicMock(return_value="fake_token")
    client._make_request = MagicMock(side_effect=[mock_response, mock_response])

    with patch.dict(
        os.environ,
        {"GRIMMORY_SERVER": "http://mock", "GRIMMORY_USER": "u", "GRIMMORY_PASSWORD": "p", "DATA_DIR": "/tmp/data"},
    ):
        assert client._refresh_book_cache() is True

    assert set(client._book_cache) == {"Book.epub"}
    assert client.find_book_by_filename("book.epub", allow_refresh=False)["id"] == "777"
    mock_db.delete_grimmory_book.assert_called_once_with("book.epub", server_id="default")
    saved_book = mock_db.save_grimmory_book.call_args[0][0]
    assert saved_book.filename == "Book.epub"


def test_migration_from_legacy_json(mock_db):
    # Setup: DB is empty, Legacy JSON exists
    mock_db.get_all_grimmory_books.side_effect = [[], []]  # First call empty, second call empty

    legacy_data = {"books": {"legacy.epub": {"id": "999", "title": "Legacy Book", "authors": "Old Author"}}}

    # Mock open AND json.load to ensure data is returned correctly
    with patch("builtins.open", mock_open(read_data=json.dumps(legacy_data))):
        # Need to ensure json.load reads from the mock
        with patch("json.load", return_value=legacy_data):
            with patch.object(Path, "exists", return_value=True):
                with patch.object(Path, "rename") as mock_rename:
                    with patch.dict(
                        os.environ,
                        {
                            "GRIMMORY_SERVER": "http://mock",
                            "GRIMMORY_USER": "u",
                            "GRIMMORY_PASSWORD": "p",
                            "DATA_DIR": "/tmp/data",
                        },
                    ):
                        GrimmoryClient(database_service=mock_db)

                        # Verification
                        mock_db.save_grimmory_book.assert_called_once()
                        call_args = mock_db.save_grimmory_book.call_args[0][0]
                        assert isinstance(call_args, GrimmoryBook)
                        assert call_args.filename == "legacy.epub"
                        assert call_args.title == "Legacy Book"

                        # Verify rename was called
                        mock_rename.assert_called()


def test_save_to_db_on_fetch(mock_db):
    # Setup basic client
    with patch.dict(
        os.environ,
        {
            "GRIMMORY_SERVER": "http://mock-grimmory",
            "GRIMMORY_USER": "test",
            "GRIMMORY_PASSWORD": "pass",
            "DATA_DIR": "/tmp/data",
        },
    ):
        client = GrimmoryClient(database_service=mock_db)

        # Mock dependencies
        mock_response = MagicMock()
        mock_response.status_code = 200
        # First call returns list with full Book DTO, second empty to stop loop
        mock_response.json.side_effect = [
            [
                {
                    "id": "new1",
                    "title": "New Book",
                    "primaryFile": {"id": 42, "fileName": "newbook.epub", "bookType": "EPUB"},
                    "metadata": {"authors": ["New Author"]},
                }
            ],
            [],
        ]

        # Mock token and request
        client._get_fresh_token = MagicMock(return_value="fake_token")
        client._make_request = MagicMock(side_effect=[mock_response, mock_response])

        client._refresh_book_cache()

        # Verify processing happened
        mock_db.save_grimmory_book.assert_called()
        saved_book = mock_db.save_grimmory_book.call_args[0][0]
        assert saved_book.filename == "newbook.epub"


def test_extract_progress_epub(grimmory_client):
    book_info = {
        "epubProgress": {"percentage": 45.5, "cfi": "epubcfi(/6/4)"},
        "pdfProgress": None,
        "cbxProgress": None,
    }
    pct, cfi = grimmory_client.extract_progress(book_info)
    assert pct == pytest.approx(0.455)
    assert cfi == "epubcfi(/6/4)"


def test_extract_progress_pdf(grimmory_client):
    book_info = {
        "epubProgress": None,
        "pdfProgress": {"percentage": 30.0},
        "cbxProgress": None,
    }
    pct, cfi = grimmory_client.extract_progress(book_info)
    assert pct == pytest.approx(0.30)
    assert cfi is None


def test_extract_progress_none(grimmory_client):
    book_info = {
        "epubProgress": None,
        "pdfProgress": None,
        "cbxProgress": None,
    }
    pct, cfi = grimmory_client.extract_progress(book_info)
    assert pct is None
    assert cfi is None


def test_extract_progress_zero(grimmory_client):
    """percentage=0 should return 0.0, not None."""
    book_info = {
        "epubProgress": {"percentage": 0, "cfi": None},
        "pdfProgress": None,
        "cbxProgress": None,
    }
    pct, cfi = grimmory_client.extract_progress(book_info)
    assert pct == 0.0
    assert cfi is None


def test_update_progress_file_progress(grimmory_client):
    """When bookFileId is present, use modern fileProgress payload."""
    from src.sync_clients.sync_client_interface import LocatorResult

    grimmory_client._book_cache = {
        "test.epub": {
            "id": 10,
            "fileName": "test.epub",
            "bookType": "EPUB",
            "bookFileId": 42,
            "epubProgress": None,
            "pdfProgress": None,
            "cbxProgress": None,
        }
    }
    grimmory_client._book_id_cache = {10: grimmory_client._book_cache["test.epub"]}
    grimmory_client._cache_timestamp = 9999999999

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    grimmory_client._make_request = MagicMock(return_value=mock_resp)

    locator = LocatorResult(percentage=0.5, cfi="epubcfi(/6/4)", href="chapter2.xhtml")
    result = grimmory_client.update_progress("test.epub", 0.5, locator)

    assert result is True
    call_args = grimmory_client._make_request.call_args
    payload = call_args[0][2]
    assert "fileProgress" in payload
    assert payload["fileProgress"]["bookFileId"] == 42
    assert payload["fileProgress"]["progressPercent"] == 50.0
    assert payload["fileProgress"]["positionData"] == "epubcfi(/6/4)"
    assert payload["fileProgress"]["positionHref"] == "chapter2.xhtml"


def test_update_progress_legacy_fallback(grimmory_client):
    """When bookFileId is missing, fall back to legacy format."""
    grimmory_client._book_cache = {
        "test.epub": {
            "id": 10,
            "fileName": "test.epub",
            "bookType": "EPUB",
            "epubProgress": None,
            "pdfProgress": None,
            "cbxProgress": None,
        }
    }
    grimmory_client._book_id_cache = {10: grimmory_client._book_cache["test.epub"]}
    grimmory_client._cache_timestamp = 9999999999

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    grimmory_client._make_request = MagicMock(return_value=mock_resp)

    result = grimmory_client.update_progress("test.epub", 0.5)
    assert result is True
    payload = grimmory_client._make_request.call_args[0][2]
    assert "epubProgress" in payload
    assert "fileProgress" not in payload
    assert payload["epubProgress"]["percentage"] == 50.0


def test_update_progress_no_page_field(grimmory_client):
    """PDF/CBX legacy payload must not include page field."""
    grimmory_client._book_cache = {
        "test.pdf": {
            "id": 20,
            "fileName": "test.pdf",
            "bookType": "PDF",
            "epubProgress": None,
            "pdfProgress": None,
            "cbxProgress": None,
        }
    }
    grimmory_client._book_id_cache = {20: grimmory_client._book_cache["test.pdf"]}
    grimmory_client._cache_timestamp = 9999999999

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    grimmory_client._make_request = MagicMock(return_value=mock_resp)

    result = grimmory_client.update_progress("test.pdf", 0.75)
    assert result is True
    payload = grimmory_client._make_request.call_args[0][2]
    assert "pdfProgress" in payload
    assert "page" not in payload["pdfProgress"]
    assert payload["pdfProgress"]["percentage"] == 75.0


def test_fetch_bulk_state():
    """GrimmorySyncClient.fetch_bulk_state keys by lowercase filename."""
    from src.sync_clients.grimmory_sync_client import GrimmorySyncClient

    mock_client = MagicMock()
    mock_client.is_configured.return_value = True
    mock_client.get_all_books.return_value = [
        {"fileName": "Book1.epub", "id": 1, "epubProgress": {"percentage": 50}},
        {"fileName": "Book2.PDF", "id": 2, "pdfProgress": {"percentage": 30}},
    ]

    sync_client = GrimmorySyncClient(grimmory_client=mock_client, ebook_parser=MagicMock())
    bulk = sync_client.fetch_bulk_state()

    assert "book1.epub" in bulk
    assert "book2.pdf" in bulk
    assert bulk["book1.epub"]["id"] == 1


# ── ABS -> Grimmory migration write/match methods ──


def test_update_read_status_by_id(grimmory_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    grimmory_client._make_request = MagicMock(return_value=mock_resp)

    result = grimmory_client.update_read_status_by_id(77, "READ")

    assert result is True
    method, endpoint, payload = grimmory_client._make_request.call_args[0]
    assert method == "POST"
    assert endpoint == "/api/v1/books/status"
    assert payload == {"bookIds": [77], "status": "READ"}


def test_update_read_status_delegates_to_by_id(grimmory_client):
    """The filename-based method resolves the id then delegates."""
    grimmory_client._book_cache = {"test.epub": {"id": 88, "fileName": "test.epub", "bookType": "EPUB"}}
    grimmory_client._book_id_cache = {88: grimmory_client._book_cache["test.epub"]}
    grimmory_client._cache_timestamp = 9999999999
    grimmory_client.update_read_status_by_id = MagicMock(return_value=True)

    result = grimmory_client.update_read_status("test.epub", "READ")

    assert result is True
    grimmory_client.update_read_status_by_id.assert_called_once_with(88, "READ")


def test_set_finished_date(grimmory_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    grimmory_client._make_request = MagicMock(return_value=mock_resp)

    result = grimmory_client.set_finished_date(12, "EPUB", "2024-03-09")

    assert result is True
    method, endpoint, payload = grimmory_client._make_request.call_args[0]
    assert method == "POST"
    assert endpoint == "/api/v1/books/progress"
    assert payload["bookId"] == 12
    # Grimmory binds dateFinished to a java.time.Instant; a bare date must be
    # expanded to a full ISO-8601 instant or the request is rejected.
    assert payload["dateFinished"] == "2024-03-09T00:00:00Z"


def test_set_finished_date_passes_through_instant(grimmory_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    grimmory_client._make_request = MagicMock(return_value=mock_resp)

    result = grimmory_client.set_finished_date(12, "EPUB", "2024-03-09T18:30:00Z")

    assert result is True
    _, _, payload = grimmory_client._make_request.call_args[0]
    assert payload["dateFinished"] == "2024-03-09T18:30:00Z"


def test_add_reading_session(grimmory_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    grimmory_client._make_request = MagicMock(return_value=mock_resp)

    result = grimmory_client.add_reading_session(
        5, "AUDIOBOOK", start_time="2024-01-01T10:00:00Z", end_time="2024-01-01T11:00:00Z", duration_seconds=3600
    )

    assert result is True
    method, endpoint, payload = grimmory_client._make_request.call_args[0]
    assert method == "POST"
    assert endpoint == "/api/v1/reading-sessions"
    assert payload["bookId"] == 5
    assert payload["startTime"] == "2024-01-01T10:00:00Z"
    assert payload["endTime"] == "2024-01-01T11:00:00Z"
    assert payload["durationSeconds"] == 3600


def test_add_bookmark_audiobook(grimmory_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    grimmory_client._make_request = MagicMock(return_value=mock_resp)

    result = grimmory_client.add_bookmark(9, position_ms=125000, track_index=2, title="Chapter 3")

    assert result is True
    method, endpoint, payload = grimmory_client._make_request.call_args[0]
    assert method == "POST"
    assert endpoint == "/api/v1/bookmarks"
    assert payload["bookId"] == 9
    assert payload["positionMs"] == 125000
    assert payload["trackIndex"] == 2
    assert payload["title"] == "Chapter 3"


def test_match_book_by_identifiers_isbn_wins(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Other", "authors": "Someone", "isbn13": "111"},
            {"id": 2, "title": "Target", "authors": "Author A", "isbn13": "9780000000001", "asin": "B00ASIN"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        isbn="9780000000001", asin="B00ASIN", title="Target", author="Author A"
    )

    assert book["id"] == 2
    assert matched_by == "isbn"


def test_match_book_by_identifiers_asin_when_no_isbn(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[{"id": 3, "title": "T", "authors": "A", "asin": "B00XYZ"}]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(isbn=None, asin="B00XYZ", title="T", author="A")

    assert book["id"] == 3
    assert matched_by == "asin"


def test_match_book_by_identifiers_title_author(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[{"id": 4, "title": "The Great Book", "authors": "Jane Doe"}]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        isbn=None, asin=None, title="The Great Book", author="Jane Doe"
    )

    assert book["id"] == 4
    assert matched_by in ("title_author", "title")


def test_match_book_by_identifiers_no_match(grimmory_client):
    grimmory_client.get_all_books = MagicMock(return_value=[{"id": 5, "title": "Nope", "authors": "Nobody"}])

    book, matched_by = grimmory_client.match_book_by_identifiers(
        isbn="999", asin="X", title="Totally Different", author="Other Person"
    )

    assert book is None
    assert matched_by is None


def test_match_book_by_identifiers_ignores_blank_asin(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[{"id": 9, "title": "Unrelated", "authors": "A", "asin": ""}]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        asin="   ", title="Different Title", author="Other"
    )

    assert book is None
    assert matched_by is None


def test_match_book_by_identifiers_authors_list_does_not_crash(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[{"id": 6, "title": "Legacy Title", "authors": ["Jane Doe"]}]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        isbn=None, asin=None, title="Legacy Title", author="Jane Doe"
    )

    assert book["id"] == 6
    assert matched_by == "title"


def test_match_book_by_identifiers_non_numeric_threshold_falls_back(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[{"id": 7, "title": "Findable Book", "authors": "Author"}]
    )

    with patch.dict(os.environ, {"FUZZY_MATCH_THRESHOLD": "eighty"}):
        book, matched_by = grimmory_client.match_book_by_identifiers(
            isbn=None, asin=None, title="Findable Book", author="Author"
        )

    assert book["id"] == 7
    assert matched_by == "title_author"


def _group_member(instance_id, match_return):
    member = MagicMock()
    member.instance_id = instance_id
    member.is_configured.return_value = True
    member.match_book_by_identifiers.return_value = match_return
    return member


def test_group_match_prefers_strongest_match_across_instances():
    from src.api.grimmory_client import GrimmoryClientGroup

    weak = _group_member("a", ({"id": 10, "title": "T"}, "title"))
    strong = _group_member("b", ({"id": 20, "title": "T"}, "isbn"))
    group = GrimmoryClientGroup([weak, strong])

    book, matched_by = group.match_book_by_identifiers(isbn="123", title="T")

    assert book["id"] == 20
    assert book["_instance_id"] == "b"
    assert matched_by == "isbn"


def test_group_match_returns_none_when_no_instance_matches():
    from src.api.grimmory_client import GrimmoryClientGroup

    a = _group_member("a", (None, None))
    b = _group_member("b", (None, None))
    group = GrimmoryClientGroup([a, b])

    book, matched_by = group.match_book_by_identifiers(title="Nope")

    assert book is None
    assert matched_by is None


def test_group_match_keeps_exact_identifier_over_preferred_type_from_weaker_tier():
    # A fuzzy title-only audiobook must not override an exact ISBN match on a
    # different instance. Preference breaks ties within a tier; it does not
    # outrank identifier confidence.
    from src.api.grimmory_client import GrimmoryClientGroup

    ebook = _group_member("a", ({"id": 1, "title": "Dual", "bookType": "EPUB"}, "isbn"))
    audiobook = _group_member("b", ({"id": 2, "title": "Dual", "bookType": "AUDIOBOOK"}, "title"))
    group = GrimmoryClientGroup([ebook, audiobook])

    book, matched_by = group.match_book_by_identifiers(
        isbn="9780000000001", title="Dual", prefer_book_type="AUDIOBOOK"
    )

    assert book["id"] == 1
    assert book["_instance_id"] == "a"
    assert matched_by == "isbn"


def test_match_prefer_audiobook_isbn_tie(grimmory_client):
    # EPUB listed first to prove ordering is overridden by the preference.
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Dual", "authors": "A", "isbn13": "9780000000001", "bookType": "EPUB"},
            {"id": 2, "title": "Dual", "authors": "A", "isbn13": "9780000000001", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        isbn="9780000000001", prefer_book_type="AUDIOBOOK"
    )

    assert book["id"] == 2
    assert matched_by == "isbn"


def test_match_prefer_audiobook_asin_tie(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Dual", "authors": "A", "asin": "B00DUAL", "bookType": "EPUB"},
            {"id": 2, "title": "Dual", "authors": "A", "asin": "B00DUAL", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(asin="B00DUAL", prefer_book_type="AUDIOBOOK")

    assert book["id"] == 2
    assert matched_by == "asin"


def test_match_prefer_audiobook_title_author_tie(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Same Title", "authors": "Jane Doe", "bookType": "EPUB"},
            {"id": 2, "title": "Same Title", "authors": "Jane Doe", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        title="Same Title", author="Jane Doe", prefer_book_type="AUDIOBOOK"
    )

    assert book["id"] == 2
    assert matched_by == "title_author"


def test_match_prefer_audiobook_falls_back_when_absent(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[{"id": 1, "title": "Dual", "authors": "A", "isbn13": "9780000000001", "bookType": "EPUB"}]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        isbn="9780000000001", prefer_book_type="AUDIOBOOK"
    )

    assert book["id"] == 1
    assert matched_by == "isbn"


def test_match_default_keeps_first_hit_without_preference(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Dual", "authors": "A", "isbn13": "9780000000001", "bookType": "EPUB"},
            {"id": 2, "title": "Dual", "authors": "A", "isbn13": "9780000000001", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(isbn="9780000000001")

    assert book["id"] == 1
    assert matched_by == "isbn"


def test_match_prefer_audiobook_lower_ratio_title_wins(grimmory_client):
    # Real dual-format data: the audiobook title carries a track prefix ("02 ")
    # scoring below the exact-match ebook. With AUDIOBOOK preferred, the
    # lower-ratio audiobook must still win over the higher-ratio ebook.
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Paladin's Strength", "authors": "", "bookType": "EPUB"},
            {"id": 2, "title": "02 Paladin's Strength", "authors": "", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        title="Paladin's Strength", prefer_book_type="AUDIOBOOK"
    )

    assert book["id"] == 2
    assert matched_by == "title"


def test_match_title_no_preference_keeps_best_ratio(grimmory_client):
    # Without a preference, the exact-ratio ebook still wins over the prefixed
    # audiobook -- the near-ratio override only applies when a type is preferred.
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Paladin's Strength", "authors": "", "bookType": "EPUB"},
            {"id": 2, "title": "02 Paladin's Strength", "authors": "", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(title="Paladin's Strength")

    assert book["id"] == 1
    assert matched_by == "title"


def test_match_prefer_audiobook_title_falls_back_when_absent(grimmory_client):
    # No audiobook candidate at all: fall back to the best-ratio ebook.
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Solo Title", "authors": "", "bookType": "EPUB"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        title="Solo Title", prefer_book_type="AUDIOBOOK"
    )

    assert book["id"] == 1
    assert matched_by == "title"


def test_match_prefer_audiobook_keeps_exact_isbn_over_later_asin(grimmory_client):
    # The ebook carries an exact ISBN while the audiobook only matches the later
    # ASIN tier. Keep the identifier cascade order instead of letting preference
    # cross exact tiers.
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Dual", "authors": "A", "isbn13": "9780000000001", "bookType": "EPUB"},
            {"id": 2, "title": "Dual", "authors": "A", "asin": "B00DUAL", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        isbn="9780000000001", asin="B00DUAL", title="Dual", prefer_book_type="AUDIOBOOK"
    )

    assert book["id"] == 1
    assert matched_by == "isbn"


def test_match_prefer_audiobook_keeps_exact_isbn_over_later_title(grimmory_client):
    # The ebook owns an exact ISBN; the audiobook only shares a fuzzy title. The
    # exact identifier match is safer and must win.
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Shared Title", "authors": "A", "isbn13": "9780000000001", "bookType": "EPUB"},
            {"id": 2, "title": "Shared Title", "authors": "A", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(
        isbn="9780000000001", title="Shared Title", prefer_book_type="AUDIOBOOK"
    )

    assert book["id"] == 1
    assert matched_by == "isbn"


def test_match_exclude_book_id_skips_audiobook(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Dual", "authors": "A", "isbn13": "9780000000001", "bookType": "AUDIOBOOK"},
            {"id": 2, "title": "Dual", "authors": "A", "isbn13": "9780000000001", "bookType": "EPUB"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(isbn="9780000000001", exclude_book_id=1)

    assert book["id"] == 2
    assert matched_by == "isbn"


def test_match_exclude_book_id_no_other_record(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Solo", "authors": "A", "isbn13": "9780000000001", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.match_book_by_identifiers(isbn="9780000000001", exclude_book_id=1)

    assert book is None
    assert matched_by is None


def test_group_match_prefers_audiobook_across_instances():
    from src.api.grimmory_client import GrimmoryClientGroup

    # Both instances return an equal-strength isbn match; only one is the audiobook.
    ebook = _group_member("a", ({"id": 10, "title": "T", "bookType": "EPUB"}, "isbn"))
    audio = _group_member("b", ({"id": 20, "title": "T", "bookType": "AUDIOBOOK"}, "isbn"))
    group = GrimmoryClientGroup([ebook, audio])

    book, matched_by = group.match_book_by_identifiers(isbn="123", title="T", prefer_book_type="AUDIOBOOK")

    assert book["id"] == 20
    assert book["_instance_id"] == "b"
    assert matched_by == "isbn"


def test_find_format_counterpart_title_only(grimmory_client):
    # ABS finished books arrive author-less; the only working tier is title.
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "01 Nettle & Bone", "authors": "", "bookType": "AUDIOBOOK"},
            {"id": 2, "title": "Nettle & Bone", "authors": "", "bookType": "EPUB"},
        ]
    )

    book, matched_by = grimmory_client.find_format_counterpart(
        matched_book={"id": 1, "bookType": "AUDIOBOOK"}, title="Nettle & Bone"
    )

    assert book["id"] == 2
    assert matched_by == "title"


def test_find_format_counterpart_excludes_audiobook(grimmory_client):
    # A second, lower-ratio audiobook must never be returned as the counterpart,
    # even when its title is the closest match after the matched audiobook.
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Paladin's Strength", "authors": "", "bookType": "AUDIOBOOK"},
            {"id": 2, "title": "Paladin's Strength", "authors": "", "bookType": "AUDIOBOOK"},
            {"id": 3, "title": "Paladin's Strength", "authors": "", "bookType": "EPUB"},
        ]
    )

    book, matched_by = grimmory_client.find_format_counterpart(
        matched_book={"id": 1, "bookType": "AUDIOBOOK"}, title="Paladin's Strength"
    )

    assert book["id"] == 3
    assert book["bookType"] == "EPUB"


def test_find_format_counterpart_collapsed_filename_other_format_survives(grimmory_client):
    # When same-filename ebook duplicates collapse in the cache, the surviving
    # ebook is a genuinely distinct bookType from the audiobook, so excluding the
    # audiobook id still leaves it to be found.
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 100, "title": "Carl's Doomsday Scenario", "authors": "", "bookType": "AUDIOBOOK"},
            {"id": 808, "title": "Carl's Doomsday Scenario", "authors": "", "bookType": "EPUB"},
        ]
    )

    book, matched_by = grimmory_client.find_format_counterpart(
        matched_book={"id": 100, "bookType": "AUDIOBOOK"}, title="Carl's Doomsday Scenario"
    )

    assert book["id"] == 808
    assert matched_by == "title"


def test_find_format_counterpart_isbn_tier(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Dual", "isbn13": "9780000000001", "bookType": "AUDIOBOOK"},
            {"id": 2, "title": "Dual", "isbn13": "9780000000001", "bookType": "EPUB"},
        ]
    )

    book, matched_by = grimmory_client.find_format_counterpart(
        matched_book={"id": 1, "bookType": "AUDIOBOOK"}, isbn="9780000000001", title="Dual"
    )

    assert book["id"] == 2
    assert matched_by == "isbn"


def test_find_format_counterpart_does_not_exclude_same_numeric_id_on_other_instance(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Dual", "isbn13": "9780000000001", "bookType": "EPUB", "_instance_id": "b"},
        ]
    )

    book, matched_by = grimmory_client.find_format_counterpart(
        matched_book={
            "id": 1,
            "title": "Dual",
            "isbn13": "9780000000001",
            "bookType": "AUDIOBOOK",
            "_instance_id": "a",
        },
        isbn="9780000000001",
        title="Dual",
    )

    assert book["id"] == 1
    assert book["_instance_id"] == "b"
    assert matched_by == "isbn"


def test_find_format_counterpart_title_author_tier(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Dual", "authors": "Jane Doe", "bookType": "AUDIOBOOK"},
            {"id": 2, "title": "Dual", "authors": "Jane Doe", "bookType": "EPUB"},
        ]
    )

    book, matched_by = grimmory_client.find_format_counterpart(
        matched_book={"id": 1, "bookType": "AUDIOBOOK"}, title="Dual", author="Jane Doe"
    )

    assert book["id"] == 2
    assert matched_by == "title_author"


def test_find_format_counterpart_no_non_audiobook_record(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[
            {"id": 1, "title": "Solo", "authors": "", "bookType": "AUDIOBOOK"},
        ]
    )

    book, matched_by = grimmory_client.find_format_counterpart(
        matched_book={"id": 1, "bookType": "AUDIOBOOK"}, title="Solo"
    )

    assert book is None
    assert matched_by is None


def test_find_format_counterpart_no_matched_book(grimmory_client):
    grimmory_client.get_all_books = MagicMock(
        return_value=[{"id": 2, "title": "Solo", "bookType": "EPUB"}]
    )

    book, matched_by = grimmory_client.find_format_counterpart(matched_book=None, title="Solo")

    assert book is None
    assert matched_by is None


def _group_counterpart_member(instance_id, counterpart_return):
    member = MagicMock()
    member.instance_id = instance_id
    member.is_configured.return_value = True
    member.find_format_counterpart.return_value = counterpart_return
    return member


def test_group_find_format_counterpart_keeps_strongest(grimmory_client):
    from src.api.grimmory_client import GrimmoryClientGroup

    weak = _group_counterpart_member("a", ({"id": 10, "title": "T", "bookType": "EPUB"}, "title"))
    strong = _group_counterpart_member("b", ({"id": 20, "title": "T", "bookType": "EPUB"}, "isbn"))
    group = GrimmoryClientGroup([weak, strong])

    book, matched_by = group.find_format_counterpart(
        matched_book={"id": 1, "bookType": "AUDIOBOOK"}, isbn="123", title="T"
    )

    assert book["id"] == 20
    assert book["_instance_id"] == "b"
    assert matched_by == "isbn"


def test_group_find_format_counterpart_none_when_no_instance_matches():
    from src.api.grimmory_client import GrimmoryClientGroup

    a = _group_counterpart_member("a", (None, None))
    b = _group_counterpart_member("b", (None, None))
    group = GrimmoryClientGroup([a, b])

    book, matched_by = group.find_format_counterpart(
        matched_book={"id": 1, "bookType": "AUDIOBOOK"}, title="Nope"
    )

    assert book is None
    assert matched_by is None


def _make_db_book(book_id, filename, title, authors):
    book = MagicMock()
    book.filename = filename
    book.title = title
    book.authors = authors
    book.raw_metadata_dict = {"id": book_id, "fileName": filename, "title": title, "authors": authors}
    return book


def test_reload_from_env_loads_cache_when_started_unconfigured(mock_db):
    """A client constructed while unconfigured becomes configured and loads its
    cache after a Settings save calls reload_from_env (issues #62/#76)."""
    mock_db.get_all_grimmory_books.return_value = [_make_db_book("123", "late_book.epub", "Late Book", "Author")]

    # Start unconfigured: __init__ must not load the cache.
    with patch.dict(os.environ, {"DATA_DIR": "/tmp/nonexistent-grimmory-data"}, clear=True):
        client = GrimmoryClient(database_service=mock_db)
        assert client._book_cache == {}

        # Settings save populates env + DB, then reloads the singleton.
        os.environ.update({"GRIMMORY_SERVER": "http://mock", "GRIMMORY_USER": "u", "GRIMMORY_PASSWORD": "p"})
        client.reload_from_env()

        assert client.is_configured()
        assert "late_book.epub" in client._book_cache
        assert client._book_id_cache["123"]["title"] == "Late Book"


def test_reload_from_env_clears_token_and_headers(mock_db):
    """reload_from_env invalidates any cached auth token and session headers."""
    mock_db.get_all_grimmory_books.return_value = []

    with patch.dict(
        os.environ,
        {
            "GRIMMORY_SERVER": "http://mock",
            "GRIMMORY_USER": "u",
            "GRIMMORY_PASSWORD": "p",
            "DATA_DIR": "/tmp/nonexistent-grimmory-data",
        },
        clear=True,
    ):
        client = GrimmoryClient(database_service=mock_db)
        client._token = "stale-token"
        client._token_timestamp = 12345
        client.session.headers["Authorization"] = "Bearer stale-token"

        client.reload_from_env()

        assert client._token is None
        assert client._token_timestamp == 0
        assert "Authorization" not in client.session.headers


def test_reload_from_env_clears_cache_when_unconfigured(mock_db):
    """A client that loses its configuration drops its cache on reload."""
    mock_db.get_all_grimmory_books.return_value = [_make_db_book("123", "book.epub", "Book", "Author")]

    with patch.dict(
        os.environ,
        {
            "GRIMMORY_SERVER": "http://mock",
            "GRIMMORY_USER": "u",
            "GRIMMORY_PASSWORD": "p",
            "DATA_DIR": "/tmp/nonexistent-grimmory-data",
        },
        clear=True,
    ):
        client = GrimmoryClient(database_service=mock_db)
        assert "book.epub" in client._book_cache

        # Configuration removed via Settings save.
        del os.environ["GRIMMORY_SERVER"]
        client.reload_from_env()

        assert not client.is_configured()
        assert client._book_cache == {}
        assert client._book_id_cache == {}
