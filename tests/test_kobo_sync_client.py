"""Tests for src/sync_clients/kobo_sync_client.py — read-only progress source."""

import unittest
from unittest.mock import MagicMock

from src.db.models import Book, State
from src.sync_clients.kobo_sync_client import KoboSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


def _kobo_book(content_id="cid-1", percent=47, read_status=1, hidden=False):
    kb = MagicMock()
    kb.content_id = content_id
    kb.percent = percent
    kb.read_status = read_status
    kb.hidden = hidden
    return kb


class TestKoboSyncClient(unittest.TestCase):
    def setUp(self):
        self.mock_kobo_service = MagicMock()
        self.mock_ebook_parser = MagicMock()
        self.client = KoboSyncClient(self.mock_kobo_service, self.mock_ebook_parser)
        self.book = Book(abs_id="test-book-id", ebook_filename="test.epub", title="Dune")
        self.book.id = 7

    def test_supported_sync_types_ebook_only(self):
        self.assertEqual(self.client.get_supported_sync_types(), {"ebook"})

    def test_is_configured_delegates(self):
        self.mock_kobo_service.is_configured.return_value = True
        self.assertTrue(self.client.is_configured())

    def test_get_service_state_from_bulk_context(self):
        state = self.client.get_service_state(self.book, None, bulk_context={7: [_kobo_book(percent=47)]})
        self.assertIsNotNone(state)
        self.assertAlmostEqual(state.current["pct"], 0.47)
        self.assertEqual(state.previous_pct, 0)

    def test_get_service_state_finished_is_full(self):
        state = self.client.get_service_state(self.book, None, bulk_context={7: [_kobo_book(percent=90, read_status=2)]})
        self.assertAlmostEqual(state.current["pct"], 1.0)

    def test_get_service_state_delta_vs_prev(self):
        prev = State(abs_id="test-book-id", book_id=7, client_name="kobo", percentage=0.40)
        state = self.client.get_service_state(self.book, prev, bulk_context={7: [_kobo_book(percent=47)]})
        self.assertAlmostEqual(state.delta, 0.07)

    def test_get_service_state_missing_book_returns_none(self):
        self.assertIsNone(self.client.get_service_state(self.book, None, bulk_context={}))
        self.mock_kobo_service.kobo_books_for.return_value = []
        self.assertIsNone(self.client.get_service_state(self.book, None))

    def test_get_text_from_current_state(self):
        state = self.client.get_service_state(self.book, None, bulk_context={7: [_kobo_book(percent=50)]})
        self.mock_ebook_parser.get_text_at_percentage.return_value = "some text"
        text = self.client.get_text_from_current_state(self.book, state)
        self.mock_ebook_parser.get_text_at_percentage.assert_called_with("test.epub", 0.5)
        self.assertEqual(text, "some text")

    def test_update_progress_is_read_only_noop(self):
        request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.75))
        result = self.client.update_progress(self.book, request)
        self.assertFalse(result.success)

    def test_fetch_bulk_state(self):
        self.mock_kobo_service.is_configured.return_value = True
        self.mock_kobo_service.kobo_books_by_library_id.return_value = {7: [_kobo_book()]}
        self.assertEqual(self.client.fetch_bulk_state(), {7: [self.mock_kobo_service.kobo_books_by_library_id()[7][0]]})

        self.mock_kobo_service.is_configured.return_value = False
        self.assertIsNone(self.client.fetch_bulk_state())


if __name__ == "__main__":
    unittest.main()
