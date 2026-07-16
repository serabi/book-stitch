from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.services.book_intake_service import BookIntakeService
from src.services.storyteller_submission_service import SubmissionResult


def _book_ref(**overrides):
    defaults = {
        "id": 11,
        "abs_id": "source-abs",
        "ebook_filename": "source.epub",
        "original_ebook_filename": None,
        "kosync_doc_id": None,
        "storyteller_uuid": None,
        "abs_ebook_item_id": None,
        "ebook_item_id": None,
        "custom_cover_url": None,
        "started_at": None,
        "finished_at": None,
        "rating": None,
        "read_count": 1,
        "status": "not_started",
        "transcript_file": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_service(*, db=None, abs_service=None, bl_match=None, bl_client=None, kosync_id="hash-new"):
    container = Mock()
    container.grimmory_client.return_value = Mock()
    container.abs_client.return_value.get_audio_files.return_value = []
    container.storyteller_submission_service.return_value.is_available.return_value = False

    if db is None:
        db = Mock()
        db.get_book_by_ref.return_value = None
        db.get_book_by_kosync_id.return_value = None
        db.get_kosync_doc_by_filename.return_value = None
        db.get_kosync_document.return_value = None

    next_id = {"value": 100}

    def save_book(book, *args, **kwargs):
        if not getattr(book, "id", None):
            book.id = next_id["value"]
            next_id["value"] += 1
        return book

    db.save_book.side_effect = save_book

    abs_service = abs_service or Mock()
    bl_client = bl_client or Mock()
    find_in_grimmory = Mock(return_value=(bl_match, bl_client if bl_match else None))
    get_kosync_id_for_ebook = Mock(return_value=kosync_id)
    attempt_hardcover_automatch = Mock()

    service = BookIntakeService(
        container=container,
        database_service=db,
        abs_service=abs_service,
        collection_name="Synced",
        books_dir="/books",
        epub_cache_dir="/cache",
        find_in_grimmory=find_in_grimmory,
        get_kosync_id_for_ebook=get_kosync_id_for_ebook,
        attempt_hardcover_automatch=attempt_hardcover_automatch,
    )
    return service, db, abs_service, bl_client, attempt_hardcover_automatch


def _run_storyteller_submission_synchronously(service, *, abs_id="abs-story", title="Story Book", ebook="story.epub"):
    def thread_factory(*, target, daemon):
        thread = Mock()
        thread.daemon = daemon
        thread.start.side_effect = target
        return thread

    with patch("src.services.book_intake_service.threading.Thread", side_effect=thread_factory):
        service._submit_to_storyteller_async(abs_id, title, ebook)


def test_map_audiobook_ebook_uses_selected_edition_hash_over_existing_abs_hash():
    db = Mock()
    db.get_book_by_ref.return_value = _book_ref(abs_id="abs-1", kosync_doc_id="hash-existing")
    service, db, _abs, _bl, _hc = _make_service(db=db, kosync_id="hash-new")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Book",
        ebook_filename="book.epub",
        duration=123,
    )

    assert result.error is None
    assert result.book.kosync_doc_id == "hash-new"
    db.resolve_suggestion.assert_any_call("hash-new")


