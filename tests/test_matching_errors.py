"""Tests for error paths in matching blueprint (src/blueprints/matching_bp.py)."""

from pathlib import Path
from unittest.mock import Mock, patch


def _setup_matching_db_defaults(mock_db):
    """Configure database_service mock for matching routes."""
    mock_db.get_all_books.return_value = []
    mock_db.get_book_by_ref.return_value = None
    mock_db.get_book_by_kosync_id.return_value = None
    mock_db.get_kosync_doc_by_filename.return_value = None
    mock_db.get_all_actionable_suggestions.return_value = []
    mock_db.get_bookfusion_books.return_value = []


# ── _create_book_mapping: Grimmory lookup fails ──────────────────


def test_create_book_mapping_grimmory_raises(flask_app, mock_container):
    """_create_book_mapping proceeds when find_in_grimmory raises internally.

    find_in_grimmory catches its own errors, so _create_book_mapping only sees
    (None, None). The mapping should still succeed if KOSync ID can be computed.
    """
    _setup_matching_db_defaults(mock_container.mock_database_service)

    mock_container.mock_grimmory_client.is_configured.return_value = True
    mock_container.mock_grimmory_client.find_book_by_filename.return_value = None

    with flask_app.app_context():
        with patch("src.blueprints.matching_bp.find_in_grimmory", return_value=(None, None)):
            with patch("src.blueprints.matching_bp.get_kosync_id_for_ebook", return_value="hash123"):
                from src.blueprints.matching_bp import _create_book_mapping

                book, error = _create_book_mapping(
                    mock_container,
                    abs_id="test-abs",
                    title="Test Book",
                    ebook_filename="test.epub",
                    duration=3600,
                )

    assert book is not None
    assert error is None
    mock_container.mock_database_service.save_book.assert_called()


def test_create_book_mapping_kosync_id_fails(flask_app, mock_container):
    """_create_book_mapping returns error when KOSync ID cannot be computed."""
    _setup_matching_db_defaults(mock_container.mock_database_service)

    mock_container.mock_grimmory_client.is_configured.return_value = False

    with flask_app.app_context():
        with patch("src.blueprints.matching_bp.get_kosync_id_for_ebook", return_value=None):
            from src.blueprints.matching_bp import _create_book_mapping

            book, error = _create_book_mapping(
                mock_container,
                abs_id="test-abs",
                title="Test Book",
                ebook_filename="test.epub",
                duration=3600,
            )

    assert book is None
    assert "KOSync ID" in error


def test_create_book_mapping_hardcover_automatch_fails(flask_app, mock_container):
    """_create_book_mapping still returns the book when Hardcover automatch throws."""
    _setup_matching_db_defaults(mock_container.mock_database_service)

    mock_container.mock_grimmory_client.is_configured.return_value = False
    mock_container.mock_hardcover_service.is_configured.return_value = True
    mock_container.mock_hardcover_service.automatch_hardcover.side_effect = Exception("HC timeout")

    with flask_app.app_context():
        with patch("src.blueprints.matching_bp.find_in_grimmory", return_value=(None, None)):
            with patch("src.blueprints.matching_bp.get_kosync_id_for_ebook", return_value="hash456"):
                from src.blueprints.matching_bp import _create_book_mapping

                book, error = _create_book_mapping(
                    mock_container,
                    abs_id="test-abs",
                    title="Test Book",
                    ebook_filename="test.epub",
                    duration=3600,
                )

    # Book is created despite HC failure
    assert book is not None
    assert error is None


def test_create_book_mapping_grimmory_add_to_shelf_fails(flask_app, mock_container):
    """_create_book_mapping logs but succeeds when Grimmory add_to_shelf throws."""
    _setup_matching_db_defaults(mock_container.mock_database_service)

    bl_client = Mock()
    bl_client.is_configured.return_value = True
    bl_client.add_to_shelf.side_effect = Exception("Shelf error")

    mock_container.mock_grimmory_client.is_configured.return_value = True
    mock_container.mock_grimmory_client.find_book_by_filename.return_value = {"id": 99, "_instance_id": "default"}
    mock_container.mock_ebook_parser.get_kosync_id_from_bytes.return_value = "hash789"
    bl_client.download_book.return_value = b"fake epub"
    mock_container.mock_ebook_parser.get_kosync_id_from_bytes.return_value = "hash789"

    # Patch find_in_grimmory to return our bl_client
    with flask_app.app_context():
        with patch("src.blueprints.matching_bp.find_in_grimmory", return_value=({"id": 99}, bl_client)):
            with patch("src.blueprints.matching_bp.get_kosync_id_for_ebook", return_value="hash789"):
                from src.blueprints.matching_bp import _create_book_mapping

                book, error = _create_book_mapping(
                    mock_container,
                    abs_id="test-abs-2",
                    title="Test Book 2",
                    ebook_filename="test2.epub",
                    duration=3600,
                )

    assert book is not None
    assert error is None
    bl_client.add_to_shelf.assert_called_once()


# ── Batch match: individual book failure continues ────────────────


def test_batch_match_keeps_same_filename_from_two_grimmory_servers(flask_app, mock_container):
    _setup_matching_db_defaults(mock_container.mock_database_service)

    with flask_app.test_client() as test_client:
        for source_id in ("default:11", "2:22"):
            response = test_client.post(
                "/batch-match",
                data={
                    "action": "add_to_queue",
                    "ebook_filename": "same.epub",
                    "ebook_source_id": source_id,
                    "ebook_display_name": source_id,
                },
            )
            assert response.status_code == 302

        with test_client.session_transaction() as sess:
            queue = sess["queue"]

    assert [(item["queue_key"], item["ebook_source_id"]) for item in queue] == [
        ("default:11", "default:11"),
        ("2:22", "2:22"),
    ]


