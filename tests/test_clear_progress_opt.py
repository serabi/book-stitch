#!/usr/bin/env python3
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from src.db.models import Book
from src.sync_manager import SyncManager


def test_clear_progress_optimization():
    print("[TEST] Testing Clear Progress optimization...")

    # 1. Setup mocks
    db_service = MagicMock()
    alignment_service = MagicMock()

    book = Book(abs_id="test_book", abs_title="Test Book", status="active")
    db_service.get_book.return_value = book
    db_service.delete_states_for_book.return_value = 5

    sync_manager = SyncManager(
        database_service=db_service,
        alignment_service=alignment_service,
        sync_clients={}
    )

    # CASE A: Smart Reset Enabled (Default), Alignment EXISTS
    print("\n[CASE A] Smart Reset enabled, Alignment EXISTS")
    with patch.dict(os.environ, {"REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT": "true"}):
        alignment_service.has_alignment.return_value = True
        sync_manager.clear_progress("test_book")

        print(f"DEBUG: Book status: {book.status}")
        assert book.status == "not_started", "Book should be not_started after clearing progress with alignment"
        db_service.save_book.assert_called()

    # CASE B: Smart Reset Enabled, Alignment MISSING
    print("\n[CASE B] Smart Reset enabled, Alignment MISSING")
    book.status = "active"
    db_service.save_book.reset_mock()
    alignment_service.has_alignment.return_value = False

    with patch.dict(os.environ, {"REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT": "true"}):
        sync_manager.clear_progress("test_book")

        print(f"DEBUG: Book status: {book.status}")
        assert book.status == "pending", "Book should be marked pending if alignment missing"
        db_service.save_book.assert_called()

    # CASE C: Smart Reset DISABLED
    print("\n[CASE C] Smart Reset DISABLED")
    book.status = "active"
    db_service.save_book.reset_mock()
    alignment_service.has_alignment.return_value = False

    with patch.dict(os.environ, {"REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT": "false"}):
        sync_manager.clear_progress("test_book")

        print(f"DEBUG: Book status: {book.status}")
        assert book.status == "not_started", "Book should be not_started after clearing progress with smart reset disabled"
        db_service.save_book.assert_called()

    print("\n[PASS] All clear_progress optimization tests passed!")

if __name__ == "__main__":
    try:
        test_clear_progress_optimization()
    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