def test_map_audiobook_ebook_merges_duplicate_book_data_and_metadata():
    existing = _book_ref(
        id=22,
        abs_id="ebook-source",
        ebook_filename="old.epub",
        original_ebook_filename="original.epub",
        kosync_doc_id="hash-dup",
        storyteller_uuid="story-1",
        abs_ebook_item_id="ebook-item",
        custom_cover_url="https://cover",
        read_count=3,
    )
    db = Mock()
    db.get_book_by_ref.return_value = None
    db.get_book_by_kosync_id.return_value = existing
    db.migrate_book_data_by_id.side_effect = lambda _book_id, target_abs, **kwargs: SimpleNamespace(
        id=22, abs_id=target_abs, **kwargs["overrides"]
    )
    service, db, abs_service, _bl, _hc = _make_service(db=db, kosync_id="hash-dup")

    result = service.map_audiobook_ebook(
        abs_id="abs-new",
        title="Merged Book",
        ebook_filename="new.epub",
        duration=456,
        confirm_combine=True,
        confirmed_merge_book_id=22,
    )

    assert result.error is None
    assert result.book.original_ebook_filename == "original.epub"
    assert result.book.ebook_item_id == "ebook-item"
    assert result.book.custom_cover_url == "https://cover"
    assert result.book.read_count == 3
    assert db.migrate_book_data_by_id.call_args.args == (22, "abs-new")
    assert db.migrate_book_data_by_id.call_args.kwargs["expected_kosync_doc_id"] == "hash-dup"
    db.delete_book.assert_not_called()
    abs_service.add_to_collection.assert_called_once_with("abs-new", "Synced")


@pytest.mark.parametrize("transcript_file", ["DB_MANAGED", "/data/transcripts/legacy.json"])
def test_map_audiobook_ebook_preserves_existing_transcript_marker(transcript_file):
    existing = _book_ref(
        id=22,
        abs_id=None,
        kosync_doc_id="hash-dup",
        transcript_file=transcript_file,
    )
    db = Mock()
    db.get_book_by_ref.return_value = None
    db.get_book_by_kosync_id.return_value = existing
    db.migrate_book_data_by_id.side_effect = lambda _book_id, target_abs, **kwargs: SimpleNamespace(
        id=22, abs_id=target_abs, **kwargs["overrides"]
    )
    service, db, _abs, _bl, _hc = _make_service(db=db, kosync_id="hash-dup")

    result = service.map_audiobook_ebook(
        abs_id="abs-new",
        title="Merged Book",
        ebook_filename="new.epub",
        duration=456,
        confirm_combine=True,
        confirmed_merge_book_id=22,
    )

    assert result.error is None
    assert result.book.transcript_file == transcript_file
    assert db.migrate_book_data_by_id.call_args.kwargs["overrides"]["transcript_file"] == transcript_file


def test_map_audiobook_ebook_merges_ebook_only_book_by_integer_id():
    existing = _book_ref(
        id=22,
        abs_id=None,
        ebook_filename="old.epub",
        kosync_doc_id="hash-dup",
    )
    db = Mock()
    db.get_book_by_ref.return_value = None
    db.get_book_by_kosync_id.return_value = existing
    db.migrate_book_data_by_id.side_effect = lambda _book_id, target_abs, **kwargs: SimpleNamespace(
        id=22, abs_id=target_abs, **kwargs["overrides"]
    )
    service, db, _abs_service, _bl, _hc = _make_service(db=db, kosync_id="hash-dup")

    result = service.map_audiobook_ebook(
        abs_id="abs-new",
        title="Merged Book",
        ebook_filename="new.epub",
        duration=456,
        confirm_combine=True,
        confirmed_merge_book_id=22,
    )

    assert result.error is None
    assert db.migrate_book_data_by_id.call_args.args == (22, "abs-new")


def test_storyteller_reservation_happens_before_async_submission_thread():
    events = []
    service, db, _abs, _bl, _hc = _make_service()
    db.get_book_by_ref.side_effect = [None, _book_ref(id=100, abs_id="abs-story")]
    db.save_storyteller_submission.side_effect = lambda submission: events.append(("reservation", submission.abs_id))

    thread = Mock()
    thread.start.side_effect = lambda: events.append(("thread_start", None))

    with patch("src.services.book_intake_service.threading.Thread", return_value=thread):
        result = service.map_audiobook_ebook(
            abs_id="abs-story",
            title="Story Book",
            ebook_filename="story.epub",
            duration=789,
            storyteller_submit=True,
        )

    assert result.error is None
    assert events == [("reservation", "abs-story"), ("thread_start", None)]


