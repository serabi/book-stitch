import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.modules.setdefault('nh3', SimpleNamespace(clean=lambda value, tags=None, attributes=None: value))

class MockContainer:
    """Mock container for testing."""
    def __init__(self):
        self.mock_database_service = Mock()
        self.mock_database_service.get_all_settings.return_value = {}
        self.mock_sync_manager = Mock()
        self.mock_sync_manager.get_abs_title.return_value = 'Test'

    def database_service(self): return self.mock_database_service
    def sync_manager(self): return self.mock_sync_manager
    def abs_client(self): return Mock()
    def booklore_client(self): return Mock()
    def storyteller_client(self): return Mock()
    def hardcover_client(self): return Mock()
    def transcriber(self): return Mock()
    def ebook_parser(self): return Mock()
    def sync_clients(self): return {}
    def data_dir(self): return Path(tempfile.gettempdir())
    def books_dir(self): return Path(tempfile.gettempdir())
    def epub_cache_dir(self): return Path(tempfile.gettempdir()) / 'test_epub_cache'

class TestSettingsComprehensive(unittest.TestCase):
    def setUp(self):
        """
        Prepare the test environment: create a temporary DATA_DIR, install a mock dependency container and mock database initializer, create the Flask test application and client, and define the list of boolean setting keys used by tests.

        The following observable effects are performed:
        - A temporary directory is created and assigned to the DATA_DIR environment variable.
        - A MockContainer instance is created and made available as self.mock_container.
        - src.db.migration_utils.initialize_database is replaced with a mock initializer that returns the mock database service.
        - The application is created via src.web_server.create_app using the mock container; the Flask app is stored in self.app and a test client in self.client.
        - self.bool_keys is populated with all boolean setting keys that tests will toggle.
        """
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir

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

        # List of all boolean keys from settings_bp.py
        self.bool_keys = [
            'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
            'SYNC_ABS_EBOOK',
            'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT',
            'KOSYNC_ENABLED',
            'STORYTELLER_ENABLED',
            'BOOKLORE_ENABLED',
            'CWA_ENABLED',
            'HARDCOVER_ENABLED',
            'TELEGRAM_ENABLED',
            'SUGGESTIONS_ENABLED',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
            'INSTANT_SYNC_ENABLED',
            'ABS_SOCKET_ENABLED',
            'BOOKFUSION_ENABLED',
        ]

    def tearDown(self):
        """
        Restore the original database initializer, remove the test temporary directory, and clear boolean-related environment variables.

        This method restores src.db.migration_utils.initialize_database to its original value saved in setUp, deletes the temporary directory created for the test, and removes any environment variables named in self.bool_keys if they exist.
        """
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        # Clear env vars
        for key in self.bool_keys:
            if key in os.environ:
                del os.environ[key]
        for key in ['STORYTELLER_PASSWORD']:
            if key in os.environ:
                del os.environ[key]

    @patch('src.web_server.apply_settings')
    def test_all_bool_toggles(self, mock_restart):
        """Verify EVERY boolean setting can be toggled ON and OFF."""

        # 1. Turn EVERYTHING ON
        # Construct form data with all keys present (simulating checked checkboxes)
        data_on = {key: 'on' for key in self.bool_keys}
        # Add a required non-bool field so validation passes if any
        data_on['SYNC_PERIOD_MINS'] = '5'

        self.client.post('/settings', data=data_on)

        # Verify calls to set_setting with 'true'
        for key in self.bool_keys:
            self.mock_container.mock_database_service.set_setting.assert_any_call(key, 'true')
            self.assertEqual(os.environ.get(key), 'true', f"{key} should be 'true' in env")

        # Reset mock calls for clean check
        self.mock_container.mock_database_service.reset_mock()

        # 2. Turn EVERYTHING OFF
        # Construct form data with NONE of the keys (simulating unchecked checkboxes)
        data_off = {
            'SYNC_PERIOD_MINS': '5'
        }

        self.client.post('/settings', data=data_off)

        # Verify calls to set_setting with 'false'
        for key in self.bool_keys:
            self.mock_container.mock_database_service.set_setting.assert_any_call(key, 'false')
            self.assertEqual(os.environ.get(key), 'false', f"{key} should be 'false' in env")

    @patch('src.web_server.apply_settings')
    def test_text_fields_save(self, mock_restart):
        """
        Ensure text-based settings are saved to the database when submitted.

        Posts TZ, SYNC_PERIOD_MINS, and ABS_SERVER to the /settings endpoint and asserts
        that each key/value pair is persisted via the container's database service.
        """
        test_data = {
            'TZ': 'Europe/Paris',
            'SYNC_PERIOD_MINS': '15',
            'ABS_SERVER': 'http://test.com'
        }

        self.client.post('/settings', data=test_data)

        for key, val in test_data.items():
            self.mock_container.mock_database_service.set_setting.assert_any_call(key, val)

    @patch('src.blueprints.settings_bp.http_requests.get')
    def test_abs_connection_test_uses_unsaved_payload(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'username': 'payload-user'}
        mock_get.return_value = mock_response

        response = self.client.post('/api/test-connection/abs', json={
            'server': 'abs.local:13378',
            'token': 'payload-token',
        })

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], 'http://abs.local:13378/api/me')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer payload-token')

    @patch('src.blueprints.settings_bp.http_requests.post')
    def test_storyteller_connection_test_falls_back_to_saved_secret(self, mock_post):
        os.environ['STORYTELLER_PASSWORD'] = 'saved-secret'
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        response = self.client.post('/api/test-connection/storyteller', json={
            'api_url': 'story.local:8000',
            'user': 'sarah',
            'password': '',
        })

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], 'http://story.local:8000/api/token')
        self.assertEqual(kwargs['data']['password'], 'saved-secret')

if __name__ == '__main__':
    unittest.main()
