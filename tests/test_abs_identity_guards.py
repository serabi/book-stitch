"""Grimmory-only books have no ABS identity; ABS clients must not call the ABS API for them."""

import unittest
from unittest.mock import MagicMock, patch

from src.api.api_clients import ABSClient
from src.db.models import Book
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.abs_sync_client import ABSSyncClient


class TestABSIdentityGuards(unittest.TestCase):
    def test_audio_client_skips_service_state_without_abs_id(self):
        abs_client = MagicMock()
        client = ABSSyncClient(abs_client, MagicMock(), MagicMock())
        book = Book(abs_id=None, title="Grimmory-only audio")

        self.assertIsNone(client.get_service_state(book, None))
        abs_client.get_progress.assert_not_called()

    def test_ebook_client_skips_service_state_without_abs_identity(self):
        abs_client = MagicMock()
        client = ABSEbookSyncClient(abs_client, MagicMock())
        book = Book(abs_id=None, abs_ebook_item_id=None, ebook_item_id=None, ebook_filename="t.epub")

        self.assertIsNone(client.get_service_state(book, None))
        abs_client.get_progress.assert_not_called()

    def test_get_progress_never_requests_a_none_item_id(self):
        client = ABSClient()
        client.session = MagicMock()

        with patch.object(ABSClient, "is_configured", return_value=True):
            self.assertIsNone(client.get_progress(None))

        client.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
