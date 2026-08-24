"""Tests for ReadingService and ReadingStatsService — error paths and edge cases."""

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.reading_service import ReadingService
from src.services.reading_stats_service import ReadingStatsService

# ---------------------------------------------------------------------------
# ReadingStatsService.get_year_stats
# ---------------------------------------------------------------------------


def _make_mock_book(**overrides):
    book = Mock()
    book.id = overrides.get("id", 1)
    book.status = overrides.get("status", "active")
    book.finished_at = overrides.get("finished_at", None)
    book.rating = overrides.get("rating", None)
    book.title = overrides.get("title", "Test Book")
    return book


def _make_state(book_id, percentage=0.5):
    state = Mock()
    state.book_id = book_id
    state.percentage = percentage
    return state


class TestGetYearStatsNoStates:
    """get_year_stats when there are no states or no books."""

    def test_no_books_returns_zero_stats(self):
        db = Mock()
        db.get_all_books.return_value = []
        db.get_all_states.return_value = []
        db.get_reading_goal.return_value = None

        svc = ReadingStatsService(database_service=db)
        result = svc.get_year_stats(2026)

        assert result["books_finished"] == 0
        assert result["currently_reading"] == 0
        assert result["total_tracked"] == 0
        assert result["average_rating"] is None
        assert result["monthly_finished"] == [0] * 12
        assert result["goal_target"] is None
        assert result["goal_percent"] is None

    def test_active_books_with_no_states_not_counted_as_reading(self):
        """A book with 'active' status but zero progress is not 'genuinely reading'."""
        book = _make_mock_book(id=1, status="active")
        db = Mock()
        db.get_all_books.return_value = [book]
        db.get_all_states.return_value = []  # No states at all
        db.get_reading_goal.return_value = None

        svc = ReadingStatsService(database_service=db)
        result = svc.get_year_stats(2026)

        assert result["currently_reading"] == 0
        assert result["total_tracked"] == 1

    def test_active_book_with_low_progress_not_counted(self):
        """Progress <= 1% means not genuinely reading."""
        book = _make_mock_book(id=1, status="active")
        state = _make_state(1, percentage=0.005)  # 0.5%
        db = Mock()
        db.get_all_books.return_value = [book]
        db.get_all_states.return_value = [state]
        db.get_reading_goal.return_value = None

        svc = ReadingStatsService(database_service=db)
        result = svc.get_year_stats(2026)

        assert result["currently_reading"] == 0

    def test_completed_book_counted_for_correct_year(self):
        book = _make_mock_book(id=1, status="completed", finished_at="2026-06-15", rating=4.5)
        db = Mock()
        db.get_all_books.return_value = [book]
        db.get_all_states.return_value = []
        db.get_reading_goal.return_value = None

        svc = ReadingStatsService(database_service=db)
        result = svc.get_year_stats(2026)

        assert result["books_finished"] == 1
        assert result["monthly_finished"][5] == 1  # June = index 5
        assert result["average_rating"] == 4.5

    def test_completed_book_wrong_year_not_counted(self):
        book = _make_mock_book(id=1, status="completed", finished_at="2025-12-31")
        db = Mock()
        db.get_all_books.return_value = [book]
        db.get_all_states.return_value = []
        db.get_reading_goal.return_value = None

        svc = ReadingStatsService(database_service=db)
        result = svc.get_year_stats(2026)

        assert result["books_finished"] == 0


class TestGetYearStatsWhenComputationRaises:
    """Verify behavior when database calls raise exceptions."""

    def test_get_all_books_raises_propagates(self):
        """If the DB is down, get_year_stats should propagate the exception."""
        db = Mock()
        db.get_all_books.side_effect = RuntimeError("DB connection lost")

        svc = ReadingStatsService(database_service=db)

        with pytest.raises(RuntimeError, match="DB connection lost"):
            svc.get_year_stats(2026)

    def test_get_all_states_raises_propagates(self):
        db = Mock()
        db.get_all_books.return_value = [_make_mock_book()]
        db.get_all_states.side_effect = RuntimeError("states table locked")

        svc = ReadingStatsService(database_service=db)

        with pytest.raises(RuntimeError, match="states table locked"):
            svc.get_year_stats(2026)

    def test_get_reading_goal_raises_propagates(self):
        db = Mock()
        db.get_all_books.return_value = []
        db.get_all_states.return_value = []
        db.get_reading_goal.side_effect = RuntimeError("goal fetch failed")

        svc = ReadingStatsService(database_service=db)

        with pytest.raises(RuntimeError, match="goal fetch failed"):
            svc.get_year_stats(2026)


