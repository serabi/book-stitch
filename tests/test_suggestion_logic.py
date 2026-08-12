import pytest

pytestmark = pytest.mark.docker

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_manager import SyncManager


class TestSuggestionLogic(unittest.TestCase):
    def setUp(self):
        self.mock_db = Mock()
        self.mock_db.get_books_by_status.return_value = []
        self.mock_db.get_all_books.return_value = []

        self.mock_abs = Mock()
        self.mock_abs.get_all_audiobooks.return_value = []
        self.mock_grimmory = Mock()

        self.manager = SyncManager(
            database_service=self.mock_db,
            abs_client=self.mock_abs,
            grimmory_client=self.mock_grimmory,
            sync_clients={},
            data_dir=Path("/tmp"),
        )

        # Default environment to enabled
        os.environ["SUGGESTIONS_ENABLED"] = "true"

    def tearDown(self):
        if "SUGGESTIONS_ENABLED" in os.environ:
            del os.environ["SUGGESTIONS_ENABLED"]

    def test_suggestion_ignored_when_progress_high(self):
        """Test that detections are NOT created if progress is above the 95% window."""
        # Setup
        abs_id = "book-123"
        progress_data = {
            "duration": 1000,
            "currentTime": 970,  # 97% — above the 95% detection window
        }

        # Mocks
        self.mock_db.get_all_books.return_value = []  # Not mapped
        self.mock_db.get_pending_suggestion.return_value = None  # No pending suggestion
        self.mock_db.suggestion_exists.return_value = False  # No hidden suggestion (simulating clean state)
        self.mock_db.get_detected_book.return_value = None

        # Action
        self.manager.check_for_suggestions({abs_id: progress_data}, [])

        # Assert: 97% is above the window, so no detection is persisted
        self.mock_db.save_detected_book.assert_not_called()

    def test_suggestion_created_when_progress_within_window(self):
        """Test that detections ARE created when progress is within the 1-95% window."""
        # Setup
        abs_id = "book-456"
        progress_data = {
            "duration": 1000,
            "currentTime": 500,  # 50%
        }

        # Mocks
        self.mock_db.get_all_books.return_value = []
        self.mock_db.get_pending_suggestion.return_value = None
        self.mock_db.suggestion_exists.return_value = False
        self.mock_db.get_detected_book.return_value = None

        # Prepare successful suggestion creation mocks
        self.mock_abs.get_item_details.return_value = {
            "media": {"metadata": {"title": "Test Book", "authorName": "Author"}}
        }
        self.mock_grimmory.is_configured.return_value = True
        self.mock_grimmory.search_books.return_value = [{"title": "Test Book", "fileName": "test.epub"}]

        # Action
        self.manager.check_for_suggestions({abs_id: progress_data}, [])

        # Assert: ABS detection persists a DetectedBook (not a legacy PendingSuggestion)
        self.mock_db.save_detected_book.assert_called_once()

    def test_promotes_not_started_ebook_with_progress(self):
        """A not_started book with a mapped ebook and >1% Grimmory progress is promoted."""
        book = MagicMock()
        book.id = 7
        book.title = "Mapped Ebook"
        book.status = "not_started"
        book.ebook_filename = "mapped.epub"
        book.started_at = None

        self.mock_db.get_books_by_status.side_effect = lambda status: [book] if status == "not_started" else []
        self.mock_grimmory.is_configured.return_value = True
        self.mock_grimmory.get_progress.return_value = (0.25, None)

        self.manager._promote_discovered_ebooks()

        self.assertEqual(book.status, "active")

    def test_does_not_promote_not_started_below_threshold(self):
        """A not_started ebook below 1% Grimmory progress stays not_started."""
        book = MagicMock()
        book.id = 8
        book.title = "Untouched Ebook"
        book.status = "not_started"
        book.ebook_filename = "untouched.epub"

        self.mock_db.get_books_by_status.side_effect = lambda status: [book] if status == "not_started" else []
        self.mock_grimmory.is_configured.return_value = True
        self.mock_grimmory.get_progress.return_value = (0.0, None)

        self.manager._promote_discovered_ebooks()

        self.assertEqual(book.status, "not_started")

    def test_promotion_forwards_container_to_transition(self):
        """Discovery promotion must pass the DI container so completion_sync side effects
        (real started_at pull, Grimmory push) can run instead of stamping date.today()."""
        from unittest.mock import patch

        book = MagicMock()
        book.id = 9
        book.title = "Container Forwarded"
        book.status = "not_started"
        book.ebook_filename = "forwarded.epub"

        sentinel_container = object()
        self.manager.container = sentinel_container
        self.mock_db.get_books_by_status.side_effect = lambda status: [book] if status == "not_started" else []
        self.mock_grimmory.is_configured.return_value = True
        self.mock_grimmory.get_progress.return_value = (0.30, None)

        with patch("src.sync_manager.StatusMachine.transition", return_value={"success": True}) as mock_transition:
            self.manager._promote_discovered_ebooks()

        mock_transition.assert_called_once()
        self.assertIs(mock_transition.call_args.kwargs.get("container"), sentinel_container)

    def test_pending_clear_books_not_promoted(self):
        """A not_started book awaiting a deferred clear must not be re-promoted from stale
        Grimmory progress, which would undo the clear."""
        book = MagicMock()
        book.id = 10
        book.title = "Pending Clear"
        book.status = "not_started"
        book.ebook_filename = "pending.epub"

        self.mock_db.get_books_by_status.side_effect = lambda status: [book] if status == "not_started" else []
        self.mock_grimmory.is_configured.return_value = True
        self.mock_grimmory.get_progress.return_value = (0.30, None)

        with self.manager._pending_clears_lock:
            self.manager._pending_clears.add(book.id)

        self.manager._promote_discovered_ebooks()

        self.assertEqual(book.status, "not_started")
        self.mock_grimmory.get_progress.assert_not_called()

    def test_suggestion_ignored_when_hidden(self):
        """Test that suggestions are NOT created if they were previously hidden."""
        # Setup
        abs_id = "book-789"
        progress_data = {
            "duration": 1000,
            "currentTime": 500,  # 50%
        }

        # Mocks
        self.mock_db.get_all_books.return_value = []
        self.mock_db.get_pending_suggestion.return_value = None  # No *active* pending suggestion
        self.mock_db.suggestion_exists.return_value = True  # BUT it exists (likely hidden)

        # Action
        self.manager.check_for_suggestions({abs_id: progress_data}, [])

        # Assert
        self.mock_db.save_pending_suggestion.assert_not_called()


if __name__ == "__main__":
    unittest.main()
