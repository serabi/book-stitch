"""
Tests for book status transitions (pause, resume, DNF) and activity detection.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.models import Book, HardcoverDetails  # noqa: E402


class MockContainer:
    """Minimal mock container for testing book status endpoints."""

    def __init__(self):
        self.mock_sync_manager = Mock()
        self.mock_abs_client = Mock()
        self.mock_database_service = Mock()
        self.mock_database_service.get_all_settings.return_value = {}
        self._sync_clients = {}
        self._hardcover_client = Mock(is_configured=Mock(return_value=False))

    def sync_manager(self):
        return self.mock_sync_manager

    def abs_client(self):
        return self.mock_abs_client

    def booklore_client(self):
        return Mock(is_configured=Mock(return_value=False))

    def hardcover_client(self):
        return self._hardcover_client

    def storyteller_client(self):
        return Mock(is_configured=Mock(return_value=False))

    def ebook_parser(self):
        return Mock()

    def database_service(self):
        return self.mock_database_service

    def sync_clients(self):
        return self._sync_clients

    def data_dir(self):
        return Path(tempfile.gettempdir()) / 'test_data'

    def books_dir(self):
        return Path(tempfile.gettempdir()) / 'test_books'

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / 'test_epub_cache'


class TestBookStatusEndpoints(unittest.TestCase):
    """Test the pause, resume, and DNF API endpoints."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self._prev_env = {k: os.environ.get(k) for k in ('DATA_DIR', 'BOOKS_DIR')}
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir
        self.addCleanup(self._cleanup_env)

        self.mock_container = MockContainer()

        def mock_initialize_database(data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_initialize_database
        self.addCleanup(self._cleanup_monkeypatch)

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.db = self.mock_container.mock_database_service

    def _cleanup_env(self):
        import shutil
        for key, prev_val in self._prev_env.items():
            if prev_val is not None:
                os.environ[key] = prev_val
            else:
                os.environ.pop(key, None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _cleanup_monkeypatch(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db

    def _make_book(self, abs_id='book-1', status='active', activity_flag=False):
        book = Book(abs_id=abs_id, abs_title='Test Book', status=status)
        book.activity_flag = activity_flag
        return book

    # --- Pause ---

    def test_pause_active_book(self):
        book = self._make_book(status='active')
        self.db.get_book.return_value = book

        resp = self.client.post('/api/pause/book-1')
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(book.status, 'paused')
        self.db.save_book.assert_called_once_with(book)

    def test_pause_syncs_hardcover(self):
        """Pausing should set Hardcover status_id=4."""
        book = self._make_book(status='active')
        self.db.get_book.return_value = book

        hc_details = HardcoverDetails(abs_id='book-1', hardcover_book_id='123', hardcover_edition_id='456')
        self.db.get_hardcover_details.return_value = hc_details

        self.mock_container._hardcover_client = Mock(is_configured=Mock(return_value=True))

        resp = self.client.post('/api/pause/book-1')
        self.assertEqual(resp.status_code, 200)

        self.mock_container._hardcover_client.update_status.assert_called_once_with(123, 4, 456)

    def test_pause_rejects_non_active(self):
        for status in ['paused', 'dnf', 'pending', 'processing']:
            book = self._make_book(status=status)
            self.db.get_book.return_value = book

            resp = self.client.post('/api/pause/book-1')
            data = resp.get_json()

            self.assertEqual(resp.status_code, 400)
            self.assertFalse(data['success'])

    def test_pause_not_found(self):
        self.db.get_book.return_value = None
        resp = self.client.post('/api/pause/nonexistent')
        self.assertEqual(resp.status_code, 404)

    # --- Resume ---

    def test_resume_paused_book(self):
        book = self._make_book(status='paused', activity_flag=True)
        self.db.get_book.return_value = book

        resp = self.client.post('/api/resume/book-1')
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(book.status, 'active')
        self.assertFalse(book.activity_flag)

    def test_resume_dnf_book(self):
        book = self._make_book(status='dnf')
        self.db.get_book.return_value = book
        self.db.get_hardcover_details.return_value = None

        resp = self.client.post('/api/resume/book-1')
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(book.status, 'active')

    def test_resume_dnf_syncs_hardcover(self):
        """Resuming from DNF should set Hardcover status to Currently Reading (2)."""
        book = self._make_book(status='dnf')
        self.db.get_book.return_value = book

        hc_details = HardcoverDetails(abs_id='book-1', hardcover_book_id='123', hardcover_edition_id='456')
        self.db.get_hardcover_details.return_value = hc_details

        self.mock_container._hardcover_client = Mock(is_configured=Mock(return_value=True))

        resp = self.client.post('/api/resume/book-1')
        self.assertEqual(resp.status_code, 200)

        self.mock_container._hardcover_client.update_status.assert_called_once_with(123, 2, 456)

    def test_resume_paused_syncs_hardcover(self):
        """Resuming from paused should set Hardcover status to Currently Reading (2)."""
        book = self._make_book(status='paused')
        self.db.get_book.return_value = book

        hc_details = HardcoverDetails(abs_id='book-1', hardcover_book_id='123', hardcover_edition_id='456')
        self.db.get_hardcover_details.return_value = hc_details

        self.mock_container._hardcover_client = Mock(is_configured=Mock(return_value=True))

        resp = self.client.post('/api/resume/book-1')
        self.assertEqual(resp.status_code, 200)

        self.mock_container._hardcover_client.update_status.assert_called_once_with(123, 2, 456)

    def test_resume_rejects_active(self):
        book = self._make_book(status='active')
        self.db.get_book.return_value = book

        resp = self.client.post('/api/resume/book-1')
        self.assertEqual(resp.status_code, 400)

    def test_resume_rejects_pending(self):
        book = self._make_book(status='pending')
        self.db.get_book.return_value = book

        resp = self.client.post('/api/resume/book-1')
        self.assertEqual(resp.status_code, 400)

    # --- DNF ---

    def test_dnf_active_book(self):
        book = self._make_book(status='active')
        self.db.get_book.return_value = book
        self.db.get_hardcover_details.return_value = None

        resp = self.client.post('/api/dnf/book-1')
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(book.status, 'dnf')

    def test_dnf_paused_book(self):
        book = self._make_book(status='paused')
        self.db.get_book.return_value = book
        self.db.get_hardcover_details.return_value = None

        resp = self.client.post('/api/dnf/book-1')
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(book.status, 'dnf')

    def test_dnf_syncs_hardcover(self):
        """DNF should set Hardcover status_id=5."""
        book = self._make_book(status='active')
        self.db.get_book.return_value = book

        hc_details = HardcoverDetails(abs_id='book-1', hardcover_book_id='123', hardcover_edition_id='456')
        self.db.get_hardcover_details.return_value = hc_details

        self.mock_container._hardcover_client = Mock(is_configured=Mock(return_value=True))

        resp = self.client.post('/api/dnf/book-1')
        self.assertEqual(resp.status_code, 200)

        self.mock_container._hardcover_client.update_status.assert_called_once_with(123, 5, 456)

    def test_dnf_rejects_pending(self):
        book = self._make_book(status='pending')
        self.db.get_book.return_value = book

        resp = self.client.post('/api/dnf/book-1')
        self.assertEqual(resp.status_code, 400)


class TestActivityDetectionSocket(unittest.TestCase):
    """Test activity detection in ABS socket listener."""

    def setUp(self):
        self.mock_db = MagicMock()

        with patch("src.services.abs_socket_listener.socketio.Client"):
            from src.services.abs_socket_listener import ABSSocketListener
            self.listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378",
                abs_api_token="test-token",
                database_service=self.mock_db,
                sync_manager=MagicMock(),
            )

    def _make_book(self, abs_id, status='active', activity_flag=False):
        book = MagicMock()
        book.abs_id = abs_id
        book.abs_title = 'Test Book'
        book.status = status
        book.activity_flag = activity_flag
        return book

    def test_paused_book_gets_activity_flag(self):
        """Progress on a paused book should set activity_flag."""
        book = self._make_book('book-1', status='paused', activity_flag=False)
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "p1", "data": {"libraryItemId": "book-1"}})

        self.assertTrue(book.activity_flag)
        self.mock_db.save_book.assert_called_once_with(book)
        # Should NOT record in pending (no sync trigger)
        self.assertEqual(len(self.listener._pending), 0)

    def test_dnf_book_gets_activity_flag(self):
        """Progress on a DNF book should set activity_flag."""
        book = self._make_book('book-1', status='dnf', activity_flag=False)
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "p1", "data": {"libraryItemId": "book-1"}})

        self.assertTrue(book.activity_flag)
        self.mock_db.save_book.assert_called_once_with(book)

    def test_already_flagged_book_not_saved_again(self):
        """If activity_flag is already True, don't save again."""
        book = self._make_book('book-1', status='paused', activity_flag=True)
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "p1", "data": {"libraryItemId": "book-1"}})

        self.mock_db.save_book.assert_not_called()
        # Also shouldn't record in pending since it's not active
        self.assertEqual(len(self.listener._pending), 0)

    def test_active_book_still_triggers_sync(self):
        """Active books should be recorded in pending, no activity flag behavior."""
        book = self._make_book('book-1', status='active')
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "p1", "data": {"libraryItemId": "book-1"}})

        self.assertIn('book-1', self.listener._pending)
        self.mock_db.save_book.assert_not_called()