# ---------------------------------------------------------------------------
# ReadingService — error paths
# ---------------------------------------------------------------------------


class TestReadingServiceMaxProgress:
    def test_max_progress_empty_states(self):
        assert ReadingService.max_progress([]) == 0.0

    def test_max_progress_as_percent(self):
        states = [_make_state(1, 0.75), _make_state(1, 0.50)]
        assert ReadingService.max_progress(states, as_percent=True) == 75.0

    def test_max_progress_caps_at_100(self):
        states = [_make_state(1, 1.5)]  # overflows
        assert ReadingService.max_progress(states, as_percent=True) == 100.0


class TestReadingServiceSetProgress:
    def test_set_progress_book_not_found(self):
        db = Mock()
        db.get_book_by_id.return_value = None

        svc = ReadingService(db)
        result = svc.set_progress(999, 0.5, Mock())

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @patch("src.services.reading_service.StatusMachine")
    def test_set_progress_sync_propagation_failure_still_succeeds(self, _mock_sm):
        """If sync propagation to clients fails, set_progress still returns success."""
        book = _make_mock_book(id=1, status="active")
        book.abs_id = "abs-1"
        book.started_at = "2026-01-01"
        db = Mock()
        db.get_book_by_id.return_value = book

        container = Mock()
        failing_client = Mock()
        failing_client.is_configured.return_value = True
        failing_client.update_progress.side_effect = ConnectionError("unreachable")
        container.sync_clients.return_value = {"Storyteller": failing_client}

        svc = ReadingService(db)
        result = svc.set_progress(1, 0.5, container)

        assert result["success"] is True
        assert result["percentage"] == 0.5

    def test_set_progress_starts_book_through_status_machine(self):
        book = _make_mock_book(id=1, status="not_started")
        book.abs_id = "abs-1"
        db = Mock()
        db.get_book_by_id.return_value = book

        container = Mock()
        container.sync_clients.return_value = {}

        svc = ReadingService(db)
        svc.status_machine = Mock()
        svc.status_machine.transition.return_value = {
            "success": True,
            "status": "active",
            "previous_status": "not_started",
        }

        result = svc.set_progress(1, 0.5, container)

        assert result["success"] is True
        svc.status_machine.transition.assert_called_once_with(book, "active", "manual_progress", container=container)
        db.save_book.assert_not_called()


class TestReadingServiceUpdateStatus:
    @patch("src.services.reading_service.StatusMachine")
    def test_update_status_book_not_found(self, _mock_sm):
        db = Mock()
        db.get_book_by_id.return_value = None

        svc = ReadingService(db)
        result = svc.update_status(999, "active", Mock())

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestReadingServiceMarkComplete:
    def test_mark_complete_records_transition_through_status_machine(self):
        book = _make_mock_book(id=1, status="active")
        book.abs_id = "abs-1"
        book.ebook_filename = None
        db = Mock()
        db.get_book_by_ref.return_value = book

        container = Mock()
        container.sync_clients.return_value = {}

        svc = ReadingService(db)
        svc.status_machine = Mock()
        svc.status_machine.transition.return_value = {
            "success": True,
            "status": "completed",
            "previous_status": "active",
        }

        result = svc.mark_complete_with_sync(1, container)

        assert result["success"] is True
        svc.status_machine.transition.assert_called_once_with(book, "completed", "completion_sync", container=container)
        db.add_reading_journal.assert_not_called()
        db.update_book_reading_fields.assert_not_called()

    def test_mark_complete_keeps_external_state_sync(self):
        book = _make_mock_book(id=1, status="active")
        book.abs_id = "abs-1"
        book.ebook_filename = None
        db = Mock()
        db.get_book_by_ref.return_value = book

        client = Mock()
        client.is_configured.return_value = True
        container = Mock()
        container.sync_clients.return_value = {"Storyteller": client}

        svc = ReadingService(db)
        svc.status_machine = Mock()
        svc.status_machine.transition.return_value = {
            "success": True,
            "status": "completed",
            "previous_status": "active",
        }

        result = svc.mark_complete_with_sync(1, container)

        assert result["success"] is True
        client.update_progress.assert_called_once()
        db.save_state.assert_called_once()
