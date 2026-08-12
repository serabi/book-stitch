"""
Tests for ABSSocketListener debounce logic and KoSync PUT instant sync trigger.
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

from src.services.abs_socket_listener import ABSSocketListener


class TestABSSocketListenerDebounce(unittest.TestCase):
    """Test the debounce logic in ABSSocketListener."""

    def setUp(self):
        """Create listener with mocked dependencies."""
        self.mock_db = MagicMock()
        self.mock_sync = MagicMock()

        with patch("src.services.abs_socket_listener.socketio.Client"):
            self.listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378",
                abs_api_token="test-token",
                database_service=self.mock_db,
                sync_manager=self.mock_sync,
            )
        # Override debounce window to 1s for fast tests
        self.listener._debounce_window = 1

    def _make_active_book(self, abs_id: str, title: str = "Test Book", book_id: int = 1):
        book = MagicMock()
        book.id = book_id
        book.abs_id = abs_id
        book.title = title
        book.status = "active"
        return book

    def test_ignores_non_active_books(self):
        """Events for books not in DB or not active should be ignored."""
        # Book not in DB
        self.mock_db.get_book_by_abs_id.return_value = None
        self.listener._handle_progress_event({"id": "prog-1", "data": {"libraryItemId": "unknown-id"}})

        self.assertEqual(len(self.listener._pending), 0)

        # Book exists but not active
        inactive = self._make_active_book("inactive-id")
        inactive.status = "pending"
        self.mock_db.get_book_by_abs_id.return_value = inactive
        self.listener._handle_progress_event({"id": "prog-2", "data": {"libraryItemId": "inactive-id"}})

        self.assertEqual(len(self.listener._pending), 0)

    def test_records_active_book_event(self):
        """Events for active books should be recorded in pending dict."""
        book = self._make_active_book("book-1")
        self.mock_db.get_book_by_abs_id.return_value = book

        self.listener._handle_progress_event({"id": "prog-3", "data": {"libraryItemId": "book-1"}})

        self.assertIn("book-1", self.listener._pending)

    def test_debounce_does_not_fire_before_window(self):
        """Sync should NOT fire if debounce window hasn't elapsed."""
        book = self._make_active_book("book-2")
        self.mock_db.get_book_by_abs_id.return_value = book

        self.listener._handle_progress_event({"id": "prog-4", "data": {"libraryItemId": "book-2"}})
        self.listener._check_and_fire()

        self.mock_sync.sync_cycle.assert_not_called()

    def test_debounce_fires_after_window(self):
        """Sync SHOULD fire after debounce window elapses."""
        book = self._make_active_book("book-3", "Debounce Test", book_id=3)
        self.mock_db.get_book_by_abs_id.return_value = book

        self.listener._handle_progress_event({"id": "prog-5", "data": {"libraryItemId": "book-3"}})

        # Simulate time passing
        self.listener._pending["book-3"] = time.time() - 2  # 2s ago, window is 1s
        self.listener._check_and_fire()

        # Give the daemon thread a moment
        time.sleep(0.1)
        self.mock_sync.sync_cycle.assert_called_once_with(target_book_id=3)

    def test_no_double_fire(self):
        """Same event should not trigger sync twice."""
        book = self._make_active_book("book-4")
        self.mock_db.get_book_by_abs_id.return_value = book

        self.listener._pending["book-4"] = time.time() - 2
        self.listener._check_and_fire()
        time.sleep(0.1)

        # First fire should have removed from pending
        self.assertEqual(len(self.listener._pending), 0)

        # Calling again should do nothing
        self.listener._check_and_fire()
        time.sleep(0.1)
        self.mock_sync.sync_cycle.assert_called_once()

    def test_new_event_after_fire_retriggers(self):
        """A new event after sync fired should start a fresh debounce."""
        book = self._make_active_book("book-5")
        self.mock_db.get_book_by_abs_id.return_value = book

        # First event + fire
        self.listener._pending["book-5"] = time.time() - 2
        self.listener._check_and_fire()
        time.sleep(0.1)
        self.assertEqual(self.mock_sync.sync_cycle.call_count, 1)

        # New event
        self.listener._handle_progress_event({"id": "prog-6", "data": {"libraryItemId": "book-5"}})
        self.assertIn("book-5", self.listener._pending)

        # Fire again after window
        self.listener._pending["book-5"] = time.time() - 2
        self.listener._check_and_fire()
        time.sleep(0.1)
        self.assertEqual(self.mock_sync.sync_cycle.call_count, 2)

    def test_handles_nested_data_format(self):
        """Should handle the real ABS event format: {id, sessionId, data: {libraryItemId}}."""
        book = self._make_active_book("nested-id")
        self.mock_db.get_book_by_abs_id.return_value = book

        self.listener._handle_progress_event(
            {
                "id": "34621755-32df-4876-b235-abc123",
                "sessionId": "session-1",
                "deviceDescription": "Windows 10 / Firefox",
                "data": {"libraryItemId": "nested-id", "progress": 0.42},
            }
        )
        self.assertIn("nested-id", self.listener._pending)

    def test_handles_top_level_library_item_id(self):
        """Should handle older ABS format with top-level libraryItemId."""
        book = self._make_active_book("top-level-id")
        self.mock_db.get_book_by_abs_id.return_value = book

        self.listener._handle_progress_event({"libraryItemId": "top-level-id"})
        self.assertIn("top-level-id", self.listener._pending)

    def test_handles_missing_library_item_id(self):
        """Should silently ignore events with no libraryItemId."""
        self.listener._handle_progress_event({"someOtherField": "value"})
        self.assertEqual(len(self.listener._pending), 0)
        self.mock_db.get_book_by_abs_id.assert_not_called()

    def _make_not_started_book(self, abs_id: str):
        book = MagicMock()
        book.id = 99
        book.abs_id = abs_id
        book.title = "Fresh Book"
        book.status = "not_started"
        book.activity_flag = False
        book.started_at = None
        book.ebook_filename = None
        return book

    def test_promotes_not_started_on_meaningful_progress(self):
        """A not_started book with >1% reported progress is promoted to active."""
        book = self._make_not_started_book("fresh-id")
        self.mock_db.get_book_by_abs_id.return_value = book

        self.listener._handle_progress_event({"id": "p", "data": {"libraryItemId": "fresh-id", "progress": 0.10}})

        self.assertEqual(book.status, "active")
        self.assertIn("fresh-id", self.listener._pending)

    def test_promotion_forwards_container_to_transition(self):
        """The listener must forward its DI container into the status transition so
        completion_sync side effects (real started_at, Grimmory push) actually run."""
        sentinel_container = object()
        with patch("src.services.abs_socket_listener.socketio.Client"):
            listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378",
                abs_api_token="tok",
                database_service=self.mock_db,
                sync_manager=self.mock_sync,
                container=sentinel_container,
            )

        book = self._make_not_started_book("container-id")
        self.mock_db.get_book_by_abs_id.return_value = book

        with patch.object(
            listener._status_machine, "transition", return_value={"success": True}
        ) as mock_transition:
            listener._handle_progress_event({"id": "p", "data": {"libraryItemId": "container-id", "progress": 0.10}})

        mock_transition.assert_called_once()
        self.assertIs(mock_transition.call_args.kwargs.get("container"), sentinel_container)

    def test_not_started_below_threshold_only_flags_activity(self):
        """A not_started book below 1% is not promoted, just flagged."""
        book = self._make_not_started_book("fresh-id-2")
        self.mock_db.get_book_by_abs_id.return_value = book

        self.listener._handle_progress_event({"id": "p", "data": {"libraryItemId": "fresh-id-2", "progress": 0.001}})

        self.assertEqual(book.status, "not_started")
        self.assertTrue(book.activity_flag)
        self.assertNotIn("fresh-id-2", self.listener._pending)

    def test_not_started_without_progress_only_flags_activity(self):
        """A not_started book with no extractable progress is flagged, not promoted."""
        book = self._make_not_started_book("fresh-id-3")
        self.mock_db.get_book_by_abs_id.return_value = book

        self.listener._handle_progress_event({"id": "p", "data": {"libraryItemId": "fresh-id-3"}})

        self.assertEqual(book.status, "not_started")
        self.assertTrue(book.activity_flag)

    def test_url_stripping(self):
        """Server URL should strip trailing /api for socket connection."""
        with patch("src.services.abs_socket_listener.socketio.Client"):
            listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378/api",
                abs_api_token="tok",
                database_service=MagicMock(),
                sync_manager=MagicMock(),
            )
        self.assertEqual(listener._server_url, "http://abs.local:13378")


