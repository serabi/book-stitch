from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.sync_clients.grimmory_sync_client import GrimmoryAudioSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, ServiceState, SyncResult, UpdateProgressRequest
from src.sync_manager import SyncManager


def _audio_book():
    return {
        "id": 10,
        "fileName": "audio.m4b",
        "bookType": "AUDIOBOOK",
        "bookFileId": 42,
        "audiobookProgress": {"percentage": 35},
    }


def test_grimmory_audio_sync_client_uses_exact_identity_for_read_and_write():
    api = Mock()
    api.is_configured.return_value = True
    api.get_all_books.return_value = [_audio_book(), {"id": 20, "bookType": "EPUB"}]
    api.audio_source_id.side_effect = lambda book: "default:10:42" if book.get("bookType") == "AUDIOBOOK" else None
    api.extract_progress.return_value = (0.35, None)
    api.update_audiobook_progress.return_value = True
    client = GrimmoryAudioSyncClient(api, Mock(), client_name="GrimmoryAudio")
    book = SimpleNamespace(id=7, grimmory_audio_source_id="default:10:42")

    bulk = client.fetch_bulk_state()
    state = client.get_service_state(book, None, bulk_context=bulk)
    result = client.update_progress(book, UpdateProgressRequest(LocatorResult(percentage=0.6)))

    assert set(bulk) == {"default:10:42"}
    assert state.current["pct"] == pytest.approx(0.35)
    assert client.get_supported_sync_types() == {"audiobook"}
    api.update_audiobook_progress.assert_called_once_with("default:10:42", 0.6)
    assert result == SyncResult(0.6, True, {"pct": 0.6})


def test_sync_manager_uses_synthetic_state_identity_without_abs():
    manager = object.__new__(SyncManager)
    leader = Mock()
    leader.get_text_from_current_state.return_value = "chapter text"
    leader.get_locator_from_text.return_value = LocatorResult(percentage=0.4)
    follower = Mock()
    follower.update_progress.return_value = SyncResult(0.4, True, {"pct": 0.4})
    manager.sync_clients = {"GrimmoryAudio": leader, "KoSync": follower}
    manager.database_service = Mock()
    manager._determine_leader = Mock(return_value=("GrimmoryAudio", 0.4))
    book = SimpleNamespace(id=7, abs_id=None, title="Book", ebook_filename="book.epub")
    config = {
        "GrimmoryAudio": ServiceState(
            current={"pct": 0.4},
            previous_pct=0.2,
            delta=0.2,
            threshold=0.01,
            is_configured=True,
            display=("GrimmoryAudio", ""),
            value_formatter=str,
        ),
        "KoSync": ServiceState(
            current={"pct": 0.2},
            previous_pct=0.2,
            delta=0,
            threshold=0.01,
            is_configured=True,
            display=("KoSync", ""),
            value_formatter=str,
        ),
    }

    manager._execute_sync_update(book, config, "book-7", "Book", manager.sync_clients)

    saved_states = [call.args[0] for call in manager.database_service.save_state.call_args_list]
    assert len(saved_states) == 2
    assert {state.abs_id for state in saved_states} == {"book-7"}


@pytest.mark.parametrize(
    ("source_id", "expected_audio_client"),
    [("default:10:42", "GrimmoryAudio"), ("2:10:42", "Grimmory2Audio")],
)
def test_audio_only_sync_uses_only_the_qualified_grimmory_instance(source_id, expected_audio_client):
    manager = object.__new__(SyncManager)
    manager.alignment_service = None
    manager.database_service = Mock()
    manager.database_service.get_states_for_book.return_value = []
    manager.sync_clients = {}
    for name in ("ABS", "Hardcover", "GrimmoryAudio", "Grimmory2Audio", "KoSync"):
        client = Mock()
        client.get_supported_sync_types.return_value = {"audiobook"}
        manager.sync_clients[name] = client
    manager._fetch_states_parallel = Mock(return_value={})
    book = SimpleNamespace(
        id=7,
        abs_id=None,
        title="Book",
        sync_mode="audiobook",
        kosync_doc_id=None,
        grimmory_audio_source_id=source_id,
    )

    manager._sync_single_book(book, {})

    active_clients = manager._fetch_states_parallel.call_args.args[-1]
    assert set(active_clients) == {"ABS", "Hardcover", expected_audio_client}


def test_di_registers_each_grimmory_audio_instance():
    from src.utils.di_container import Container

    assert {"GrimmoryAudio", "Grimmory2Audio"} <= set(Container.sync_clients.kwargs)
