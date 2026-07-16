from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.api.grimmory_client import GrimmoryClientGroup
from src.services.suggestion_service import SuggestionService
from src.sync_manager import SyncManager


def _service(db, *, abs_client=None, grimmory=None, storyteller=None):
    return SuggestionService(
        database_service=db,
        abs_client=abs_client,
        grimmory_client=grimmory,
        storyteller_client=storyteller,
        library_service=None,
        books_dir=Path("/missing"),
        ebook_parser=None,
    )


def _db():
    db = Mock()
    db.get_all_books.return_value = []
    db.get_all_actionable_suggestions.return_value = []
    db.get_unlinked_kosync_documents.return_value = []
    db.suggestion_exists.return_value = False
    return db


def test_storyteller_only_detection_survives_abs_and_grimmory_failures():
    db = _db()
    storyteller = Mock()
    storyteller.is_configured.return_value = True
    storyteller.get_all_positions_bulk.return_value = {
        "solo read": {"uuid": "st-1", "pct": 0.42, "ts": 1_700_000_000_000}
    }
    grimmory = Mock()
    grimmory.is_configured.return_value = True
    grimmory.get_all_books.side_effect = RuntimeError("server down")

    _service(db, grimmory=grimmory, storyteller=storyteller).check_for_suggestions({}, [])

    detected = db.save_detected_book.call_args.args[0]
    assert detected.source == "storyteller"
    assert detected.source_id == "st-1"
    assert detected.progress_percentage == 0.42
    assert detected.source_updated_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_scheduled_discovery_runs_without_abs_bulk_state():
    manager = object.__new__(SyncManager)
    manager.database_service = Mock()
    manager.database_service.get_books_by_status.return_value = []
    manager.sync_clients = {"Storyteller": Mock()}
    manager.sync_clients["Storyteller"].fetch_bulk_state.return_value = {}
    manager.check_for_suggestions = Mock()

    manager._prepare_sync_books(None)

    manager.check_for_suggestions.assert_called_once_with({}, [])


def test_manual_rescan_runs_currently_reading_discovery():
    db = _db()
    service = _service(db)
    service.check_for_suggestions = Mock()
    service.rescan_library_suggestions = Mock(
        return_value={
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "total": 0,
            "bookfusion_catalog": False,
        }
    )

    service._run_rescan_job()

    service.check_for_suggestions.assert_called_once_with({}, [], errors=set())
    service.rescan_library_suggestions.assert_not_called()


def test_catalog_flag_does_not_disable_currently_reading_detection(monkeypatch):
    monkeypatch.setenv("SUGGESTIONS_ENABLED", "false")
    db = _db()
    service = _service(db)
    service._create_suggestion = Mock()
    service._check_reverse_suggestions = Mock()
    service._check_cross_ebook_suggestions = Mock()

    service.check_for_suggestions(
        {"abs-1": {"mediaType": "audiobook", "duration": 100, "currentTime": 42}},
        [],
    )
    service._save_suggestion_with_merge("abs", "abs-1", "Book", None, "", [{"title": "Candidate"}])

    service._create_suggestion.assert_called_once_with(
        "abs-1", {"mediaType": "audiobook", "duration": 100, "currentTime": 42}
    )
    db.save_pending_suggestion.assert_not_called()


def test_grimmory_instances_with_same_filename_keep_separate_identity_and_progress():
    db = _db()
    first = Mock(instance_id="default")
    first.is_configured.return_value = True
    first.get_all_books.return_value = [{"id": 10, "title": "First", "fileName": "same.epub"}]
    first.get_progress.return_value = (0.25, None)
    second = Mock(instance_id="2")
    second.is_configured.return_value = True
    second.get_all_books.return_value = [{"id": 20, "title": "Second", "fileName": "same.epub"}]
    second.get_progress.return_value = (0.75, None)

    _service(db, grimmory=GrimmoryClientGroup([first, second]))._check_cross_ebook_suggestions()

    detected = [call.args[0] for call in db.save_detected_book.call_args_list]
    assert [(row.source_id, row.progress_percentage) for row in detected] == [
        ("default:same.epub", 0.25),
        ("2:same.epub", 0.75),
    ]
    first.get_progress.assert_called_once_with("same.epub")
    second.get_progress.assert_called_once_with("same.epub")