def test_batch_match_process_continues_on_individual_failure(flask_app, mock_container, client):
    """Batch match should continue processing remaining items when one fails."""
    _setup_matching_db_defaults(mock_container.mock_database_service)

    call_count = {"n": 0}

    def create_mapping_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("First book fails")
        return Mock(), None

    with flask_app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["queue"] = [
                {
                    "queue_key": "abs-1",
                    "abs_id": "abs-1",
                    "title": "Book 1",
                    "ebook_filename": "book1.epub",
                    "duration": 100,
                },
                {
                    "queue_key": "abs-2",
                    "abs_id": "abs-2",
                    "title": "Book 2",
                    "ebook_filename": "book2.epub",
                    "duration": 200,
                },
            ]

        with patch("src.blueprints.matching_bp._create_book_mapping", side_effect=create_mapping_side_effect):
            response = test_client.post("/batch-match", data={"action": "process_queue"})

    # Should redirect to dashboard (302)
    assert response.status_code == 302
    # Both items were attempted (the second should succeed)
    assert call_count["n"] == 2


def test_batch_match_audio_only_continues_on_failure(flask_app, mock_container, client):
    """Batch match should continue when an audio-only item fails save."""
    _setup_matching_db_defaults(mock_container.mock_database_service)
    mock_container.mock_database_service.save_book.side_effect = [
        Exception("DB error on first save"),
        None,  # second save succeeds
    ]

    with flask_app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["queue"] = [
                {
                    "queue_key": "abs-audio",
                    "abs_id": "abs-audio",
                    "title": "Audio Book",
                    "ebook_filename": "",
                    "duration": 100,
                    "audio_only": True,
                },
                {
                    "queue_key": "abs-audio2",
                    "abs_id": "abs-audio2",
                    "title": "Audio Book 2",
                    "ebook_filename": "",
                    "duration": 200,
                    "audio_only": True,
                },
            ]

        response = test_client.post("/batch-match", data={"action": "process_queue"})

    assert response.status_code == 302


def test_batch_match_ebook_only_kosync_failure_adds_to_failed(flask_app, mock_container, client):
    """Ebook-only batch items that fail KOSync ID computation are added to failed list."""
    _setup_matching_db_defaults(mock_container.mock_database_service)

    with flask_app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["queue"] = [
                {
                    "queue_key": "ebook1.epub",
                    "abs_id": "",
                    "title": "Ebook Only",
                    "ebook_filename": "ebook1.epub",
                    "ebook_display_name": "My Ebook",
                    "duration": 0,
                    "ebook_only": True,
                },
            ]

        with patch("src.blueprints.matching_bp.find_in_grimmory", return_value=(None, None)):
            with patch("src.blueprints.matching_bp.get_kosync_id_for_ebook", return_value=None):
                response = test_client.post("/batch-match", data={"action": "process_queue"})

    # Should redirect with a flash warning about failed items
    assert response.status_code == 302


def test_batch_match_skips_standard_item_without_ebook(flask_app, mock_container, client):
    """Queued audiobook+ebook mappings without an ebook are marked failed before mapping."""
    _setup_matching_db_defaults(mock_container.mock_database_service)

    with flask_app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["queue"] = [
                {
                    "queue_key": "abs-missing-ebook",
                    "abs_id": "abs-missing-ebook",
                    "title": "Missing Ebook",
                    "ebook_filename": "",
                    "duration": 100,
                    "storyteller_uuid": "storyteller-id",
                    "audio_only": False,
                    "ebook_only": False,
                },
            ]

        with patch("src.blueprints.matching_bp._create_book_mapping") as create_mapping:
            response = test_client.post("/batch-match", data={"action": "process_queue"})

    assert response.status_code == 302
    create_mapping.assert_not_called()


# ── Suggestions page: serialize edge cases ────────────────────────


def test_suggestions_page_filters_no_matches(flask_app, mock_container):
    """Suggestions page filters out suggestions with empty matches."""
    _setup_matching_db_defaults(mock_container.mock_database_service)

    suggestion_no_matches = Mock()
    suggestion_no_matches.matches = []
    suggestion_no_matches.id = 1
    suggestion_no_matches.source_id = "abc"

    suggestion_with_matches = Mock()
    suggestion_with_matches.id = 2
    suggestion_with_matches.source_id = "def"
    suggestion_with_matches.source = "abs"
    suggestion_with_matches.title = "Test"
    suggestion_with_matches.author = None
    suggestion_with_matches.cover_url = None
    suggestion_with_matches.matches = [{"ebook_filename": "test.epub", "evidence": []}]
    suggestion_with_matches.created_at = None
    suggestion_with_matches.status = "pending"

    mock_container.mock_database_service.get_all_actionable_suggestions.return_value = [
        suggestion_no_matches,
        suggestion_with_matches,
    ]

    # Need ABS available for suggestions route
    flask_app.config["abs_service"] = Mock()
    flask_app.config["abs_service"].is_available.return_value = True
    flask_app.template_folder = str(Path(__file__).parent.parent / "templates")

    with flask_app.test_client() as test_client:
        response = test_client.get("/suggestions?view=library")

    assert response.status_code == 200