def test_storyteller_reservation_returns_none_when_book_not_found(caplog):
    service, db, _abs, _bl, _hc = _make_service()

    with caplog.at_level("WARNING"):
        submission = service._create_storyteller_reservation("missing-abs")

    assert submission is None
    assert "Cannot create Storyteller reservation: book not found for abs_id=missing-abs" in caplog.messages
    db.save_storyteller_submission.assert_not_called()


def test_storyteller_submission_marks_reservation_failed_when_service_unavailable():
    service, db, _abs, _bl, _hc = _make_service()
    db.get_book_by_abs_id.return_value = _book_ref(id=42, abs_id="abs-story")
    db.get_active_storyteller_submission_by_book_id.return_value = SimpleNamespace(id=84)

    _run_storyteller_submission_synchronously(service)

    db.update_storyteller_submission_status.assert_called_once_with(84, "failed")
    service.container.storyteller_submission_service.return_value.submit_book.assert_not_called()


def test_storyteller_submission_marks_reservation_failed_when_submit_book_fails():
    service, db, _abs, _bl, _hc = _make_service()
    db.get_book_by_abs_id.return_value = _book_ref(id=42, abs_id="abs-story")
    db.get_active_storyteller_submission_by_book_id.return_value = SimpleNamespace(id=84)
    storyteller_service = service.container.storyteller_submission_service.return_value
    storyteller_service.is_available.return_value = True
    storyteller_service.submit_book.return_value = SubmissionResult(success=False, error="copy failed")
    service.container.abs_client.return_value.get_audio_files.return_value = [{"stream_url": "https://audio"}]

    with patch("src.utils.epub_resolver.get_local_epub", return_value="/cache/story.epub"):
        _run_storyteller_submission_synchronously(service)

    db.update_storyteller_submission_status.assert_called_once_with(84, "failed")
    storyteller_service.submit_book.assert_called_once()


def test_map_audiobook_ebook_resolves_abs_hash_and_device_suggestions():
    db = Mock()
    db.get_book_by_ref.return_value = None
    db.get_book_by_kosync_id.return_value = None
    db.get_kosync_doc_by_filename.return_value = SimpleNamespace(document_hash="device-hash")
    service, db, _abs, _bl, _hc = _make_service(db=db, kosync_id="primary-hash")

    result = service.map_audiobook_ebook(
        abs_id="abs-suggest",
        title="Suggest Book",
        ebook_filename="suggest.epub",
        duration=100,
    )

    assert result.error is None
    db.resolve_suggestion.assert_any_call("abs-suggest")
    db.resolve_suggestion.assert_any_call("primary-hash")
    db.resolve_suggestion.assert_any_call("device-hash")


def test_map_audiobook_ebook_updates_abs_collection_and_grimmory_shelf():
    service, _db, abs_service, bl_client, _hc = _make_service(bl_match={"id": "grimmory-1"})

    result = service.map_audiobook_ebook(
        abs_id="abs-side-effects",
        title="Side Effects",
        ebook_filename="side.epub",
        duration=321,
    )

    assert result.error is None
    abs_service.add_to_collection.assert_called_once_with("abs-side-effects", "Synced")
    bl_client.add_to_shelf.assert_called_once_with("side.epub")


def test_map_audiobook_ebook_persists_and_resolves_qualified_grimmory_identity():
    doc = SimpleNamespace(
        document_hash="hash-server-2",
        linked_book_id=100,
        filename="same.epub",
        source=None,
        grimmory_id=None,
    )
    db = Mock()
    db.get_book_by_ref.return_value = None
    db.get_book_by_kosync_id.return_value = None
    db.get_kosync_doc_by_filename.return_value = None
    db.get_kosync_document.return_value = doc
    service, db, _abs, bl_client, _hc = _make_service(
        db=db,
        bl_match={"id": "22", "fileName": "same.epub"},
        kosync_id="hash-server-2",
    )

    result = service.map_audiobook_ebook(
        abs_id="abs-server-2",
        title="Server Two",
        ebook_filename="same.epub",
        ebook_source_id="2:22",
        duration=100,
    )

    assert result.error is None
    service.find_in_grimmory.assert_called_once_with("same.epub", "2:22")
    service.get_kosync_id_for_ebook.assert_called_once_with("same.epub", "22", bl_client=bl_client)
    assert (doc.source, doc.grimmory_id) == ("grimmory", "2:22")
    db.resolve_detected_book.assert_any_call("2:same.epub", source="grimmory")
    assert not any(
        call.args == ("default:same.epub",) and call.kwargs == {"source": "grimmory"}
        for call in db.resolve_detected_book.call_args_list
    )


