"""Behavior-freeze: KOSync protocol byte/status freeze for the ``/koreader/*`` forms.

``tests/test_kosync_server.py`` already locks down the bare-path KOSync protocol
(auth, login, create, progress GET/PUT, the 502-not-404 missing-progress quirk,
rate limiting). What it does *not* assert is that the duplicate ``/koreader/...``
prefixed routes KOReader devices use behave identically. This file freezes that
prefix parity plus the healthcheck contract, so cleanup cannot drop or diverge
one of the two registered forms.

Setup mirrors ``test_kosync_server.py`` (real ``DatabaseService`` against a temp
SQLite DB, ``KosyncService`` wired into ``app.config``) to keep behavior real
rather than mocked at the protocol layer.
"""

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

TEST_DIR = tempfile.mkdtemp(prefix="kosync_protocol_freeze_")
os.environ["DATA_DIR"] = TEST_DIR
os.environ["KOSYNC_USER"] = "testuser"
os.environ["KOSYNC_KEY"] = "testpass"

from src.db.database_service import DatabaseService
from src.db.models import KosyncDocument


class _KosyncMockContainer:
    """Lightweight container that avoids the epubcfi (Docker-only) import chain."""

    def __init__(self):
        self.mock_sync_manager = Mock()
        self.mock_abs_client = Mock()
        self.mock_grimmory_client = Mock()
        self.mock_database_service = Mock()
        self.mock_database_service.get_all_settings.return_value = {}
        self.mock_sync_manager.abs_client = self.mock_abs_client
        self.mock_sync_manager.grimmory_client = self.mock_grimmory_client
        self.mock_sync_manager.get_title.return_value = "Test Book"
        self.mock_sync_manager.get_duration.return_value = 3600

    def sync_manager(self):
        return self.mock_sync_manager

    def abs_client(self):
        return self.mock_abs_client

    def grimmory_client(self):
        return self.mock_grimmory_client

    def grimmory_client_group(self):
        return self.mock_grimmory_client

    def ebook_parser(self):
        return Mock()

    def database_service(self):
        return self.mock_database_service

    def sync_clients(self):
        return {}

    def data_dir(self):
        return Path(TEST_DIR)

    def books_dir(self):
        return Path(TEST_DIR) / "books"

    def epub_cache_dir(self):
        return Path(TEST_DIR) / "epub_cache"


class TestKosyncKoreaderPrefixParity(unittest.TestCase):
    """The ``/koreader/...`` forms must match the bare forms byte-for-byte."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.join(TEST_DIR, "test.db")
        from src import web_server

        cls.database_service = DatabaseService(cls.db_path)
        cls.mock_container = _KosyncMockContainer()
        # create_app bootstraps settings into os.environ; snapshot/restore so
        # feature flags do not leak into later tests.
        _saved_env = os.environ.copy()
        try:
            cls.app, _ = web_server.create_app(test_container=cls.mock_container)
        finally:
            os.environ.clear()
            os.environ.update(_saved_env)
        cls.app.config["database_service"] = cls.database_service
        from src.services.kosync_service import KosyncService

        cls.app.config["kosync_service"] = KosyncService(
            cls.database_service,
            cls.mock_container,
            cls.mock_container.mock_sync_manager,
        )
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_DIR, ignore_errors=True)

    def setUp(self):
        self.auth_headers = {
            "x-auth-user": "testuser",
            "x-auth-key": hashlib.md5(b"testpass").hexdigest(),
            "Content-Type": "application/json",
        }
        with self.database_service.get_session() as session:
            session.query(KosyncDocument).delete()
        with self.app.app_context():
            rate_limiter = self.app.config.get("rate_limiter")
            if rate_limiter:
                rate_limiter.clear()

    # ---------------- Healthcheck ----------------

    def test_healthcheck_both_forms(self):
        """Both healthcheck forms return 200 / ``OK``."""
        for path in ("/healthcheck", "/koreader/healthcheck"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.get_data(as_text=True), "OK", path)

    # ---------------- Auth ----------------

    def test_auth_both_forms_accept_valid(self):
        for path in ("/users/auth", "/koreader/users/auth"):
            response = self.client.get(path, headers=self.auth_headers)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.get_json(), {"username": "testuser"}, path)

    def test_auth_both_forms_reject_missing_credentials(self):
        for path in ("/users/auth", "/koreader/users/auth"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertIn("message", response.get_json(), path)

    # ---------------- Create / Login stubs ----------------

    def test_create_both_forms(self):
        for path in ("/users/create", "/koreader/users/create"):
            response = self.client.post(path, json={})
            self.assertEqual(response.status_code, 201, path)
            data = response.get_json()
            self.assertEqual(data["id"], 1, path)
            self.assertEqual(data["username"], "testuser", path)

    def test_login_both_forms(self):
        for path in ("/users/login", "/koreader/users/login"):
            response = self.client.post(path, json={})
            self.assertEqual(response.status_code, 200, path)
            data = response.get_json()
            self.assertEqual(data["id"], 1, path)
            self.assertTrue(data["active"], path)
            # Must not leak secrets.
            self.assertNotIn("token", data, path)
            self.assertNotIn("key", data, path)

    # ---------------- Progress GET: the 502-not-404 quirk on both forms ----------------

    def test_get_progress_missing_returns_502_both_forms(self):
        """Missing progress returns 502 (not 404) with ``{message}`` on both forms.

        This is the spec-required KOReader quirk; freezing it on both the bare
        and prefixed routes prevents a cleanup from "normalizing" it to 404.
        """
        for prefix in ("", "/koreader"):
            path = f"{prefix}/syncs/progress/" + "z" * 32
            response = self.client.get(path, headers=self.auth_headers)
            self.assertEqual(response.status_code, 502, path)
            data = response.get_json()
            self.assertIn("message", data, path)
            self.assertNotIn("404", str(response.status_code), path)

    def test_put_then_get_progress_both_forms(self):
        """A PUT under the prefixed form is retrievable under the bare form."""
        doc = "p" * 32
        put = self.client.put(
            "/koreader/syncs/progress",
            headers=self.auth_headers,
            json={
                "document": doc,
                "progress": "/body/chapter[1]",
                "percentage": 0.5,
                "device": "Kobo",
                "device_id": "K1",
            },
        )
        self.assertEqual(put.status_code, 200)
        self.assertEqual(put.get_json()["document"], doc)

        get = self.client.get("/syncs/progress/" + doc, headers=self.auth_headers)
        self.assertEqual(get.status_code, 200)
        self.assertAlmostEqual(get.get_json()["percentage"], 0.5)


if __name__ == "__main__":
    unittest.main()
