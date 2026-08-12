"""
Tests for KosyncService business logic.

Tests handle_put_progress, handle_get_progress, resolve_best_progress,
serialize_progress, resolve_book_by_sibling_hash, and register_hash_for_book.
All tests use mocked database_service and container — no Flask app needed.
"""

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.services.kosync_service import KosyncService


def _make_service(db=None, container=None, manager=None):
    db = db or MagicMock()
    container = container or MagicMock()
    return KosyncService(db, container, manager)


def _make_doc(
    doc_hash="a" * 32,
    percentage=0.5,
    device="TestDevice",
    device_id="DEV1",
    linked_abs_id=None,
    linked_book_id=None,
    filename=None,
    timestamp=None,
):
    doc = MagicMock()
    doc.document_hash = doc_hash
    doc.percentage = percentage
    doc.device = device
    doc.device_id = device_id
    doc.linked_abs_id = linked_abs_id
    doc.linked_book_id = linked_book_id
    doc.filename = filename
    doc.timestamp = timestamp or datetime(2025, 1, 1, 12, 0, 0)
    doc.progress = "/body/p[1]"
    return doc


def _make_book(
    book_id=1,
    abs_id="book-1",
    title="Test Book",
    status="active",
    kosync_doc_id=None,
    ebook_filename=None,
    activity_flag=False,
):
    book = MagicMock()
    book.id = book_id
    book.abs_id = abs_id
    book.title = title
    book.status = status
    book.kosync_doc_id = kosync_doc_id
    book.ebook_filename = ebook_filename
    book.activity_flag = activity_flag
    return book


class TestSerializeProgress:
    def test_full_data(self):
        doc = _make_doc()
        result = KosyncService.serialize_progress(doc)
        assert result["device"] == "TestDevice"
        assert result["percentage"] == 0.5
        assert result["document"] == "a" * 32

    def test_defaults_for_missing_fields(self):
        doc = _make_doc(device=None, device_id=None, percentage=None)
        result = KosyncService.serialize_progress(doc, device_default="fallback")
        assert result["device"] == "fallback"
        assert result["device_id"] == "fallback"
        assert result["percentage"] == 0


class TestResolveBookBySiblingHash:
    def test_filename_match_finds_linked_book(self):
        db = MagicMock()
        sibling = _make_doc(doc_hash="b" * 32, linked_abs_id="book-1", filename="test.epub")
        db.get_kosync_document.return_value = _make_doc(filename="test.epub")
        db.get_kosync_doc_by_filename.return_value = sibling
        book = _make_book()
        db.get_book_by_abs_id.return_value = book

        svc = _make_service(db=db)
        result = svc.resolve_book_by_sibling_hash("c" * 32)
        assert result == book

    def test_no_match_returns_none(self):
        db = MagicMock()
        db.get_kosync_document.return_value = _make_doc(filename=None)
        svc = _make_service(db=db)
        result = svc.resolve_book_by_sibling_hash("d" * 32)
        assert result is None


class TestRegisterHashForBook:
    def test_new_hash_creates_and_links(self):
        db = MagicMock()
        db.get_kosync_document.return_value = None
        svc = _make_service(db=db)
        book = _make_book()
        svc.register_hash_for_book("e" * 32, book)
        db.save_kosync_document.assert_called_once()

    def test_existing_unlinked_gets_linked(self):
        db = MagicMock()
        existing = _make_doc(linked_book_id=None)
        db.get_kosync_document.return_value = existing
        svc = _make_service(db=db)
        book = _make_book()
        svc.register_hash_for_book("f" * 32, book)
        db.link_kosync_document.assert_called_once()