def test_pairing_review_rejects_changed_kosync_edition_before_write():
    service, db, abs_service, _bl, _hc = _make_service(kosync_id="different-hash")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="exact.epub",
        duration=100,
        detected_source="kosync",
        detected_source_id="expected-hash",
        expected_ebook_kosync_id="expected-hash",
    )

    assert result.status_code == 409
    assert "no longer matches" in result.error
    db.save_book.assert_not_called()
    abs_service.add_to_collection.assert_not_called()


def test_pairing_review_same_mapping_retry_reconciles_side_effects_before_completion():
    existing = _book_ref(abs_id="abs-1", ebook_filename="exact.epub", kosync_doc_id="hash-exact")
    db = Mock()
    db.get_book_by_ref.return_value = existing
    db.get_kosync_document.return_value = None
    db.claim_detected_book.return_value = "owner-token"
    service, db, abs_service, _bl, hc = _make_service(db=db, kosync_id="hash-exact")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="exact.epub",
        duration=100,
        detected_source="abs",
        detected_source_id="abs-1",
    )

    assert result.error is None
    db.save_book.assert_called_once()
    abs_service.add_to_collection.assert_called_once_with("abs-1", "Synced")
    hc.assert_called_once()
    db.claim_detected_book.assert_called_once_with("abs-1", source="abs")
    assert db.renew_detected_book_claim.call_count == 2
    assert all(
        call.args == ("abs-1", "owner-token") and call.kwargs == {"source": "abs"}
        for call in db.renew_detected_book_claim.call_args_list
    )
    db.complete_detected_book.assert_called_once_with("abs-1", "owner-token", source="abs")


def test_combine_conflict_claims_then_restores_before_any_write_or_side_effect():
    current = _book_ref(id=10, abs_id="abs-1", kosync_doc_id="old-hash")
    merge_source = _book_ref(id=22, abs_id=None, kosync_doc_id="selected-hash")
    db = Mock()
    db.get_book_by_ref.return_value = current
    db.get_book_by_kosync_id.return_value = merge_source
    db.claim_detected_book.return_value = True
    service, db, abs_service, _bl, hc = _make_service(db=db, kosync_id="selected-hash")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="selected.epub",
        duration=100,
        detected_source="abs",
        detected_source_id="abs-1",
    )

    assert result.conflict_code == "combine_required"
    assert result.conflict_book_id == 22
    db.restore_detected_book.assert_called_once_with("abs-1", True, source="abs")
    db.save_book.assert_not_called()
    db.migrate_book_data_by_id.assert_not_called()
    abs_service.add_to_collection.assert_not_called()
    hc.assert_not_called()


def test_confirmed_combine_migrates_captured_exact_book_id_with_null_target_hash():
    current = _book_ref(id=10, abs_id="abs-1", kosync_doc_id=None)
    merge_source = _book_ref(id=22, abs_id=None, kosync_doc_id="selected-hash")
    db = Mock()
    db.get_book_by_ref.return_value = current
    db.get_book_by_kosync_id.return_value = merge_source
    db.claim_detected_book.return_value = True
    db.complete_detected_book.return_value = True
    db.migrate_book_data_by_id.side_effect = lambda _book_id, target_abs, **kwargs: SimpleNamespace(
        id=22, abs_id=target_abs, **kwargs["overrides"]
    )
    service, db, _abs, _bl, _hc = _make_service(db=db, kosync_id="selected-hash")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="selected.epub",
        duration=100,
        detected_source="abs",
        detected_source_id="abs-1",
        confirm_combine=True,
        confirmed_merge_book_id=22,
    )

    assert result.error is None
    assert db.migrate_book_data_by_id.call_args.args == (22, "abs-1")
    assert db.migrate_book_data_by_id.call_args.kwargs["expected_abs_id"] is None
    assert db.migrate_book_data_by_id.call_args.kwargs["overrides"]["kosync_doc_id"] == "selected-hash"
    db.save_book.assert_not_called()