class TestActivityDetectionKoSync(unittest.TestCase):
    """Test activity detection in KOSync server via the real PUT endpoint."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self._prev_env = {k: os.environ.get(k) for k in ('DATA_DIR', 'BOOKS_DIR', 'KOSYNC_USER', 'KOSYNC_KEY')}
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir
        os.environ['KOSYNC_USER'] = 'testuser'
        os.environ['KOSYNC_KEY'] = 'testpass'
        self.addCleanup(self._cleanup_env)

        self.mock_container = MockContainer()

        def mock_initialize_database(data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_initialize_database
        self.addCleanup(self._cleanup_monkeypatch)

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.db = self.mock_container.mock_database_service

    def _cleanup_env(self):
        import shutil
        for key, prev_val in self._prev_env.items():
            if prev_val is not None:
                os.environ[key] = prev_val
            else:
                os.environ.pop(key, None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _cleanup_monkeypatch(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db

    def test_paused_book_flagged_on_kosync_put(self):
        """KOSync PUT for paused book should set activity_flag."""
        book = Book(abs_id='book-1', abs_title='Test Book', status='paused')
        book.activity_flag = False

        kosync_doc = MagicMock()
        kosync_doc.linked_abs_id = 'book-1'
        kosync_doc.percentage = 0.5

        self.db.get_kosync_document.return_value = kosync_doc
        self.db.get_book.return_value = book
        self.db.save_book.return_value = book

        from src.utils.kosync_headers import hash_kosync_key
        auth_key = hash_kosync_key('testpass')

        resp = self.client.put('/syncs/progress',
            json={
                'document': 'test-doc-hash',
                'progress': '/body/text/p',
                'percentage': '0.75',
                'device': 'test-device',
                'device_id': 'test-device-id',
            },
            headers={
                'x-auth-user': 'testuser',
                'x-auth-key': auth_key,
            })

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(book.activity_flag)
        self.db.save_book.assert_called_with(book)


if __name__ == '__main__':
    unittest.main()