def test_mapped_grimmory_server_does_not_hide_same_filename_on_other_server():
    db = _db()
    db.get_all_books.return_value = [
        SimpleNamespace(
            ebook_filename="same.epub",
            kosync_doc_id="mapped-hash",
            storyteller_uuid=None,
        )
    ]
    db.get_kosync_document.return_value = SimpleNamespace(source="grimmory", grimmory_id="2:20")
    first = Mock(instance_id="default")
    first.is_configured.return_value = True
    first.get_all_books.return_value = [{"id": 10, "title": "First", "fileName": "same.epub"}]
    first.get_progress.return_value = (0.25, None)
    second = Mock(instance_id="2")
    second.is_configured.return_value = True
    second.get_all_books.return_value = [{"id": 20, "title": "Second", "fileName": "same.epub"}]
    second.get_progress.return_value = (0.75, None)

    _service(db, grimmory=GrimmoryClientGroup([first, second]))._check_cross_ebook_suggestions()

    detected = [call.args[0] for call in db.save_detected_book.call_args_list]
    assert [(row.source, row.source_id) for row in detected] == [("grimmory", "default:same.epub")]
    first.get_progress.assert_called_once_with("same.epub")
    second.get_progress.assert_not_called()


def test_malformed_ebook_source_records_do_not_hide_later_healthy_records():
    db = _db()
    db.get_unlinked_kosync_documents.return_value = [
        SimpleNamespace(document_hash="bad", filename="bad.epub", percentage="not-a-number", linked_abs_id=None),
        SimpleNamespace(
            document_hash="good-hash",
            filename="Good KoSync.epub",
            percentage=0.4,
            device="KOReader",
            timestamp=None,
            linked_abs_id=None,
        ),
    ]
    storyteller = Mock()
    storyteller.is_configured.return_value = True
    storyteller.get_all_positions_bulk.return_value = {
        "broken": None,
        "good storyteller": {"uuid": "st-good", "pct": 0.3},
    }
    grimmory = Mock()
    grimmory.is_configured.return_value = True
    grimmory.get_all_books.return_value = [
        None,
        {"id": 10, "title": "Good Grimmory", "fileName": "good.epub", "_instance_id": "default"},
    ]
    grimmory.get_progress.return_value = (0.2, None)

    _service(db, grimmory=grimmory, storyteller=storyteller)._check_cross_ebook_suggestions()

    detected = {(call.args[0].source, call.args[0].source_id) for call in db.save_detected_book.call_args_list}
    assert detected == {
        ("storyteller", "st-good"),
        ("grimmory", "default:good.epub"),
        ("kosync", "good-hash"),
    }


def test_kosync_scheduled_detection_uses_real_progress_and_timestamp():
    db = _db()
    source_time = datetime(2026, 7, 15, 12, tzinfo=UTC)
    db.get_unlinked_kosync_documents.return_value = [
        SimpleNamespace(
            document_hash="abc123",
            filename="Real Read.epub",
            percentage=0.61,
            device="KOReader",
            timestamp=source_time,
            linked_abs_id=None,
        )
    ]

    _service(db)._check_cross_ebook_suggestions()

    detected = db.save_detected_book.call_args.args[0]
    assert detected.source == "kosync"
    assert detected.progress_percentage == 0.61
    assert detected.source_updated_at == source_time


def test_abs_ebook_bulk_payload_is_not_misclassified_as_audiobook():
    db = _db()
    service = _service(db)
    service._create_suggestion = Mock()
    service._check_reverse_suggestions = Mock()
    service._check_cross_ebook_suggestions = Mock()

    service.check_for_suggestions(
        {"ebook-1": {"mediaType": "ebook", "duration": 100, "currentTime": 50}},
        [],
    )

    service._create_suggestion.assert_not_called()