def test_changed_merge_row_requires_new_confirmation():
    merge_source = _book_ref(id=23, abs_id=None, kosync_doc_id="selected-hash")
    db = Mock()
    db.get_book_by_ref.return_value = None
    db.get_book_by_kosync_id.return_value = merge_source
    service, db, _abs, _bl, _hc = _make_service(db=db, kosync_id="selected-hash")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="selected.epub",
        duration=100,
        confirm_combine=True,
        confirmed_merge_book_id=22,
    )

    assert result.conflict_book_id == 23
    db.migrate_book_data_by_id.assert_not_called()


def test_collection_exception_after_commit_still_completes_detection():
    existing = _book_ref(abs_id="abs-1", ebook_filename="exact.epub", kosync_doc_id="hash-exact")
    db = Mock()
    db.get_book_by_ref.return_value = existing
    db.get_book_by_kosync_id.return_value = existing
    db.claim_detected_book.return_value = True
    db.complete_detected_book.return_value = True
    abs_service = Mock()
    abs_service.add_to_collection.side_effect = RuntimeError("ABS unavailable")
    service, db, _abs, _bl, _hc = _make_service(
        db=db, abs_service=abs_service, kosync_id="hash-exact"
    )
    kwargs = {
        "abs_id": "abs-1",
        "title": "Exact Book",
        "ebook_filename": "exact.epub",
        "duration": 100,
        "detected_source": "abs",
        "detected_source_id": "abs-1",
    }

    result = service.map_audiobook_ebook(**kwargs)

    assert result.error is None
    assert result.book.kosync_doc_id == "hash-exact"
    assert db.save_book.call_count == 1
    assert abs_service.add_to_collection.call_count == 1
    db.restore_detected_book.assert_not_called()
    db.complete_detected_book.assert_called_once_with("abs-1", True, source="abs")


def test_false_collection_result_after_commit_still_completes_detection():
    existing = _book_ref(abs_id="abs-1", ebook_filename="exact.epub", kosync_doc_id="hash-exact")
    db = Mock()
    db.get_book_by_ref.return_value = existing
    db.get_book_by_kosync_id.return_value = existing
    db.claim_detected_book.return_value = "owner-token"
    db.complete_detected_book.return_value = True
    abs_service = Mock()
    abs_service.add_to_collection.return_value = False
    service, db, _abs, _bl, _hc = _make_service(db=db, abs_service=abs_service, kosync_id="hash-exact")
    kwargs = {
        "abs_id": "abs-1",
        "title": "Exact Book",
        "ebook_filename": "exact.epub",
        "duration": 100,
        "detected_source": "abs",
        "detected_source_id": "abs-1",
    }

    result = service.map_audiobook_ebook(**kwargs)

    assert result.error is None
    assert result.book.kosync_doc_id == "hash-exact"
    assert abs_service.add_to_collection.call_count == 1
    db.restore_detected_book.assert_not_called()
    db.complete_detected_book.assert_called_once_with("abs-1", "owner-token", source="abs")


