"""Tests for SyncManager.queue_suggestion (socket-triggered suggestion discovery)."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_manager import SyncManager


class TestQueueSuggestion(unittest.TestCase):
    def setUp(self):
        self.mock_db = Mock()
        self.mock_db.get_books_by_status.return_value = []
        self.mock_db.get_all_books.return_value = []

        self.mock_abs = Mock()

        self.manager = SyncManager(
            database_service=self.mock_db,
            abs_client=self.mock_abs,
            sync_clients={},
            data_dir=Path('/tmp')
        )

        os.environ['SUGGESTIONS_ENABLED'] = 'true'

    def tearDown(self):
        os.environ.pop('SUGGESTIONS_ENABLED', None)

    def test_skips_when_disabled(self):
        os.environ['SUGGESTIONS_ENABLED'] = 'false'
        self.manager.queue_suggestion('book-123')
        self.mock_db.suggestion_exists.assert_not_called()

    def test_skips_mapped_book(self):
        mock_book = Mock()
        mock_book.abs_id = 'book-123'
        self.mock_db.get_all_books.return_value = [mock_book]

        self.manager.queue_suggestion('book-123')
        self.mock_db.suggestion_exists.assert_not_called()

    def test_skips_existing_suggestion(self):
        self.mock_db.suggestion_exists.return_value = True

        self.manager.queue_suggestion('book-456')
        self.mock_abs.get_item_details.assert_not_called()

    def test_creates_suggestion_for_new_book(self):
        self.mock_db.suggestion_exists.return_value = False
        self.mock_abs.get_item_details.return_value = {
            'media': {'metadata': {'title': 'Test Book', 'authorName': 'Author'}}
        }
        # No matches found, so suggestion creation won't save
        self.manager.queue_suggestion('book-789')
        self.mock_abs.get_item_details.assert_called_once_with('book-789')

    def test_thread_safety_prevents_duplicate(self):
        """Second concurrent call for same ID should be skipped."""
        self.mock_db.suggestion_exists.return_value = False

        # Simulate first call in-flight
        self.manager._suggestion_in_flight.add('book-dup')
        self.manager.queue_suggestion('book-dup')

        # Should not reach _create_suggestion since it's in-flight
        self.mock_abs.get_item_details.assert_not_called()

    def test_cleans_up_in_flight_on_error(self):
        self.mock_db.suggestion_exists.return_value = False
        self.mock_abs.get_item_details.side_effect = Exception("boom")

        self.manager.queue_suggestion('book-err')

        # Should clean up despite error
        self.assertNotIn('book-err', self.manager._suggestion_in_flight)


if __name__ == '__main__':
    unittest.main()