class TestHandlePutProgress:
    def test_creates_new_document(self):
        db = MagicMock()
        db.get_kosync_document.return_value = None
        db.get_book_by_kosync_id.return_value = None
        svc = _make_service(db=db)

        result, status = svc.handle_put_progress(
            {"document": "a" * 32, "percentage": 0.5, "progress": "/body/p[1]", "device": "Kobo"}, "127.0.0.1"
        )
        assert status == 200
        assert result["document"] == "a" * 32
        db.save_kosync_document.assert_called_once()

    def test_updates_existing_document(self):
        db = MagicMock()
        existing = _make_doc(percentage=0.3)
        db.get_kosync_document.return_value = existing
        db.get_book_by_abs_id.return_value = None
        svc = _make_service(db=db)

        with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "false"}):
            result, status = svc.handle_put_progress(
                {"document": "a" * 32, "percentage": 0.7, "device": "Kobo", "device_id": "DEV2"}, "127.0.0.1"
            )
        assert status == 200
        db.save_kosync_document.assert_called()

    def test_furthest_wins_rejects_backward(self):
        db = MagicMock()
        existing = _make_doc(percentage=0.8, device_id="DEV-A")
        db.get_kosync_document.return_value = existing
        svc = _make_service(db=db)

        with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "true"}):
            result, status = svc.handle_put_progress(
                {"document": "a" * 32, "percentage": 0.2, "device": "Other", "device_id": "DEV-B"}, "127.0.0.1"
            )
        assert status == 200
        assert result["document"] == "a" * 32
        # Should NOT have saved (rejected)
        db.save_kosync_document.assert_not_called()

    def test_furthest_wins_allows_same_device(self):
        db = MagicMock()
        existing = _make_doc(percentage=0.8, device_id="DEV-A")
        db.get_kosync_document.return_value = existing
        db.get_book_by_abs_id.return_value = None
        svc = _make_service(db=db)

        with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "true"}):
            result, status = svc.handle_put_progress(
                {"document": "a" * 32, "percentage": 0.2, "device": "Kobo", "device_id": "DEV-A"}, "127.0.0.1"
            )
        assert status == 200
        db.save_kosync_document.assert_called()

    def test_furthest_wins_allows_force(self):
        db = MagicMock()
        existing = _make_doc(percentage=0.8, device_id="DEV-A")
        db.get_kosync_document.return_value = existing
        db.get_book_by_abs_id.return_value = None
        svc = _make_service(db=db)

        with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "true"}):
            result, status = svc.handle_put_progress(
                {"document": "a" * 32, "percentage": 0.1, "device": "sync-bot", "device_id": "BOT", "force": True},
                "127.0.0.1",
            )
        assert status == 200
        db.save_kosync_document.assert_called()

    def test_links_to_existing_book(self):
        db = MagicMock()
        db.get_kosync_document.return_value = _make_doc(linked_abs_id=None)
        book = _make_book()
        db.get_book_by_kosync_id.return_value = book
        svc = _make_service(db=db)

        with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "false"}):
            result, status = svc.handle_put_progress(
                {"document": "a" * 32, "percentage": 0.5, "device": "Kobo", "device_id": "DEV1"}, "127.0.0.1"
            )
        assert status == 200
        db.link_kosync_document.assert_called_once()

    def test_sets_activity_flag_on_paused_book(self):
        db = MagicMock()
        db.get_kosync_document.return_value = _make_doc(linked_abs_id="book-1")
        book = _make_book(status="paused", activity_flag=False)
        db.get_book_by_abs_id.return_value = book
        svc = _make_service(db=db)

        with patch.dict(os.environ, {"KOSYNC_FURTHEST_WINS": "false"}):
            result, status = svc.handle_put_progress(
                {"document": "a" * 32, "percentage": 0.6, "device": "Kobo", "device_id": "DEV1"}, "127.0.0.1"
            )
        assert status == 200
        assert book.activity_flag is True
        # save_book should be called for the activity flag update
        assert db.save_book.called

    def test_validation_errors(self):
        svc = _make_service()
        assert svc.handle_put_progress(None, "127.0.0.1")[1] == 400
        assert svc.handle_put_progress({}, "127.0.0.1")[1] == 400
        assert svc.handle_put_progress({"document": "a" * 32, "percentage": 2.0}, "127.0.0.1")[1] == 400
        assert svc.handle_put_progress({"document": "x" * 100}, "127.0.0.1")[1] == 400


class TestHandleGetProgress:
    def test_direct_hash_match(self):
        db = MagicMock()
        doc = _make_doc(linked_abs_id="book-1")
        db.get_kosync_document.return_value = doc
        book = _make_book()
        db.get_book_by_abs_id.return_value = book
        db.get_kosync_documents_for_book_by_book_id.return_value = [doc]
        db.get_states_for_book.return_value = []
        svc = _make_service(db=db)

        result, status = svc.handle_get_progress("a" * 32, "127.0.0.1")
        assert status == 200
        assert result["percentage"] == 0.5

    def test_lookup_via_book_kosync_id(self):
        db = MagicMock()
        db.get_kosync_document.return_value = None
        book = _make_book()
        db.get_book_by_kosync_id.return_value = book
        db.get_kosync_documents_for_book_by_book_id.return_value = [_make_doc(percentage=0.7)]
        db.get_states_for_book.return_value = []
        svc = _make_service(db=db)

        result, status = svc.handle_get_progress("a" * 32, "127.0.0.1")
        assert status == 200

    def test_unknown_hash_returns_502(self):
        db = MagicMock()
        db.get_kosync_document.return_value = None
        db.get_book_by_kosync_id.return_value = None
        svc = _make_service(db=db)
        svc.resolve_book_by_sibling_hash = MagicMock(return_value=None)
        svc.start_discovery_if_available = MagicMock(return_value=False)

        result, status = svc.handle_get_progress("unknown" + "0" * 25, "127.0.0.1")
        assert status == 502

    def test_doc_id_too_long(self):
        svc = _make_service()
        result, status = svc.handle_get_progress("x" * 100, "127.0.0.1")
        assert status == 400