def test_completion_lease_loss_after_commit_does_not_restore_detection():
    existing = _book_ref(abs_id="abs-1", ebook_filename="exact.epub", kosync_doc_id="hash-exact")
    db = Mock()
    db.get_book_by_ref.return_value = existing
    db.get_book_by_kosync_id.return_value = existing
    db.claim_detected_book.return_value = "owner-token"
    db.complete_detected_book.return_value = False
    service, db, _abs, _bl, _hc = _make_service(db=db, kosync_id="hash-exact")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="exact.epub",
        duration=100,
        detected_source="abs",
        detected_source_id="abs-1",
    )

    assert result.error is None
    assert result.book.kosync_doc_id == "hash-exact"
    assert db.renew_detected_book_claim.call_count == 2
    db.restore_detected_book.assert_not_called()


def test_confirmed_merge_identity_change_returns_typed_failure_without_side_effects():
    merge_source = _book_ref(id=22, abs_id="ebook-source", kosync_doc_id="selected-hash")
    db = Mock()
    db.get_book_by_ref.return_value = None
    db.get_book_by_kosync_id.return_value = merge_source
    db.claim_detected_book.return_value = "owner-token"
    db.migrate_book_data_by_id.return_value = None
    service, db, abs_service, _bl, hc = _make_service(db=db, kosync_id="selected-hash")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="selected.epub",
        duration=100,
        detected_source="abs",
        detected_source_id="abs-1",
        confirm_combine=True,
        confirmed_merge_book_id=22,
    )

    assert result.conflict_code == "combine_changed"
    assert result.status_code == 409
    db.restore_detected_book.assert_called_once_with("abs-1", "owner-token", source="abs")
    abs_service.add_to_collection.assert_not_called()
    hc.assert_not_called()


def test_lost_claim_stops_later_side_effects_and_cannot_restore_new_owner():
    existing = _book_ref(abs_id="abs-1", ebook_filename="exact.epub", kosync_doc_id="hash-exact")
    db = Mock()
    db.get_book_by_ref.return_value = existing
    db.get_book_by_kosync_id.return_value = existing
    db.claim_detected_book.return_value = "original-token"
    db.renew_detected_book_claim.side_effect = [True, False]
    service, db, abs_service, _bl, hc = _make_service(db=db, kosync_id="hash-exact")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="exact.epub",
        duration=100,
        detected_source="abs",
        detected_source_id="abs-1",
    )

    assert result.conflict_code == "claim_lost"
    db.save_book.assert_not_called()
    abs_service.add_to_collection.assert_not_called()
    hc.assert_not_called()
    db.restore_detected_book.assert_called_once_with("abs-1", "original-token", source="abs")
    db.complete_detected_book.assert_not_called()


def test_resolved_retry_requires_exact_existing_mapping():
    existing = _book_ref(abs_id="abs-1", ebook_filename="exact.epub", kosync_doc_id="hash-exact")
    db = Mock()
    db.get_book_by_ref.return_value = existing
    db.get_book_by_kosync_id.return_value = existing
    db.claim_detected_book.return_value = False
    db.get_detected_book.return_value = SimpleNamespace(status="resolved")
    service, db, abs_service, _bl, _hc = _make_service(db=db, kosync_id="hash-exact")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="exact.epub",
        duration=100,
        detected_source="abs",
        detected_source_id="abs-1",
    )

    assert result.book is existing
    db.save_book.assert_not_called()
    abs_service.add_to_collection.assert_not_called()


def test_concurrent_processing_claim_loser_returns_conflict_without_write():
    db = Mock()
    db.get_book_by_ref.return_value = None
    db.get_book_by_kosync_id.return_value = None
    db.claim_detected_book.return_value = False
    db.get_detected_book.return_value = SimpleNamespace(status="processing")
    service, db, abs_service, _bl, _hc = _make_service(db=db, kosync_id="hash-exact")

    result = service.map_audiobook_ebook(
        abs_id="abs-1",
        title="Exact Book",
        ebook_filename="exact.epub",
        duration=100,
        detected_source="abs",
        detected_source_id="abs-1",
    )

    assert result.status_code == 409
    assert "already being processed" in result.error
    db.save_book.assert_not_called()
    abs_service.add_to_collection.assert_not_called()
