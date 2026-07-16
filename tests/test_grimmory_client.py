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


@pytest.mark.parametrize("extension", ["m4b", "m4a", "mp3", "opus"])
def test_exact_audiobook_identity_and_progress(grimmory_client, extension):
    book = {
        "id": 10,
        "fileName": f"audio.{extension}",
        "bookType": "AUDIOBOOK",
        "bookFileId": 42,
        "audiobookProgress": {"percentage": 37.5, "positionMs": 1200, "trackIndex": 1},
    }
    grimmory_client._reset_book_caches()
    grimmory_client._cache_book_info(book["fileName"], book)
    grimmory_client._cache_timestamp = 9999999999

    assert grimmory_client.audio_source_id(book) == "default:10:42"
    assert grimmory_client.find_audiobook_by_source_id("default:10:42") is book
    assert grimmory_client.find_audiobook_by_source_id("2:10:42") is None
    assert grimmory_client.find_audiobook_by_source_id("default:10:99") is None
    assert grimmory_client.get_audiobook_progress("default:10:42") == (pytest.approx(0.375), None)


def test_update_exact_audiobook_uses_file_progress(grimmory_client):
    book = {
        "id": 10,
        "fileName": "audio.m4b",
        "bookType": "AUDIOBOOK",
        "bookFileId": 42,
        "audiobookProgress": {"percentage": 10},
    }
    grimmory_client._reset_book_caches()
    grimmory_client._cache_book_info(book["fileName"], book)
    grimmory_client._cache_timestamp = 9999999999
    response = MagicMock(status_code=200)
    grimmory_client._make_request = MagicMock(return_value=response)

    assert grimmory_client.update_audiobook_progress("default:10:42", 0.5) is True
    assert grimmory_client._make_request.call_args.args[2] == {
        "bookId": 10,
        "fileProgress": {"bookFileId": 42, "progressPercent": 50.0},
    }
    assert book["audiobookProgress"]["percentage"] == 50.0


@pytest.mark.parametrize("audio_is_primary", [False, True])
def test_processes_primary_and_alternative_formats_with_exact_file_identity(grimmory_client, audio_is_primary):
    ebook = {"id": 41, "fileName": "book.epub", "bookType": "EPUB"}
    audio = {"id": 42, "fileName": "book.m4b", "bookType": "AUDIOBOOK"}
    detail = {
        "id": 10,
        "primaryFile": audio if audio_is_primary else ebook,
        "alternativeFormats": [ebook if audio_is_primary else audio],
        "metadata": {"title": "Book", "authors": [{"name": "Author"}]},
        "epubProgress": {"percentage": 20},
        "audiobookProgress": {"percentage": 35},
    }

    grimmory_client._process_book_detail(detail)

    assert set(grimmory_client._book_cache) == {"book.epub", "book.m4b"}
    assert grimmory_client._book_id_cache[10]["fileName"] == detail["primaryFile"]["fileName"]
    assert grimmory_client.find_audiobook_by_source_id("default:10:42", allow_refresh=False)["fileName"] == "book.m4b"
    assert grimmory_client.find_book_by_filename("book.epub", allow_refresh=False)["bookFileId"] == 41


def test_refresh_prunes_removed_alternative_without_removing_primary_ebook(grimmory_client, mock_db):
    detail = {
        "id": 10,
        "primaryFile": {"id": 41, "fileName": "book.epub", "bookType": "EPUB"},
        "alternativeFormats": [{"id": 42, "fileName": "book.m4b", "bookType": "AUDIOBOOK"}],
        "metadata": {"title": "Book", "authors": ["Author"]},
    }
    grimmory_client._process_book_detail(detail)
    response = MagicMock(status_code=200)
    response.json.return_value = {"content": [{**detail, "alternativeFormats": []}]}
    grimmory_client._make_request = MagicMock(return_value=response)
    mock_db.reset_mock()

    assert grimmory_client._refresh_book_cache() is True

    assert set(grimmory_client._book_cache) == {"book.epub"}
    assert grimmory_client.find_book_by_filename("book.epub", allow_refresh=False)["bookFileId"] == 41
    assert grimmory_client.find_audiobook_by_source_id("default:10:42", allow_refresh=False) is None
    mock_db.delete_grimmory_book.assert_called_once_with("book.m4b", server_id="default")


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