class TestResolveBestProgress:
    def test_picks_highest_percentage_sibling(self):
        db = MagicMock()
        doc_a = _make_doc(doc_hash="a" * 32, percentage=0.3)
        doc_b = _make_doc(doc_hash="b" * 32, percentage=0.8)
        db.get_kosync_documents_for_book_by_book_id.return_value = [doc_a, doc_b]
        db.get_states_for_book.return_value = []
        svc = _make_service(db=db)

        result, status = svc.resolve_best_progress("c" * 32, _make_book())
        assert status == 200
        assert result["percentage"] == 0.8

    def test_falls_back_to_states(self):
        db = MagicMock()
        db.get_kosync_documents_for_book_by_book_id.return_value = []
        state = MagicMock()
        state.client_name = "KoSync"
        state.percentage = 0.4
        state.xpath = "/body/p[2]"
        state.cfi = None
        state.last_updated = 1700000000
        db.get_states_for_book.return_value = [state]
        svc = _make_service(db=db)

        result, status = svc.resolve_best_progress("d" * 32, _make_book())
        assert status == 200
        assert result["percentage"] == 0.4
        assert result["device"] == "pagekeeper"

    def test_no_data_returns_502(self):
        db = MagicMock()
        db.get_kosync_documents_for_book_by_book_id.return_value = []
        db.get_states_for_book.return_value = []
        svc = _make_service(db=db)

        result, status = svc.resolve_best_progress("e" * 32, _make_book())
        assert status == 502


class TestFindEpubInGrimmory:
    @staticmethod
    def _cached_file(*, book_id, file_id, filename, book_type):
        return SimpleNamespace(
            server_id="default",
            remote_book_id=str(book_id),
            remote_file_id=str(file_id),
            filename=filename,
            title=filename,
            raw_metadata_dict={"bookType": book_type},
        )

    def test_uses_exact_epub_identity_and_deduplicates_rows(self, tmp_path):
        db = MagicMock()
        audio = self._cached_file(book_id=10, file_id=41, filename="book.m4b", book_type="AUDIOBOOK")
        epub = self._cached_file(book_id=10, file_id=42, filename="book.epub", book_type="EPUB")
        duplicate = self._cached_file(book_id=10, file_id=42, filename="book.epub", book_type="EPUB")
        db.get_all_grimmory_books.return_value = [audio, epub, duplicate]
        db.get_kosync_doc_by_grimmory_id.return_value = None
        container = MagicMock()
        group = container.grimmory_client_group.return_value
        group.is_configured.return_value = True
        group.download_book.return_value = b"epub bytes"
        container.ebook_parser.return_value.get_kosync_id_from_bytes.return_value = "target-hash"
        container.data_dir.return_value = tmp_path
        service = _make_service(db=db, container=container)

        assert service._find_epub_in_grimmory("target-hash") == "default_book.epub"

        group.download_book.assert_called_once_with("default:10", file_id="42")
        db.get_kosync_doc_by_grimmory_id.assert_called_once_with("default:10:42")

    def test_rejects_untrusted_remote_filename_before_download(self, tmp_path):
        db = MagicMock()
        db.get_all_grimmory_books.return_value = [
            self._cached_file(book_id=10, file_id=42, filename="../escape.epub", book_type="EPUB")
        ]
        container = MagicMock()
        group = container.grimmory_client_group.return_value
        group.is_configured.return_value = True
        container.data_dir.return_value = tmp_path
        service = _make_service(db=db, container=container)

        assert service._find_epub_in_grimmory("target-hash") is None
        group.download_book.assert_not_called()

    def test_rejects_cache_symlink_escape_after_download(self, tmp_path):
        db = MagicMock()
        db.get_all_grimmory_books.return_value = [
            self._cached_file(book_id=10, file_id=42, filename="book.epub", book_type="EPUB")
        ]
        db.get_kosync_doc_by_grimmory_id.return_value = None
        container = MagicMock()
        group = container.grimmory_client_group.return_value
        group.is_configured.return_value = True
        group.download_book.return_value = b"epub bytes"
        container.ebook_parser.return_value.get_kosync_id_from_bytes.return_value = "target-hash"
        container.data_dir.return_value = tmp_path
        cache_dir = tmp_path / "epub_cache"
        cache_dir.mkdir()
        outside = tmp_path / "outside.epub"
        outside.write_bytes(b"outside")
        (cache_dir / "default_book.epub").symlink_to(outside)
        service = _make_service(db=db, container=container)

        assert service._find_epub_in_grimmory("target-hash") is None
        assert outside.read_bytes() == b"outside"
        db.save_kosync_document.assert_not_called()
