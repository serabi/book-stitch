from types import SimpleNamespace
from unittest.mock import Mock, patch

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


def test_map_audiobook_ebook_preserves_existing_kosync_hash():
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
    assert result.book.kosync_doc_id == "hash-existing"
    db.resolve_suggestion.assert_any_call("hash-existing")


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
    service, db, abs_service, _bl, _hc = _make_service(db=db, kosync_id="hash-dup")

    result = service.map_audiobook_ebook(
        abs_id="abs-new",
        title="Merged Book",
        ebook_filename="new.epub",
        duration=456,
    )

    assert result.error is None
    assert result.book.original_ebook_filename == "original.epub"
    assert result.book.ebook_item_id == "ebook-item"
    assert result.book.custom_cover_url == "https://cover"
    assert result.book.read_count == 3
    db.migrate_book_data.assert_called_once_with("ebook-source", "abs-new")
    db.delete_book.assert_not_called()
    abs_service.add_to_collection.assert_called_once_with("abs-new", "Synced")


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
    service, db, _abs_service, _bl, _hc = _make_service(db=db, kosync_id="hash-dup")

    result = service.map_audiobook_ebook(
        abs_id="abs-new",
        title="Merged Book",
        ebook_filename="new.epub",
        duration=456,
    )

    assert result.error is None
    db.migrate_book_data.assert_called_once_with(22, "abs-new")


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
