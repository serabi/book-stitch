import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

class MockContainer:
    """Mock container for testing."""
    def __init__(self):
        self.mock_sync_manager = Mock()
        self.mock_abs_client = Mock()
        self.mock_booklore_client = Mock()
        self.mock_storyteller_client = Mock()
        self.mock_bookfusion_client = Mock()
        self.mock_database_service = Mock()
        # Ensure get_all_settings returns a dict
        self.mock_database_service.get_all_settings.return_value = {}
        # Ensure list return values for iteration
        self.mock_database_service.get_books_by_status.return_value = []
        self.mock_database_service.get_all_books.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_all_actionable_suggestions.return_value = []
        self.mock_database_service.get_bookfusion_books.return_value = []
        self.mock_bookfusion_client.is_configured.return_value = False

        self.mock_ebook_parser = Mock()
        self.mock_sync_clients = Mock()
        self.mock_hardcover_client = Mock()

        # Link up the manager
        self.mock_sync_manager.abs_client = self.mock_abs_client
        self.mock_sync_manager.get_abs_title.return_value = 'Test Book Title'
        self.mock_sync_manager.get_duration.return_value = 3600

    def sync_manager(self): return self.mock_sync_manager
    def abs_client(self): return self.mock_abs_client
    def booklore_client(self): return self.mock_booklore_client
    def storyteller_client(self): return self.mock_storyteller_client
    def bookfusion_client(self): return self.mock_bookfusion_client
    def ebook_parser(self): return self.mock_ebook_parser
    def database_service(self): return self.mock_database_service
    def hardcover_client(self): return self.mock_hardcover_client
    def sync_clients(self): return self.mock_sync_clients
    def data_dir(self): return Path(tempfile.gettempdir()) / 'test_data'
    def books_dir(self): return Path(tempfile.gettempdir()) / 'test_books'
    def epub_cache_dir(self): return Path(tempfile.gettempdir()) / 'test_epub_cache'

