"""Tests for error paths in src/blueprints/helpers.py."""

from unittest.mock import Mock, patch

# ── get_kosync_id_for_ebook: Grimmory download failure ────────────


def test_get_kosync_id_grimmory_download_raises(flask_app, mock_container):
    """When Grimmory download_book raises, fall through to filesystem lookup."""
    bl_client = Mock()
    bl_client.is_configured.return_value = True
    bl_client.download_book.side_effect = Exception("Grimmory network error")

    mock_container.mock_ebook_parser.get_kosync_id.return_value = None

    with flask_app.app_context():
        from src.blueprints.helpers import get_kosync_id_for_ebook

        result = get_kosync_id_for_ebook("book.epub", grimmory_id=42, bl_client=bl_client)

    # Should return None because filesystem also doesn't have the file
    assert result is None
    bl_client.download_book.assert_called_once_with(42)


def test_get_kosync_id_grimmory_download_returns_none(flask_app, mock_container):
    """When Grimmory download_book returns None, fall through to filesystem."""
    bl_client = Mock()
    bl_client.is_configured.return_value = True
    bl_client.download_book.return_value = None

    with flask_app.app_context():
        from src.blueprints.helpers import get_kosync_id_for_ebook

        result = get_kosync_id_for_ebook("book.epub", grimmory_id=42, bl_client=bl_client)

    assert result is None


def test_get_kosync_id_downloads_selected_alternative_file(flask_app, mock_container):
    bl_client = Mock()
    bl_client.is_configured.return_value = True
    bl_client.download_book.return_value = b"selected alternative epub"
    mock_container.mock_ebook_parser.get_kosync_id_from_bytes.return_value = "alternative-hash"

    with flask_app.app_context():
        from src.blueprints.helpers import get_kosync_id_for_ebook

        result = get_kosync_id_for_ebook(
            "book.epub",
            grimmory_id=10,
            grimmory_file_id=42,
            bl_client=bl_client,
        )

    assert result == "alternative-hash"
    bl_client.download_book.assert_called_once_with(10, file_id=42)
    mock_container.mock_ebook_parser.get_kosync_id_from_bytes.assert_called_once_with(
        "book.epub", b"selected alternative epub"
    )


def test_get_kosync_id_rejects_untrusted_remote_filename(flask_app):
    bl_client = Mock()
    bl_client.is_configured.return_value = True

    with flask_app.app_context():
        from src.blueprints.helpers import get_kosync_id_for_ebook

        assert get_kosync_id_for_ebook("../escape.epub", grimmory_id=10, bl_client=bl_client) is None

    bl_client.download_book.assert_not_called()


def test_get_kosync_id_abs_download_raises(flask_app, mock_container):
    """When ABS on-demand download raises, should return None gracefully."""
    mock_container.mock_abs_client.is_configured.return_value = True
    mock_container.mock_abs_client.get_ebook_files.side_effect = Exception("ABS timeout")

    with flask_app.app_context():
        from src.blueprints.helpers import get_kosync_id_for_ebook

        result = get_kosync_id_for_ebook("someitem_abs.epub")

    assert result is None


def test_get_kosync_id_cwa_download_raises(flask_app, mock_container):
    """When CWA on-demand download raises, should return None gracefully."""
    mock_cwa = Mock()
    mock_cwa.is_configured.return_value = True
    mock_cwa.search_ebooks.side_effect = Exception("CWA error")
    mock_container.cwa_client = lambda: mock_cwa

    with flask_app.app_context():
        from src.blueprints.helpers import get_kosync_id_for_ebook

        result = get_kosync_id_for_ebook("cwa_123.epub")

    assert result is None


# ── find_in_grimmory: API raises ──────────────────────────────────


def test_find_in_grimmory_empty_filename(flask_app, mock_container):
    """find_in_grimmory returns (None, None) for empty filename."""
    with flask_app.app_context():
        from src.blueprints.helpers import find_in_grimmory

        book, client = find_in_grimmory("")

    assert book is None
    assert client is None


def test_find_in_grimmory_none_filename(flask_app, mock_container):
    """find_in_grimmory returns (None, None) for None filename."""
    with flask_app.app_context():
        from src.blueprints.helpers import find_in_grimmory

        book, client = find_in_grimmory(None)

    assert book is None
    assert client is None


def test_find_in_grimmory_not_configured(flask_app, mock_container):
    """find_in_grimmory returns (None, None) when Grimmory is not configured."""
    mock_container.mock_grimmory_client.is_configured.return_value = False

    with flask_app.app_context():
        from src.blueprints.helpers import find_in_grimmory

        book, client = find_in_grimmory("test.epub")

    assert book is None
    assert client is None


def test_find_in_grimmory_no_match(flask_app, mock_container):
    """find_in_grimmory returns (None, None) when no book matches."""
    mock_container.mock_grimmory_client.is_configured.return_value = True
    mock_container.mock_grimmory_client.find_book_by_filename.return_value = None

    with flask_app.app_context():
        from src.blueprints.helpers import find_in_grimmory

        book, client = find_in_grimmory("missing.epub")

    assert book is None
    assert client is None