class TestKosyncPutInstantSync(unittest.TestCase):
    """Test that KoSync PUT records debounce events for active linked books.

    These tests call KosyncService.handle_put_progress directly with a mock
    debounce_manager, verifying that events are correctly recorded (or not).
    """

    def setUp(self):
        import os

        os.environ.setdefault("DATA_DIR", "/tmp/test_kosync_instant")
        os.environ["INSTANT_SYNC_ENABLED"] = "true"

    def tearDown(self):
        import os

        os.environ.pop("INSTANT_SYNC_ENABLED", None)

    def test_put_records_debounce_event_for_active_linked_book(self):
        """PUT for a linked active book should record a debounce event."""
        from src.services.kosync_service import KosyncService

        mock_db = MagicMock()
        mock_manager = MagicMock()
        svc = KosyncService(mock_db, MagicMock(), mock_manager)

        mock_book = MagicMock()
        mock_book.id = 42
        mock_book.abs_id = "test-instant-sync"
        mock_book.title = "Instant Sync Book"
        mock_book.status = "active"
        mock_book.activity_flag = False

        mock_doc = MagicMock()
        mock_doc.linked_abs_id = "test-instant-sync"
        mock_doc.linked_book_id = None
        mock_doc.percentage = 0.3
        mock_doc.device_id = "D1"

        mock_db.get_kosync_document.return_value = mock_doc
        mock_db.get_book_by_abs_id.return_value = mock_book

        mock_debounce = MagicMock()

        with patch.dict("os.environ", {"KOSYNC_FURTHEST_WINS": "false", "INSTANT_SYNC_ENABLED": "true"}):
            svc.handle_put_progress(
                {
                    "document": "x" * 32,
                    "percentage": 0.55,
                    "progress": "/body/test",
                    "device": "TestDevice",
                    "device_id": "D1",
                },
                "127.0.0.1",
                debounce_manager=mock_debounce,
            )

        mock_debounce.record_event.assert_called_once_with(42, "Instant Sync Book")

    def test_instant_sync_disabled_skips_debounce(self):
        """PUT should NOT record a debounce event when INSTANT_SYNC_ENABLED=false."""
        from src.services.kosync_service import KosyncService

        mock_db = MagicMock()
        mock_manager = MagicMock()
        svc = KosyncService(mock_db, MagicMock(), mock_manager)

        mock_book = MagicMock()
        mock_book.abs_id = "test-disabled"
        mock_book.title = "Disabled Book"
        mock_book.status = "active"
        mock_book.activity_flag = False

        mock_doc = MagicMock()
        mock_doc.linked_abs_id = "test-disabled"
        mock_doc.linked_book_id = None
        mock_doc.percentage = 0.1
        mock_doc.device_id = "D1"

        mock_db.get_kosync_document.return_value = mock_doc
        mock_db.get_book_by_abs_id.return_value = mock_book

        mock_debounce = MagicMock()

        with patch.dict("os.environ", {"KOSYNC_FURTHEST_WINS": "false", "INSTANT_SYNC_ENABLED": "false"}):
            svc.handle_put_progress(
                {"document": "d" * 32, "percentage": 0.55, "device": "TestDevice", "device_id": "D1"},
                "127.0.0.1",
                debounce_manager=mock_debounce,
            )

        mock_debounce.record_event.assert_not_called()

    def test_put_does_not_record_debounce_event_for_inactive_book(self):
        """PUT for a linked but inactive book should NOT record a debounce event."""
        from src.services.kosync_service import KosyncService

        mock_db = MagicMock()
        mock_manager = MagicMock()
        svc = KosyncService(mock_db, MagicMock(), mock_manager)

        mock_book = MagicMock()
        mock_book.abs_id = "test-inactive"
        mock_book.title = "Inactive Book"
        mock_book.status = "pending"
        mock_book.activity_flag = False

        mock_doc = MagicMock()
        mock_doc.linked_abs_id = "test-inactive"
        mock_doc.linked_book_id = None
        mock_doc.percentage = 0.1
        mock_doc.device_id = "D1"

        mock_db.get_kosync_document.return_value = mock_doc
        mock_db.get_book_by_abs_id.return_value = mock_book

        mock_debounce = MagicMock()

        with patch.dict("os.environ", {"KOSYNC_FURTHEST_WINS": "false", "INSTANT_SYNC_ENABLED": "true"}):
            svc.handle_put_progress(
                {"document": "y" * 32, "percentage": 0.55, "device": "TestDevice", "device_id": "D1"},
                "127.0.0.1",
                debounce_manager=mock_debounce,
            )

        mock_debounce.record_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
