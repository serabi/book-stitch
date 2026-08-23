"""Tests for dashboard graceful degradation when services fail."""

from types import SimpleNamespace
from unittest.mock import Mock

from src.db.models import Book


def _setup_dashboard_db_defaults(mock_db):
    """Configure database_service mock with defaults the dashboard route needs."""
    mock_db.get_all_books.return_value = []
    mock_db.get_setting.return_value = None
    mock_db.get_states_by_book.return_value = {}
    mock_db.get_all_hardcover_details.return_value = []
    mock_db.get_grimmory_by_filename.return_value = {}
    mock_db.get_bookfusion_linked_book_ids.return_value = set()
    mock_db.get_bookfusion_highlight_counts_by_book_id.return_value = {}
    mock_db.get_all_storyteller_submissions_latest.return_value = {}
    mock_db.get_latest_jobs_bulk.return_value = {}


# ── ABS errors ────────────────────────────────────────────────────


def test_index_renders_when_abs_get_audiobooks_raises(flask_app, mock_container):
    """Dashboard should render 200 even when ABS get_audiobooks() throws."""
    _setup_dashboard_db_defaults(mock_container.mock_database_service)
    # Replace the abs_service in app config directly (it was wired at app creation)
    failing_abs = Mock()
    failing_abs.get_audiobooks.side_effect = Exception("ABS down")
    failing_abs.is_available.return_value = False
    flask_app.config["abs_service"] = failing_abs
    with flask_app.test_client() as client:
        response = client.get("/")
    assert response.status_code == 200


def test_index_renders_when_abs_service_unavailable(flask_app, mock_container):
    """Dashboard should render 200 when ABS service is not available."""
    _setup_dashboard_db_defaults(mock_container.mock_database_service)
    unavailable_abs = Mock()
    unavailable_abs.get_audiobooks.return_value = []
    unavailable_abs.is_available.return_value = False
    flask_app.config["abs_service"] = unavailable_abs
    with flask_app.test_client() as client:
        response = client.get("/")
    assert response.status_code == 200


# ── BookFusion errors ─────────────────────────────────────────────


def test_index_renders_when_bookfusion_linked_ids_raises(client, mock_container):
    """Dashboard should render 200 when BookFusion linked IDs query fails."""
    _setup_dashboard_db_defaults(mock_container.mock_database_service)
    mock_container.mock_database_service.get_bookfusion_linked_book_ids.side_effect = Exception("BookFusion DB error")
    response = client.get("/")
    assert response.status_code == 200


def test_index_renders_when_bookfusion_highlight_counts_raises(client, mock_container):
    """Dashboard should render 200 when BookFusion highlight counts query fails."""
    _setup_dashboard_db_defaults(mock_container.mock_database_service)
    mock_container.mock_database_service.get_bookfusion_highlight_counts_by_book_id.side_effect = Exception(
        "BookFusion highlights error"
    )
    response = client.get("/")
    assert response.status_code == 200


# ── Storyteller errors ────────────────────────────────────────────


def test_index_renders_when_storyteller_submissions_raises(flask_app, mock_container):
    """Dashboard should render 200 when Storyteller submission fetch fails."""
    _setup_dashboard_db_defaults(mock_container.mock_database_service)
    # Register a storyteller sync client so integrations["storyteller"] is True
    st_sync_client = Mock()
    st_sync_client.is_configured.return_value = True
    mock_container.sync_clients = lambda: {"storyteller": st_sync_client}
    mock_container.mock_database_service.get_all_storyteller_submissions_latest.side_effect = Exception(
        "Storyteller DB error"
    )
    with flask_app.test_client() as client:
        response = client.get("/")
    assert response.status_code == 200


# ── Empty state ───────────────────────────────────────────────────


def test_index_renders_with_no_books(client, mock_container):
    """Dashboard should render 200 with zero books (empty library)."""
    _setup_dashboard_db_defaults(mock_container.mock_database_service)
    response = client.get("/")
    assert response.status_code == 200


def test_index_shows_currently_reading_preview_with_no_mapped_books(client, mock_container):
    """Unresolved detected books remain visible before the first mapping exists."""
    _setup_dashboard_db_defaults(mock_container.mock_database_service)
    mock_container.mock_database_service.get_active_detected_books.return_value = [
        SimpleNamespace(
            id=1,
            source="kosync",
            source_id="hash-1",
            title="Unmapped Reader",
            author="Reader",
            cover_url=None,
            progress_percentage=0.3,
            first_detected_at=None,
            last_seen_at=None,
            matches=[],
            device="Kobo",
            ebook_filename=None,
        )
    ]

    page = client.get("/").get_data(as_text=True)

    assert "Untracked books in progress" in page
    assert "Open Reading Activity" in page
    assert "Unmapped Reader" in page
    assert "No books syncing yet" in page


def test_index_uses_metadata_overrides_over_source_enrichment(flask_app, mock_container):
    """Dashboard cards should prefer PageKeeper metadata overrides."""
    book = Book(
        abs_id="abs-1",
        title="default_source_title",
        author="Cached Author",
        ebook_filename="default_source_title.epub",
        status="completed",
        title_override="Override Title",
        author_override="Override Author",
    )
    book.id = 1
    db = mock_container.mock_database_service
    _setup_dashboard_db_defaults(db)
    db.get_all_books.return_value = [book]
    db.get_grimmory_by_filename.return_value = {
        "default_source_title.epub": [
            SimpleNamespace(
                title="Grimmory Title",
                authors="Grimmory Author",
                raw_metadata_dict={},
                server_id="1",
            )
        ]
    }

    abs_service = Mock()
    abs_service.get_audiobooks.return_value = [
        {"id": "abs-1", "media": {"metadata": {"authorName": "Live ABS Author", "subtitle": ""}}}
    ]
    abs_service.is_available.return_value = True
    abs_service.get_cover_proxy_url.return_value = "/covers/abs-1.jpg"
    flask_app.config["abs_service"] = abs_service

    with flask_app.test_client() as client:
        response = client.get("/")

    assert response.status_code == 200
    assert b"Override Title" in response.data
    assert b"Override Author" in response.data
    assert b"Grimmory Title" not in response.data
    assert b"Live ABS Author" not in response.data


# ── Multiple service failures ─────────────────────────────────────


def test_index_renders_when_all_external_services_fail(flask_app, mock_container):
    """Dashboard should render 200 even when ABS, BookFusion, and Storyteller all fail."""
    _setup_dashboard_db_defaults(mock_container.mock_database_service)

    # ABS failure (must patch app config directly)
    failing_abs = Mock()
    failing_abs.get_audiobooks.side_effect = Exception("ABS down")
    failing_abs.is_available.return_value = False
    flask_app.config["abs_service"] = failing_abs

    # BookFusion failure
    mock_container.mock_database_service.get_bookfusion_linked_book_ids.side_effect = Exception("BF down")
    mock_container.mock_database_service.get_bookfusion_highlight_counts_by_book_id.side_effect = Exception("BF down")

    # Storyteller failure (need sync_clients to include storyteller)
    st_sync_client = Mock()
    st_sync_client.is_configured.return_value = True
    mock_container.sync_clients = lambda: {"storyteller": st_sync_client}
    mock_container.mock_database_service.get_all_storyteller_submissions_latest.side_effect = Exception("ST down")

    with flask_app.test_client() as client:
        response = client.get("/")
    assert response.status_code == 200
