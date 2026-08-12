"""Behavior-freeze: UI JSON API response-shape snapshots.

The vanilla-JS frontend calls ~50 ``/api/*`` endpoints and depends on their
*exact, heterogeneous* response shapes (some return a bare array, some a keyed
map, some ``{success: ...}``). There is intentionally **no uniform envelope**;
each endpoint's real shape is the contract. These tests freeze a representative,
deterministic subset so backend cleanup behind these endpoints cannot silently
change a shape the JS reads.

This is the Stage 01 scaffold, not exhaustive coverage. It establishes the
reusable snapshot helper (``tests/snapshot_helpers.py``) plus high-value
endpoints. More endpoints can be added incrementally as later stages touch them.

Endpoints chosen because they are fully driven by ``MockContainer``:

* ``GET /api/status``            — keyed map ``{mappings: [...]}``
* ``GET /api/processing-status`` — raw ``{book_id: {...}}`` map (no envelope)
* ``GET /api/suggestions``       — raw array
* ``GET /api/logs``              — keyed map; shape frozen, content is runtime-variable

To regenerate after a *reviewed* shape change::

    PAGEKEEPER_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_ui_api_snapshots.py

See ``tests/snapshots/README.md``.
"""

from src.db.models import Book, State
from tests.snapshot_helpers import snapshot_shape, snapshot_value


def test_api_status_empty_value(client, mock_container):
    """Empty database → exact ``{mappings: []}``."""
    mock_container.mock_database_service.get_all_books.return_value = []
    mock_container.mock_database_service.get_all_states.return_value = []

    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.content_type == "application/json"
    snapshot_value("api_status_empty", response.get_json())


def test_api_status_populated_shape(client, mock_container):
    """One book with multiple client states → freeze the per-mapping shape.

    Uses shape-only freezing because some leaf values (percentages) are
    computed, but the key set per mapping and per state is the real contract.
    """
    book = Book(
        abs_id="snapshot-book",
        title="Snapshot Book",
        ebook_filename="snapshot.epub",
        kosync_doc_id="snapshot-doc",
        status="active",
        sync_mode="audiobook",
        duration=3600,
    )
    book.id = 1

    states = [
        State(book_id=1, abs_id="snapshot-book", client_name="kosync", percentage=0.45, last_updated=1000),
        State(book_id=1, abs_id="snapshot-book", client_name="abs", percentage=0.44, timestamp=1584, last_updated=900),
        State(
            book_id=1,
            abs_id="snapshot-book",
            client_name="storyteller",
            percentage=0.40,
            last_updated=950,
        ),
        State(book_id=1, abs_id="snapshot-book", client_name="grimmory", percentage=0.42, last_updated=920),
    ]

    mock_container.mock_database_service.get_all_books.return_value = [book]
    mock_container.mock_database_service.get_all_states.return_value = states

    response = client.get("/api/status")
    assert response.status_code == 200
    snapshot_shape("api_status_populated", response.get_json())


def test_api_processing_status_empty_value(client, mock_container):
    """No processing books → exact empty map ``{}`` (no envelope)."""
    mock_container.mock_database_service.get_all_books.return_value = []
    mock_container.mock_database_service.get_latest_jobs_bulk.return_value = {}

    response = client.get("/api/processing-status")
    assert response.status_code == 200
    snapshot_value("api_processing_status_empty", response.get_json())


def test_api_processing_status_populated_shape(client, mock_container):
    """A pending book with a job → freeze the per-book entry shape."""
    book = Book(abs_id="proc-book", title="Processing Book", status="pending")
    book.id = 7

    class _Job:
        progress = 0.5
        retry_count = 2

    mock_container.mock_database_service.get_all_books.return_value = [book]
    mock_container.mock_database_service.get_latest_jobs_bulk.return_value = {7: _Job()}

    response = client.get("/api/processing-status")
    assert response.status_code == 200
    snapshot_shape("api_processing_status_populated", response.get_json())


def test_api_suggestions_empty_value(client, mock_container):
    """No actionable suggestions → exact empty array (bare array, no envelope)."""
    mock_container.mock_database_service.get_all_actionable_suggestions.return_value = []

    response = client.get("/api/suggestions")
    assert response.status_code == 200
    snapshot_value("api_suggestions_empty", response.get_json())


def test_api_logs_shape(client):
    """Freeze the top-level key shape of ``/api/logs``.

    Content depends on the runtime log file, so only the structure is frozen:
    ``{logs: [...], total_lines, displayed_lines, has_more}``.
    """
    response = client.get("/api/logs")
    assert response.status_code == 200
    payload = response.get_json()
    # Top-level keys are the contract; freeze them exactly.
    snapshot_value("api_logs_keys", sorted(payload.keys()))
