"""Tests for ReadingDateService — focused on error paths and edge cases."""

from unittest.mock import MagicMock, patch

import pytest

from src.services.reading_date_service import ReadingDateService, push_grimmory_read_status

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def mock_hc_client():
    return MagicMock()


@pytest.fixture
def mock_abs_client():
    return MagicMock()


@pytest.fixture
def service(mock_db, mock_hc_client, mock_abs_client):
    return ReadingDateService(mock_db, mock_hc_client, mock_abs_client)


def _make_book(**overrides):
    book = MagicMock()
    book.id = overrides.get("id", 1)
    book.abs_id = overrides.get("abs_id", "abs-123")
    book.title = overrides.get("title", "Test Book")
    book.status = overrides.get("status", "active")
    book.started_at = overrides.get("started_at", None)
    book.finished_at = overrides.get("finished_at", None)
    book.ebook_filename = overrides.get("ebook_filename", None)
    return book


# ===========================================================================
# pull_reading_dates
# ===========================================================================


class TestPullReadingDates:
    """Tests for pull_reading_dates (ABS date retrieval)."""

    def test_returns_empty_when_book_not_found(self, service, mock_db):
        mock_db.get_book_by_id.return_value = None
        assert service.pull_reading_dates(99) == {}

    def test_returns_empty_when_book_has_no_abs_id(self, service, mock_db):
        mock_db.get_book_by_id.return_value = _make_book(abs_id=None)
        assert service.pull_reading_dates(1) == {}

    def test_returns_empty_when_abs_not_configured(self, service, mock_db, mock_abs_client):
        mock_db.get_book_by_id.return_value = _make_book()
        mock_abs_client.is_configured.return_value = False
        assert service.pull_reading_dates(1) == {}

    def test_returns_empty_when_abs_progress_is_none(self, service, mock_db, mock_abs_client):
        mock_db.get_book_by_id.return_value = _make_book()
        mock_abs_client.is_configured.return_value = True
        mock_abs_client.get_progress.return_value = None
        assert service.pull_reading_dates(1) == {}

    def test_parses_started_and_finished_timestamps(self, service, mock_db, mock_abs_client):
        mock_db.get_book_by_id.return_value = _make_book()
        mock_abs_client.is_configured.return_value = True
        # 1750032000000 ms = 2025-06-16 00:00:00 UTC
        mock_abs_client.get_progress.return_value = {
            "startedAt": 1750032000000,
            "finishedAt": 1750118400000,  # +1 day
        }
        dates = service.pull_reading_dates(1)
        assert dates["started_at"] == "2025-06-16"
        assert dates["finished_at"] == "2025-06-17"

    def test_returns_only_started_when_finished_missing(self, service, mock_db, mock_abs_client):
        mock_db.get_book_by_id.return_value = _make_book()
        mock_abs_client.is_configured.return_value = True
        mock_abs_client.get_progress.return_value = {"startedAt": 1750032000000}
        dates = service.pull_reading_dates(1)
        assert "started_at" in dates
        assert "finished_at" not in dates

    def test_returns_empty_when_api_raises(self, service, mock_db, mock_abs_client):
        """Exception in ABS client should be caught and return empty dict."""
        mock_db.get_book_by_id.return_value = _make_book()
        mock_abs_client.is_configured.return_value = True
        mock_abs_client.get_progress.side_effect = ConnectionError("timeout")
        assert service.pull_reading_dates(1) == {}

    def test_returns_empty_when_db_raises(self, service, mock_db):
        """Exception in database lookup should be caught and return empty dict."""
        mock_db.get_book_by_id.side_effect = RuntimeError("DB gone")
        assert service.pull_reading_dates(1) == {}

    def test_zero_timestamp_not_included(self, service, mock_db, mock_abs_client):
        """A startedAt of 0 is falsy and should not produce a date."""
        mock_db.get_book_by_id.return_value = _make_book()
        mock_abs_client.is_configured.return_value = True
        mock_abs_client.get_progress.return_value = {"startedAt": 0, "finishedAt": 1750032000000}
        dates = service.pull_reading_dates(1)
        assert "started_at" not in dates
        assert "finished_at" in dates

    def test_kobo_fills_dates_abs_did_not_provide(self, mock_db, mock_hc_client, mock_abs_client):
        """Kobo device dates act as fallback when ABS has nothing."""
        mock_kobo = MagicMock()
        mock_kobo.reading_dates_for.return_value = {"started_at": "2025-07-20", "finished_at": "2025-07-27"}
        svc = ReadingDateService(mock_db, mock_hc_client, mock_abs_client, kobo_service=mock_kobo)

        mock_db.get_book_by_id.return_value = _make_book()
        mock_abs_client.is_configured.return_value = False

        dates = svc.pull_reading_dates(1)
        assert dates == {"started_at": "2025-07-20", "finished_at": "2025-07-27"}
        mock_kobo.reading_dates_for.assert_called_once_with(1)

    def test_abs_dates_win_over_kobo(self, mock_db, mock_hc_client, mock_abs_client):
        """ABS stays authoritative; Kobo only fills keys ABS didn't provide."""
        mock_kobo = MagicMock()
        mock_kobo.reading_dates_for.return_value = {"started_at": "2025-07-20", "finished_at": "2025-07-27"}
        svc = ReadingDateService(mock_db, mock_hc_client, mock_abs_client, kobo_service=mock_kobo)

        mock_db.get_book_by_id.return_value = _make_book()
        mock_abs_client.is_configured.return_value = True
        mock_abs_client.get_progress.return_value = {"startedAt": 1750032000000}  # 2025-06-16

        dates = svc.pull_reading_dates(1)
        assert dates["started_at"] == "2025-06-16"
        assert dates["finished_at"] == "2025-07-27"

    def test_kobo_failure_is_swallowed(self, mock_db, mock_hc_client, mock_abs_client):
        """A Kobo lookup error degrades to ABS-only dates, never an exception."""
        mock_kobo = MagicMock()
        mock_kobo.reading_dates_for.side_effect = RuntimeError("kobo gone")
        svc = ReadingDateService(mock_db, mock_hc_client, mock_abs_client, kobo_service=mock_kobo)

        mock_db.get_book_by_id.return_value = _make_book()
        mock_abs_client.is_configured.return_value = False

        assert svc.pull_reading_dates(1) == {}