@pytest.mark.parametrize(
    ("pct", "expected"),
    [(0.0, False), (0.01, True), (0.95, True), (1.0, False)],
)
def test_abs_and_kosync_events_share_detection_window(pct, expected):
    db = _db()
    abs_client = Mock()
    abs_client.get_all_audiobooks.return_value = []
    abs_client.get_item_details.return_value = {
        "media": {"metadata": {"title": "Window Book", "authorName": "Reader"}}
    }
    service = _service(db, abs_client=abs_client)
    service._check_reverse_suggestions = Mock(return_value=[])
    service._check_cross_ebook_suggestions = Mock()

    service.queue_suggestion(
        "abs-window",
        {"progress": pct, "lastUpdate": "2026-07-15T12:00:00Z"},
    )
    abs_saved = db.save_detected_book.call_count
    db.save_detected_book.reset_mock()
    service.queue_kosync_suggestion(
        "hash-window",
        filename="Window Book.epub",
        progress_percentage=pct,
        source_updated_at="2026-07-15T12:00:00Z",
    )

    assert bool(abs_saved) is expected
    assert bool(db.save_detected_book.call_count) is expected
    if expected:
        detected = db.save_detected_book.call_args.args[0]
        assert detected.progress_percentage == pct
        assert detected.source_updated_at == datetime(2026, 7, 15, 12, tzinfo=UTC)


def test_scheduled_ebook_detection_merges_ranked_abs_and_live_candidates():
    db = _db()
    db.get_unlinked_kosync_documents.return_value = [
        SimpleNamespace(
            document_hash="hash-merge",
            filename="Merge Book.epub",
            percentage=0.4,
            device="KOReader",
            timestamp=datetime(2026, 7, 15, 12, tzinfo=UTC),
            linked_abs_id=None,
        )
    ]
    live_match = {
        "source_family": "grimmory",
        "source_key": "grimmory:default:Merge Book.epub",
        "title": "Merge Book",
        "score": 0.9,
    }
    db.get_detected_book.side_effect = lambda source_id, source="abs": (
        SimpleNamespace(matches=[live_match]) if (source, source_id) == ("kosync", "hash-merge") else None
    )
    abs_client = Mock()
    abs_client.get_all_audiobooks.return_value = [
        {
            "id": "abs-merge",
            "media": {"metadata": {"title": "Merge Book", "authorName": "Reader"}},
        }
    ]

    _service(db, abs_client=abs_client).check_for_suggestions({}, [])

    detected = next(
        call.args[0]
        for call in db.save_detected_book.call_args_list
        if call.args[0].source == "kosync"
    )
    source_keys = {match.get("source_key") for match in detected.matches}
    assert source_keys == {"abs:abs-merge", "grimmory:default:Merge Book.epub"}


def test_source_failure_marks_rescan_partial_and_keeps_activity_results():
    db = _db()
    db.get_active_detected_book_count.return_value = 1
    db.get_unlinked_kosync_documents.return_value = [
        SimpleNamespace(
            document_hash="healthy",
            filename="Healthy.epub",
            percentage=0.4,
            device="KOReader",
            timestamp=datetime(2026, 7, 15, 12, tzinfo=UTC),
            linked_abs_id=None,
        )
    ]
    abs_client = Mock()
    abs_client.is_configured.return_value = True
    abs_client.get_all_progress_raw.side_effect = RuntimeError("offline")
    abs_client.get_all_audiobooks.side_effect = RuntimeError("offline")
    service = _service(db, abs_client=abs_client)
    service.rescan_library_suggestions = Mock()

    service._run_rescan_job()

    assert service.get_rescan_status()["phase"] == "partial"
    assert service.get_rescan_status()["failed_sources"] == ["Audiobookshelf"]
    assert service.get_rescan_status()["detected"] == 1
    assert any(call.args[0].source_id == "healthy" for call in db.save_detected_book.call_args_list)
    service.rescan_library_suggestions.assert_not_called()
