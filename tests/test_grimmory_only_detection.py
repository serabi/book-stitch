"""Tests for surfacing Grimmory-only in-progress books as DetectedBooks.

A book read only on Grimmory (no ABS audiobook, no other ebook source) used to
produce no match and therefore no DetectedBook. It should now be surfaced for
manual promotion even when matches == [].
"""

import pytest

pytestmark = pytest.mark.docker

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.suggestion_service import SuggestionService


def _make_service(grimmory_client, database_service):
    return SuggestionService(
        database_service=database_service,
        abs_client=Mock(),
        grimmory_client=grimmory_client,
        storyteller_client=None,
        library_service=Mock(),
        books_dir=Path("/tmp"),
        ebook_parser=Mock(),
    )


class TestGrimmoryOnlyDetection(unittest.TestCase):
    def setUp(self):
        self.mock_db = Mock()
        self.mock_db.get_all_books.return_value = []
        self.mock_db.get_all_actionable_suggestions.return_value = []
        self.mock_db.get_unlinked_kosync_documents.return_value = []
        self.mock_db.suggestion_exists.return_value = False

        self.grimmory = Mock()
        self.grimmory.is_configured.return_value = True
        # One in-progress Grimmory book with no cross-source counterpart.
        self.grimmory.get_all_books.return_value = [
            {"id": 1, "title": "Solo Read", "fileName": "solo.epub", "authors": "Some Author"},
        ]
        self.grimmory.get_progress.return_value = (0.40, None)

        self.service = _make_service(self.grimmory, self.mock_db)

    def test_grimmory_only_book_is_upserted_without_match(self):
        self.service._check_cross_ebook_suggestions()

        self.mock_db.save_detected_book.assert_called_once()
        detected = self.mock_db.save_detected_book.call_args.args[0]
        self.assertEqual(detected.source, "grimmory")
        self.assertEqual(detected.source_id, "solo.epub")
        self.assertEqual(detected.title, "Solo Read")
        # No cross-source match — matches_json should be NULL so a later enrichment
        # pass is free to attach matches without being clobbered first.
        self.assertIsNone(detected.matches_json)

    def test_grimmory_book_outside_window_is_dropped(self):
        self.grimmory.get_progress.return_value = (0.97, None)  # above 95% window

        self.service._check_cross_ebook_suggestions()

        self.mock_db.save_detected_book.assert_not_called()

    def test_grimmory_book_within_widened_window_is_surfaced(self):
        self.grimmory.get_progress.return_value = (0.80, None)  # within new 1-95% window

        self.service._check_cross_ebook_suggestions()

        self.mock_db.save_detected_book.assert_called_once()


if __name__ == "__main__":
    unittest.main()
