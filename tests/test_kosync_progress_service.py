from datetime import UTC, datetime
from unittest.mock import Mock

from src.services.kosync_progress_service import KosyncProgressService


class _ServiceStub:
    def __init__(self):
        self._db = Mock()
        self._container = Mock()
        self._manager = Mock()
        self.start_discovery_if_available = Mock(return_value=False)
        self.run_put_auto_discovery = Mock()
        self.run_get_auto_discovery = Mock()
        self.resolve_book_by_sibling_hash = Mock(return_value=None)
        self.register_hash_for_book = Mock()
        self.serialize_progress = Mock(return_value={"document": "doc"})


def _progress_service(service):
    return KosyncProgressService(service, service._db, service._container, service._manager)


def test_handle_put_progress_rejects_invalid_percentage():
    service = _ServiceStub()
    progress = _progress_service(service)

    body, status = progress.handle_put_progress(
        {
            "document": "doc-123",
            "percentage": "not-a-number",
        },
        remote_addr="127.0.0.1",
    )

    assert status == 400
    assert body["error"] == "Invalid percentage value"


def test_handle_get_progress_returns_502_for_unknown_document_without_discovery():
    service = _ServiceStub()
    service._db.get_kosync_document.return_value = None
    service._db.get_book_by_kosync_id.return_value = None
    progress = _progress_service(service)

    body, status = progress.handle_get_progress("missing-doc", remote_addr="127.0.0.1")

    assert status == 502
    assert body["message"] == "Document not found on server"


def test_handle_put_progress_ignored_furthest_wins_uses_iso_timestamp():
    service = _ServiceStub()
    existing = Mock()
    existing.percentage = 0.8
    existing.device_id = "other-device"
    existing.timestamp = datetime(2026, 1, 15, 12, 30, tzinfo=UTC)
    existing.linked_book_id = None
    existing.linked_abs_id = None
    service._db.get_kosync_document.return_value = existing
    progress = _progress_service(service)

    body, status = progress.handle_put_progress(
        {
            "document": "doc-123",
            "percentage": 0.3,
            "device": "koreader",
            "device_id": "new-device",
        },
        remote_addr="127.0.0.1",
    )

    assert status == 200
    assert body["timestamp"] == "2026-01-15T12:30:00+00:00"


def test_handle_put_progress_forwards_real_progress_to_detection():
    service = _ServiceStub()
    service._db.get_kosync_document.return_value = None
    service._db.get_book_by_kosync_id.return_value = None
    suggestion_service = service._container.suggestion_service.return_value
    progress = _progress_service(service)

    _, status = progress.handle_put_progress(
        {
            "document": "doc-123",
            "percentage": 0.42,
            "device": "KOReader",
            "device_id": "device-1",
        },
        remote_addr="127.0.0.1",
    )

    assert status == 200
    kwargs = suggestion_service.queue_kosync_suggestion.call_args.kwargs
    assert kwargs["progress_percentage"] == 0.42
    assert kwargs["device"] == "KOReader"
    assert isinstance(kwargs["source_updated_at"], datetime)


def test_resolve_best_progress_handles_state_without_last_updated():
    service = _ServiceStub()
    book = Mock(id=42, title="Test Book")
    older = Mock(client_name="abs", last_updated=None, percentage=0.2, xpath="old", cfi=None)
    newer = Mock(client_name="storyteller", last_updated=1700000000.0, percentage=0.6, xpath="new", cfi=None)
    service._db.get_states_for_book.return_value = [older, newer]
    service._db.get_kosync_documents_for_book_by_book_id.return_value = []
    progress = _progress_service(service)

    body, status = progress.resolve_best_progress("doc-123", book)

    assert status == 200
    assert body["percentage"] == 0.6
    assert body["timestamp"] == 1700000000