class TestSuggestionsFeature(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        project_root = Path(__file__).parent.parent
        os.environ['TEMPLATE_DIR'] = str(project_root / 'templates')
        os.environ['STATIC_DIR'] = str(project_root / 'static')

        self.mock_container = MockContainer()

        # Mock database initialization
        def mock_init_db(data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_init_db

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def tearDown(self):
        """
        Restore the original database initializer, remove the temporary test directory, and clear the SUGGESTIONS_ENABLED environment variable.

        Reverts any patch applied to src.db.migration_utils.initialize_database, recursively deletes the temporary directory created for the test, and removes SUGGESTIONS_ENABLED from the process environment if present.
        """
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        # Reset env var
        for key in ('SUGGESTIONS_ENABLED', 'TEMPLATE_DIR', 'STATIC_DIR'):
            if key in os.environ:
                del os.environ[key]

    @patch('src.web_server.apply_settings')
    def test_settings_save_toggle(self, mock_restart):
        """Test that saving settings updates the env var and DB."""
        # Initial state: default is true (implied) or unset

        # 1. Turn OFF
        self.mock_container.mock_database_service.get_all_settings.return_value = {}
        response = self.client.post('/settings', data={
             # No SUGGESTIONS_ENABLED sent means checkbox unchecked = False
             'SYNC_PERIOD_MINS': '5'
        })
        self.assertEqual(response.status_code, 302)

        # Verify DB set (should be 'false' because it's missing from form)
        self.mock_container.mock_database_service.set_setting.assert_any_call('SUGGESTIONS_ENABLED', 'false')
        self.assertEqual(os.environ.get('SUGGESTIONS_ENABLED'), 'false')

        # 2. Turn ON
        response = self.client.post('/settings', data={
            'SUGGESTIONS_ENABLED': 'on',
            'SYNC_PERIOD_MINS': '5'
        })
        self.mock_container.mock_database_service.set_setting.assert_any_call('SUGGESTIONS_ENABLED', 'true')
        self.assertEqual(os.environ.get('SUGGESTIONS_ENABLED'), 'true')

    def test_sync_manager_respects_setting(self):
        """Test that check_for_suggestions returns early if disabled."""
        from src.sync_manager import SyncManager

        # Initialize SyncManager with mocks
        manager = SyncManager(
            database_service=self.mock_container.mock_database_service,
            sync_clients={},
            data_dir=Path(self.temp_dir)
        )

        # Case 1: Disabled
        os.environ['SUGGESTIONS_ENABLED'] = 'false'
        manager.check_for_suggestions({}, [])
        # Should NOT call get_all_books (optimization check is inside the try block after the return)
        self.mock_container.mock_database_service.get_all_books.assert_not_called()

        # Case 2: Enabled
        os.environ['SUGGESTIONS_ENABLED'] = 'true'
        # Mock get_all_books to avoid crash further down
        self.mock_container.mock_database_service.get_all_books.return_value = []
        manager.check_for_suggestions({}, [])
        # Should proceed to call DB
        self.mock_container.mock_database_service.get_all_books.assert_called()

    def test_match_resolves_suggestions(self):
        """Test that /match endpoint resolves suggestions."""
        # Mock dependencies for match
        import src.blueprints.matching_bp
        original_get_kosync = src.blueprints.matching_bp.get_kosync_id_for_ebook
        src.blueprints.matching_bp.get_kosync_id_for_ebook = Mock(return_value='test-kosync-id')

        try:
            self.mock_container.mock_abs_client.get_all_audiobooks.return_value = [
                {'id': 'abc-123', 'media': {'metadata': {'title': 'T'}, 'duration': 100}}
            ]
            self.mock_container.mock_sync_clients.items.return_value = {}.items()

            # Perform Match
            self.mock_container.mock_database_service.get_book.return_value = None
            self.client.post('/match', data={
                'audiobook_id': 'abc-123',
                'ebook_filename': 'test.epub'
            })

            # Verify resolution was called for both abs_id AND kosync_doc_id
            self.mock_container.mock_database_service.resolve_suggestion.assert_any_call('abc-123')
            self.mock_container.mock_database_service.resolve_suggestion.assert_any_call('test-kosync-id')

        finally:
            src.blueprints.matching_bp.get_kosync_id_for_ebook = original_get_kosync

    def test_suggestions_page_renders_hidden_section(self):
        """Test that the Suggestions page includes the hidden section UI."""
        self.mock_container.mock_database_service.get_bookfusion_books.return_value = []
        self.mock_container.mock_database_service.get_all_actionable_suggestions.return_value = [
            SimpleNamespace(
                id=1,
                source_id='visible-1',
                title='Visible Book',
                author='Visible Author',
                cover_url=None,
                matches=[{'title': 'Candidate', 'author': 'Author', 'source_family': 'booklore', 'confidence': 'high', 'score': 0.9, 'evidence': []}],
                status='pending',
                created_at=None,
            ),
            SimpleNamespace(
                id=2,
                source_id='hidden-1',
                title='Hidden Book',
                author='Hidden Author',
                cover_url=None,
                matches=[{'title': 'Candidate', 'author': 'Author', 'source_family': 'booklore', 'confidence': 'high', 'score': 0.9, 'evidence': []}],
                status='hidden',
                created_at=None,
            ),
        ]

        response = self.client.get('/suggestions')

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('Hidden', page)
        self.assertIn('hidden suggestions', page)

    def test_hide_and_unhide_suggestion_api(self):
        """Test hide/unhide suggestion API endpoints."""
        self.mock_container.mock_database_service.hide_suggestion.return_value = True
        self.mock_container.mock_database_service.unhide_suggestion.return_value = True

        hide_response = self.client.post('/api/suggestions/test-source/hide')
        unhide_response = self.client.post('/api/suggestions/test-source/unhide')

        self.assertEqual(hide_response.status_code, 200)
        self.assertEqual(unhide_response.status_code, 200)
        self.mock_container.mock_database_service.hide_suggestion.assert_called_once_with('test-source')
        self.mock_container.mock_database_service.unhide_suggestion.assert_called_once_with('test-source')

if __name__ == '__main__':
    unittest.main()