def test_find_in_grimmory_uses_qualified_server_and_book_id(flask_app, mock_container):
    primary = mock_container.mock_grimmory_client
    secondary = Mock()
    primary.is_configured.return_value = True
    secondary.is_configured.return_value = True
    secondary.find_book_by_filename.return_value = {"id": 22, "fileName": "same.epub"}
    mock_container.grimmory_client_2 = lambda: secondary

    with flask_app.app_context():
        from src.blueprints.helpers import find_in_grimmory

        book, client = find_in_grimmory("same.epub", "2:22")

    assert book["_instance_id"] == "2"
    assert client is secondary
    primary.find_book_by_filename.assert_not_called()


def test_searchable_ebooks_keeps_same_filename_from_two_grimmory_servers(flask_app, mock_container):
    group = mock_container.mock_grimmory_client
    group.is_configured.return_value = True
    group.search_books.return_value = [
        {"id": 11, "title": "Primary", "fileName": "same.epub", "_instance_id": "default"},
        {"id": 22, "title": "Secondary", "fileName": "same.epub", "_instance_id": "2"},
    ]

    with flask_app.app_context():
        from src.blueprints.helpers import get_searchable_ebooks

        results = get_searchable_ebooks("same")

    assert [(result.name, result.source_id) for result in results[:2]] == [
        ("same.epub", "default:11"),
        ("same.epub", "2:22"),
    ]


# ── serialize_suggestion with None fields ─────────────────────────


def test_serialize_suggestion_with_none_fields():
    """serialize_suggestion handles None created_at and empty matches."""
    from src.blueprints.helpers import serialize_suggestion

    suggestion = Mock()
    suggestion.id = 1
    suggestion.source_id = "abc"
    suggestion.source = None
    suggestion.title = None
    suggestion.author = None
    suggestion.cover_url = None
    suggestion.matches = []
    suggestion.created_at = None
    suggestion.status = "pending"

    result = serialize_suggestion(suggestion)

    assert result["id"] == 1
    assert result["source"] == "unknown"
    assert result["title"] is None
    assert result["created_at"] is None
    assert result["matches"] == []
    assert result["top_match"] is None
    assert result["hidden"] is False


def test_serialize_suggestion_with_bookfusion_evidence():
    """serialize_suggestion flags bookfusion evidence correctly."""
    from src.blueprints.helpers import serialize_suggestion

    suggestion = Mock()
    suggestion.id = 2
    suggestion.source_id = "def"
    suggestion.source = "abs"
    suggestion.title = "Test Book"
    suggestion.author = "Author"
    suggestion.cover_url = "/cover.jpg"
    suggestion.matches = [
        {
            "ebook_filename": "test.epub",
            "evidence": ["bookfusion_catalog"],
            "source_family": "bookfusion",
            "bookfusion_ids": [1],
        },
    ]
    suggestion.created_at = None
    suggestion.status = "pending"

    result = serialize_suggestion(suggestion)

    assert result["has_bookfusion_evidence"] is True
    assert result["matches"][0]["has_bookfusion"] is True
    assert result["top_match"] is not None


def test_serialize_suggestion_hidden_status():
    """serialize_suggestion correctly reports hidden=True for hidden status."""
    from src.blueprints.helpers import serialize_suggestion

    suggestion = Mock()
    suggestion.id = 3
    suggestion.source_id = "ghi"
    suggestion.source = "abs"
    suggestion.title = "Hidden Book"
    suggestion.author = None
    suggestion.cover_url = None
    suggestion.matches = [{"ebook_filename": "x.epub", "evidence": []}]
    suggestion.created_at = None
    suggestion.status = "hidden"

    result = serialize_suggestion(suggestion)

    assert result["hidden"] is True


# ── attempt_hardcover_automatch: exception swallowed ──────────────


def test_attempt_hardcover_automatch_swallows_exception(flask_app, mock_container):
    """attempt_hardcover_automatch logs but does not raise on failure."""
    mock_container.mock_hardcover_service.is_configured.return_value = True
    mock_container.mock_hardcover_service.automatch_hardcover.side_effect = Exception("HC down")

    with flask_app.app_context():
        from src.blueprints.helpers import attempt_hardcover_automatch

        book = Mock()
        # Should not raise
        attempt_hardcover_automatch(mock_container, book)

    mock_container.mock_hardcover_service.automatch_hardcover.assert_called_once()


# ── find_grimmory_metadata ────────────────────────────────────────


def test_find_grimmory_metadata_no_match():
    """find_grimmory_metadata returns None when no filename matches."""
    from src.blueprints.helpers import find_grimmory_metadata

    book = Mock()
    book.ebook_filename = "missing.epub"
    book.original_ebook_filename = None

    result = find_grimmory_metadata(book, {})

    assert result is None


def test_find_grimmory_metadata_matches_original():
    """find_grimmory_metadata falls back to original_ebook_filename."""
    from src.blueprints.helpers import find_grimmory_metadata

    book = Mock()
    book.ebook_filename = "renamed.epub"
    book.original_ebook_filename = "original.epub"

    meta = Mock()
    meta.title = "Original Title"
    grimmory_by_filename = {"original.epub": [meta]}

    result = find_grimmory_metadata(book, grimmory_by_filename)

    assert result == meta
