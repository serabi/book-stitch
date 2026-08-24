import os
from unittest.mock import MagicMock, patch

import pytest

from src.api.libby_client import LibbyClient
from src.db.models import Book, State
from src.sync_clients.libby_sync_client import LibbySyncClient


@pytest.fixture
def mock_libby():
    return MagicMock(spec=LibbyClient)


@pytest.fixture
def client(mock_libby):
    c = LibbySyncClient(mock_libby, ebook_parser=MagicMock())
    return c


def make_loan(psn="55881092-12503377", fmt="audiobook"):
    card_id, title_id = psn.split("-")
    return {
        "psn_key": psn,
        "card_id": card_id,
        "title_id": title_id,
        "title": "A Book",
        "authors": "Author",
        "format": fmt,
        "isbn": None,
        "expires": None,
        "library_key": "libkey",
        "media_type": fmt,
    }


class TestBulkFetch:
    def test_fetch_builds_positions_from_legacy_store(self, client, mock_libby):
        mock_libby.is_configured.return_value = True
        mock_libby.poll_mins = 60
        mock_libby.get_active_loans.return_value = [make_loan()]
        mock_libby.get_legacy_position.return_value = {
            "position": {"percentageOfBook": 0.7703, "spinePosition": 12},
            "statistics": {"readingTime": 43491},
        }

        state = client.fetch_bulk_state()

        assert state == {"55881092-12503377": {"pct": 0.7703, "reading_time": 43491}}
        mock_libby.get_legacy_position.assert_called_once()
        # Possession not needed when legacy answered
        mock_libby.get_possession.assert_not_called()

    def test_fetch_falls_back_to_possession(self, client, mock_libby):
        mock_libby.is_configured.return_value = True
        mock_libby.poll_mins = 60
        loan = make_loan("55881092-10334152", fmt="book")
        mock_libby.get_active_loans.return_value = [loan]
        mock_libby.get_legacy_position.return_value = None
        passport = {
            "urls": {
                "web": "https://dewey-x.read.libbyapp.com/",
                "possession": "https://dewey-x.read.libbyapp.com/_d/possession",
            },
            "message": "m=blob",
            "expires": 9999999999,
            "leeway": 3600,
        }
        mock_libby.can_read_positions = True
        mock_libby.get_passport.return_value = passport
        mock_libby.get_possession.return_value = {
            "position": {"percentageOfBook": 0.0348},
            "statistics": {"readingTime": 200},
        }

        state = client.fetch_bulk_state()

        assert state["55881092-10334152"] == {"pct": 0.0348, "reading_time": 200}
        mock_libby.get_passport.assert_called_once()

    def test_null_positions_skipped(self, client, mock_libby):
        mock_libby.is_configured.return_value = True
        mock_libby.poll_mins = 60
        mock_libby.get_active_loans.return_value = [make_loan()]
        mock_libby.get_legacy_position.return_value = {"position": None, "statistics": {"readingTime": 0}}
        mock_libby.can_read_positions = False

        state = client.fetch_bulk_state()
        assert state == {}

    def test_throttle_serves_cache_between_polls(self, client, mock_libby):
        mock_libby.is_configured.return_value = True
        mock_libby.poll_mins = 60
        mock_libby.get_active_loans.return_value = [make_loan()]
        mock_libby.get_legacy_position.return_value = {
            "position": {"percentageOfBook": 0.5},
            "statistics": {},
        }

        first = client.fetch_bulk_state()
        second = client.fetch_bulk_state()

        assert first == second
        assert mock_libby.get_active_loans.call_count == 1

    def test_sync_failure_counts_toward_disable(self, client, mock_libby):
        mock_libby.is_configured.return_value = True
        mock_libby.poll_mins = 60
        mock_libby.get_active_loans.return_value = None  # sync failed

        for _ in range(5):
            client.fetch_bulk_state()

        assert client.auto_disabled is True
        assert client.is_configured() is False

    def test_auto_disable_recovers_after_cooldown(self, client, mock_libby):
        import time

        mock_libby.poll_mins = 60
        mock_libby.get_active_loans.return_value = None
        mock_libby.is_configured.return_value = True
        client._disabled_until = time.time() - 1  # cooldown expired
        assert client.is_configured() is True


class TestServiceState:
    def _state(self, pct):
        s = MagicMock(spec=State)
        s.percentage = pct
        return s

    def test_state_from_bulk_context(self, client):
        book = Book(title="Lost Metal", libby_psn_key="65497484-8209392")
        bulk = {"65497484-8209392": {"pct": 0.7703, "reading_time": 43491}}

        service_state = client.get_service_state(book, self._state(0.70), bulk_context=bulk)

        assert service_state is not None
        assert service_state.current["pct"] == 0.7703
        assert service_state.delta == pytest.approx(0.0703)

    def test_no_psn_key_returns_none(self, client):
        book = Book(title="Unlinked")
        assert client.get_service_state(book, None, bulk_context={"x": {}}) is None

    def test_book_absent_from_bulk_returns_none(self, client):
        book = Book(title="Linked", libby_psn_key="a-b")
        assert client.get_service_state(book, None, bulk_context={"other-c": {}}) is None

    def test_targeted_fetch_without_bulk_context(self, client, mock_libby):
        mock_libby.get_active_loans.return_value = [make_loan("65497484-8209392")]
        mock_libby.get_legacy_position.return_value = {
            "position": {"percentageOfBook": 0.7706},
            "statistics": {"readingTime": 43491},
        }
        book = Book(title="Lost Metal", libby_psn_key="65497484-8209392")

        service_state = client.get_service_state(book, None)

        assert service_state is not None
        assert service_state.current["pct"] == 0.7706


class TestReadOnly:
    def test_update_progress_always_fails(self, client):
        from src.sync_clients.sync_client_interface import LocatorResult, SyncResult, UpdateProgressRequest

        book = Book(title="X")
        req = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5))
        result = client.update_progress(book, req)
        assert isinstance(result, SyncResult)
        assert result.success is False

    def test_leader_and_modes(self, client):
        assert client.can_be_leader() is True
        assert client.get_supported_sync_types() == {"audiobook", "ebook"}