# ===========================================================================
# push_dates_to_hardcover
# ===========================================================================


class TestPushDatesToHardcover:
    """Tests for push_dates_to_hardcover."""

    def test_not_configured(self, service, mock_hc_client):
        mock_hc_client.is_configured.return_value = False
        ok, msg = service.push_dates_to_hardcover(1)
        assert ok is False
        assert "not configured" in msg

    def test_no_hardcover_details(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        mock_db.get_hardcover_details.return_value = None
        ok, msg = service.push_dates_to_hardcover(1)
        assert ok is False
        assert "not linked" in msg

    def test_book_not_found(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = None
        ok, msg = service.push_dates_to_hardcover(1)
        assert ok is False
        assert "not found" in msg.lower()

    def test_no_local_dates(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book()
        ok, msg = service.push_dates_to_hardcover(1)
        assert ok is False
        assert "No local dates" in msg

    def test_creates_read_when_no_reads_exist(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        book = _make_book(started_at="2025-01-01")
        mock_db.get_book_by_id.return_value = book
        mock_hc_client.find_user_book.return_value = {
            "id": 10,
            "edition_id": 5,
            "user_book_reads": [],
        }
        mock_hc_client.create_read_with_dates.return_value = 99

        with patch("src.services.reading_date_service.log_hardcover_action"):
            ok, msg = service.push_dates_to_hardcover(1)

        assert ok is True
        assert "Created" in msg
        mock_hc_client.create_read_with_dates.assert_called_once()

    def test_create_read_failure(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book(started_at="2025-01-01")
        mock_hc_client.find_user_book.return_value = {
            "id": 10,
            "edition_id": 5,
            "user_book_reads": [],
        }
        mock_hc_client.create_read_with_dates.return_value = None
        ok, msg = service.push_dates_to_hardcover(1)
        assert ok is False
        assert "Failed to create" in msg

    def test_updates_existing_read_missing_dates(self, service, mock_db, mock_hc_client):
        """Default mode: only fills in missing HC dates."""
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book(started_at="2025-01-01", finished_at="2025-02-01")
        mock_hc_client.find_user_book.return_value = {
            "id": 10,
            "user_book_reads": [{"id": 7, "started_at": "2025-01-01", "finished_at": None}],
        }
        mock_hc_client.update_read_dates.return_value = True

        with patch("src.services.reading_date_service.log_hardcover_action"):
            ok, msg = service.push_dates_to_hardcover(1)

        assert ok is True
        # started_at should NOT be pushed (HC already has it); finished_at should be pushed
        call_kwargs = mock_hc_client.update_read_dates.call_args
        assert call_kwargs[1]["finished_at"] == "2025-02-01"

    def test_skips_when_dates_already_match(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book(started_at="2025-01-01")
        mock_hc_client.find_user_book.return_value = {
            "id": 10,
            "user_book_reads": [{"id": 7, "started_at": "2025-01-01", "finished_at": None}],
        }
        ok, msg = service.push_dates_to_hardcover(1)
        assert ok is False
        assert "already match" in msg

    def test_force_overwrites_existing_dates(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book(started_at="2025-03-01", finished_at="2025-04-01")
        mock_hc_client.find_user_book.return_value = {
            "id": 10,
            "user_book_reads": [{"id": 7, "started_at": "2025-01-01", "finished_at": "2025-02-01"}],
        }
        mock_hc_client.update_read_dates.return_value = True

        with patch("src.services.reading_date_service.log_hardcover_action"):
            ok, msg = service.push_dates_to_hardcover(1, force=True)

        assert ok is True
        call_kwargs = mock_hc_client.update_read_dates.call_args
        assert call_kwargs[1]["started_at"] == "2025-03-01"
        assert call_kwargs[1]["finished_at"] == "2025-04-01"

    def test_hardcover_rejects_update(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book(started_at="2025-01-01")
        mock_hc_client.find_user_book.return_value = {
            "id": 10,
            "user_book_reads": [{"id": 7, "started_at": None, "finished_at": None}],
        }
        mock_hc_client.update_read_dates.return_value = False
        ok, msg = service.push_dates_to_hardcover(1)
        assert ok is False
        assert "rejected" in msg

    def test_exception_returns_error_tuple(self, service, mock_hc_client):
        """Any unexpected exception should be caught and return a clean error."""
        mock_hc_client.is_configured.side_effect = RuntimeError("boom")
        ok, msg = service.push_dates_to_hardcover(1)
        assert ok is False
        assert "Unexpected error" in msg

    def test_user_book_not_found(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book(started_at="2025-01-01")
        mock_hc_client.find_user_book.return_value = None
        ok, msg = service.push_dates_to_hardcover(1)
        assert ok is False
        assert "not found in your Hardcover library" in msg


# ===========================================================================
# pull_dates_from_hardcover
# ===========================================================================


class TestPullDatesFromHardcover:
    """Tests for pull_dates_from_hardcover."""

    def test_not_configured(self, service, mock_hc_client):
        mock_hc_client.is_configured.return_value = False
        ok, msg, dates = service.pull_dates_from_hardcover(1)
        assert ok is False
        assert dates == {}

    def test_no_hc_reads(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book()
        mock_hc_client.find_user_book.return_value = {"user_book_reads": []}
        ok, msg, dates = service.pull_dates_from_hardcover(1)
        assert ok is False
        assert "No reading sessions" in msg

    def test_truncates_iso_timestamps_to_date(self, service, mock_db, mock_hc_client):
        """HC may return full ISO timestamps; service should truncate to YYYY-MM-DD."""
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        book = _make_book()
        mock_db.get_book_by_id.return_value = book
        mock_hc_client.find_user_book.return_value = {
            "user_book_reads": [{"started_at": "2025-03-15T14:30:00Z", "finished_at": "2025-04-20T09:00:00Z"}],
        }
        updated_book = _make_book(started_at="2025-03-15", finished_at="2025-04-20")
        mock_db.get_book_by_ref.return_value = updated_book

        with patch("src.services.reading_date_service.log_hardcover_action"):
            ok, msg, dates = service.pull_dates_from_hardcover(1)

        assert ok is True
        # Verify DB was called with truncated dates
        mock_db.update_book_reading_fields.assert_called_once_with(1, started_at="2025-03-15", finished_at="2025-04-20")

    def test_no_dates_on_hardcover(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book()
        mock_hc_client.find_user_book.return_value = {
            "user_book_reads": [{"started_at": None, "finished_at": None}],
        }
        ok, msg, dates = service.pull_dates_from_hardcover(1)
        assert ok is False
        assert "No dates found" in msg

    def test_local_dates_already_match(self, service, mock_db, mock_hc_client):
        mock_hc_client.is_configured.return_value = True
        hc_details = MagicMock()
        hc_details.hardcover_book_id = "42"
        mock_db.get_hardcover_details.return_value = hc_details
        mock_db.get_book_by_id.return_value = _make_book(started_at="2025-01-01")
        mock_hc_client.find_user_book.return_value = {
            "user_book_reads": [{"started_at": "2025-01-01", "finished_at": None}],
        }
        ok, msg, dates = service.pull_dates_from_hardcover(1)
        assert ok is False
        assert "already match" in msg

    def test_exception_returns_error_triple(self, service, mock_hc_client):
        mock_hc_client.is_configured.side_effect = RuntimeError("boom")
        ok, msg, dates = service.pull_dates_from_hardcover(1)
        assert ok is False
        assert "Unexpected error" in msg
        assert dates == {}


# ===========================================================================
# auto_complete_finished_books
# ===========================================================================


class TestAutoCompleteFinishedBooks:
    """Tests for auto_complete_finished_books."""

    @patch("src.services.status_machine.StatusMachine")
    def test_skips_non_active_books(self, MockMachine, service, mock_db):
        mock_db.get_all_books.return_value = [
            _make_book(status="completed"),
            _make_book(status="not_started"),
        ]
        container = MagicMock()
        stats = service.auto_complete_finished_books(container)
        assert stats == {"completed": 0, "errors": 0}

    @patch("src.services.status_machine.StatusMachine")
    def test_individual_book_failure_continues(self, MockMachine, service, mock_db, mock_abs_client):
        """One book raising should not stop processing of remaining books."""
        book1 = _make_book(id=1, status="active")
        book2 = _make_book(id=2, status="active")
        mock_db.get_all_books.return_value = [book1, book2]

        # Both books are "finished" by state
        state = MagicMock()
        state.percentage = 1.0
        mock_db.get_states_for_book.return_value = [state]

        # ABS not configured so pull_reading_dates is quick
        mock_abs_client.is_configured.return_value = False

        machine_inst = MockMachine.return_value
        # First book's transition raises, second succeeds
        machine_inst.transition.side_effect = [RuntimeError("oops"), {"success": True}]

        container = MagicMock()
        container.sync_clients.return_value = {}

        stats = service.auto_complete_finished_books(container)
        assert stats["errors"] == 1
        assert stats["completed"] == 1

    @patch("src.services.status_machine.StatusMachine")
    def test_below_threshold_not_completed(self, MockMachine, service, mock_db):
        book = _make_book(status="active")
        mock_db.get_all_books.return_value = [book]

        state = MagicMock()
        state.percentage = 0.5
        mock_db.get_states_for_book.return_value = [state]

        container = MagicMock()
        stats = service.auto_complete_finished_books(container)
        assert stats["completed"] == 0


    @patch("src.services.status_machine.StatusMachine")
    def test_pushes_dates_to_hardcover_after_completion(self, MockMachine, service, mock_db, mock_abs_client):
        """A successful auto-complete lands the finish date on Hardcover."""
        book = _make_book(id=1, status="active")
        mock_db.get_all_books.return_value = [book]

        state = MagicMock()
        state.percentage = 1.0
        mock_db.get_states_for_book.return_value = [state]
        mock_abs_client.is_configured.return_value = False

        MockMachine.return_value.transition.return_value = {"success": True}

        container = MagicMock()
        container.sync_clients.return_value = {}

        with patch.object(service, "push_dates_to_hardcover") as mock_push:
            stats = service.auto_complete_finished_books(container)

        assert stats["completed"] == 1
        mock_push.assert_called_once_with(book.id)

    @patch("src.services.status_machine.StatusMachine")
    def test_no_hardcover_push_when_transition_fails(self, MockMachine, service, mock_db, mock_abs_client):
        book = _make_book(id=1, status="active")
        mock_db.get_all_books.return_value = [book]

        state = MagicMock()
        state.percentage = 1.0
        mock_db.get_states_for_book.return_value = [state]
        mock_abs_client.is_configured.return_value = False

        MockMachine.return_value.transition.return_value = {"success": False, "error": "nope"}

        container = MagicMock()
        container.sync_clients.return_value = {}

        with patch.object(service, "push_dates_to_hardcover") as mock_push:
            service.auto_complete_finished_books(container)

        mock_push.assert_not_called()


# ===========================================================================
# _push_completion_to_clients
# ===========================================================================


class TestPushCompletionToClients:
    """Tests for _push_completion_to_clients (internal helper)."""

    def test_individual_client_failure_continues(self, service, mock_db):
        """Failure pushing to one client should not prevent pushing to others."""
        book = _make_book(ebook_filename=None)  # no Grimmory push
        container = MagicMock()

        client_a = MagicMock()
        client_a.is_configured.return_value = True
        client_a.update_progress.side_effect = ConnectionError("dead")

        client_b = MagicMock()
        client_b.is_configured.return_value = True
        client_b.update_progress.return_value = None

        container.sync_clients.return_value = {"storyteller": client_a, "bookfusion": client_b}

        service._push_completion_to_clients(book, container)

        # client_b should still have been called despite client_a failing
        client_b.update_progress.assert_called_once()
        # State saved only for the successful client
        assert mock_db.save_state.call_count == 1


# ===========================================================================
# push_grimmory_read_status (module-level helper)
# ===========================================================================


class TestPushGrimmoryReadStatus:
    def test_exception_is_swallowed(self):
        container = MagicMock()
        bl_client = MagicMock()
        container.grimmory_client.return_value = bl_client
        bl_client.is_configured.return_value = True
        bl_client.update_read_status.side_effect = RuntimeError("Grimmory down")

        book = _make_book(ebook_filename="book.epub")
        # Should not raise
        push_grimmory_read_status(book, container, "READ")

    def test_skips_when_not_configured(self):
        container = MagicMock()
        bl_client = MagicMock()
        container.grimmory_client.return_value = bl_client
        bl_client.is_configured.return_value = False

        book = _make_book(ebook_filename="book.epub")
        push_grimmory_read_status(book, container, "READ")
        bl_client.update_read_status.assert_not_called()
