"""
Unit tests for the database service, including migration testing.
"""

import json
import logging
import os
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Override environment variables for testing
os.environ["DATA_DIR"] = "test_data"
os.environ["BOOKS_DIR"] = "test_data"

# Setup basic logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")


class TestDatabaseServiceIntegration(unittest.TestCase):
    """Unit tests for database service integration functionality."""

    def setUp(self):
        """Set up test environment before each test."""
        # Create temporary directory for test database
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / "test_database.db")

        # Import here to avoid circular imports
        from src.db.database_service import DatabaseMigrator, DatabaseService
        from src.db.models import Book, HardcoverDetails, Job, State

        self.DatabaseService = DatabaseService
        self.DatabaseMigrator = DatabaseMigrator
        self.Book = Book
        self.State = State
        self.Job = Job
        self.HardcoverDetails = HardcoverDetails

        # Create database service
        self.db_service = DatabaseService(self.test_db_path)

    def tearDown(self):
        """Clean up after each test."""
        # Close database connection to release file lock on Windows
        if hasattr(self, "db_service") and hasattr(self.db_service, "db_manager"):
            self.db_service.db_manager.close()

        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_database_service_initialization(self):
        """Test that database service initializes correctly."""
        self.assertIsNotNone(self.db_service)
        self.assertTrue(Path(self.test_db_path).exists())

    def test_create_book(self):
        """Test creating a book record."""
        test_abs_id = "test-book-create"

        book = self.Book(
            abs_id=test_abs_id,
            title="Test Book Creation",
            ebook_filename="test-create.epub",
            kosync_doc_id="test-create-doc",
            status="active",
            duration=3600.0,  # 1 hour test duration
        )

        saved_book = self.db_service.save_book(book)

        self.assertEqual(saved_book.abs_id, test_abs_id)
        self.assertEqual(saved_book.title, "Test Book Creation")
        self.assertEqual(saved_book.status, "active")

    def test_save_detected_book_creates_and_updates(self):
        """Detected books upsert by source and source_id."""
        from src.db.models import DetectedBook

        first = DetectedBook(
            source="abs",
            source_id="detected-1",
            title="Detected Title",
            author="Author One",
            progress_percentage=0.25,
            cover_url="/cover/1",
        )
        saved = self.db_service.save_detected_book(first)
        self.assertEqual(saved.title, "Detected Title")
        self.assertAlmostEqual(saved.progress_percentage, 0.25)

        second = DetectedBook(
            source="abs",
            source_id="detected-1",
            title="Detected Title Updated",
            author="Author Two",
            progress_percentage=0.55,
        )
        updated = self.db_service.save_detected_book(second)

        self.assertEqual(updated.id, saved.id)
        self.assertEqual(updated.title, "Detected Title Updated")
        self.assertEqual(updated.author, "Author Two")
        self.assertAlmostEqual(updated.progress_percentage, 0.55)

    def test_resolve_detected_book_scoped_by_source(self):
        """Resolving a detected book only affects the matching source row."""
        from src.db.models import DetectedBook

        abs_detected = DetectedBook(source="abs", source_id="shared-id", title="ABS", progress_percentage=0.2)
        kosync_detected = DetectedBook(source="kosync", source_id="shared-id", title="KOSync", progress_percentage=0.3)
        self.db_service.save_detected_book(abs_detected)
        self.db_service.save_detected_book(kosync_detected)

        self.assertTrue(self.db_service.resolve_detected_book("shared-id", source="abs"))

        resolved = self.db_service.get_detected_book("shared-id", source="abs")
        still_active = self.db_service.get_detected_book("shared-id", source="kosync")
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(still_active.status, "detected")

    def test_detected_book_claim_is_compare_and_set_and_restorable(self):
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="claim-once", title="Claim", progress_percentage=0.2)
        )

        first_token = self.db_service.claim_detected_book("claim-once", source="abs")
        self.assertIsInstance(first_token, str)
        self.assertIsNone(self.db_service.claim_detected_book("claim-once", source="abs"))
        claimed = self.db_service.get_detected_book("claim-once", source="abs")
        self.assertEqual(claimed.status, "processing")
        self.assertEqual(claimed.processing_token, first_token)
        self.assertFalse(self.db_service.restore_detected_book("claim-once", "wrong", source="abs"))
        self.assertFalse(self.db_service.complete_detected_book("claim-once", "wrong", source="abs"))
        self.assertTrue(self.db_service.restore_detected_book("claim-once", first_token, source="abs"))
        second_token = self.db_service.claim_detected_book("claim-once", source="abs")
        self.assertNotEqual(second_token, first_token)
        self.assertTrue(self.db_service.complete_detected_book("claim-once", second_token, source="abs"))
        self.assertEqual(self.db_service.get_detected_book("claim-once", source="abs").status, "resolved")

    def test_discovery_upsert_preserves_live_claim_until_owner_completes(self):
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="claimed-save", title="Before", progress_percentage=0.2)
        )
        token = self.db_service.claim_detected_book("claimed-save", source="abs")

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="claimed-save", title="After", progress_percentage=0.4)
        )

        claimed = self.db_service.get_detected_book("claimed-save", source="abs")
        self.assertEqual(claimed.status, "processing")
        self.assertEqual(claimed.processing_token, token)
        self.assertTrue(self.db_service.complete_detected_book("claimed-save", token, source="abs"))

    def test_completed_claim_cannot_be_reclaimed_after_lease_expiry(self):
        from src.db.detected_repository import DetectedRepository
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="completed-claim", title="Done", progress_percentage=0.2)
        )
        token = self.db_service.claim_detected_book("completed-claim", source="abs")
        self.assertTrue(self.db_service.complete_detected_book("completed-claim", token, source="abs"))

        with self.db_service.get_session() as session:
            row = session.query(DetectedBook).filter_by(source="abs", source_id="completed-claim").one()
            row.processing_started_at = datetime.now(UTC) - DetectedRepository._PROCESSING_LEASE - timedelta(seconds=1)

        self.assertIsNone(self.db_service.claim_detected_book("completed-claim", source="abs"))
        completed = self.db_service.get_detected_book("completed-claim", source="abs")
        self.assertEqual(completed.status, "resolved")
        self.assertIsNone(completed.processing_token)

    def test_renewed_near_expiry_claim_cannot_be_reclaimed(self):
        from src.db.detected_repository import DetectedRepository
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="renewed-claim", title="Renew", progress_percentage=0.2)
        )
        token = self.db_service.claim_detected_book("renewed-claim", source="abs")
        with self.db_service.get_session() as session:
            row = session.query(DetectedBook).filter_by(source="abs", source_id="renewed-claim").one()
            row.processing_started_at = (
                datetime.now(UTC) - DetectedRepository._PROCESSING_LEASE + timedelta(seconds=1)
            )

        self.assertFalse(self.db_service.renew_detected_book_claim("renewed-claim", "wrong", source="abs"))
        self.assertTrue(self.db_service.renew_detected_book_claim("renewed-claim", token, source="abs"))
        self.assertIsNone(self.db_service.claim_detected_book("renewed-claim", source="abs"))

    def test_dismiss_and_resolve_cannot_revoke_processing_claim(self):
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="claimed-status", title="Claim", progress_percentage=0.2)
        )
        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="normal-status", title="Normal", progress_percentage=0.2)
        )
        token = self.db_service.claim_detected_book("claimed-status", source="abs")

        self.assertFalse(self.db_service.dismiss_detected_book("claimed-status", source="abs"))
        self.assertFalse(self.db_service.resolve_detected_book("claimed-status", source="abs"))
        self.assertTrue(self.db_service.dismiss_detected_book("normal-status", source="abs"))
        claimed = self.db_service.get_detected_book("claimed-status", source="abs")
        normal = self.db_service.get_detected_book("normal-status", source="abs")
        self.assertEqual(claimed.status, "processing")
        self.assertEqual(claimed.processing_token, token)
        self.assertEqual(normal.status, "dismissed")
        self.assertTrue(self.db_service.complete_detected_book("claimed-status", token, source="abs"))

    def test_expired_claim_reappears_and_can_be_reclaimed(self):
        from src.db.detected_repository import DetectedRepository
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="expired-claim", title="Expired", progress_percentage=0.2)
        )
        old_token = self.db_service.claim_detected_book("expired-claim", source="abs")
        with self.db_service.get_session() as session:
            row = session.query(DetectedBook).filter_by(source="abs", source_id="expired-claim").one()
            row.processing_started_at = datetime.now(UTC) - DetectedRepository._PROCESSING_LEASE - timedelta(seconds=1)

        active_ids = {row.source_id for row in self.db_service.get_active_detected_books()}
        self.assertIn("expired-claim", active_ids)
        visible = self.db_service.get_detected_book("expired-claim", source="abs")
        self.assertEqual(visible.status, "detected")
        self.assertIsNone(visible.processing_token)

        new_token = self.db_service.claim_detected_book("expired-claim", source="abs")
        self.assertIsInstance(new_token, str)
        self.assertNotEqual(new_token, old_token)

    def test_dismiss_detected_book_sets_status_on_existing_row(self):
        """Dismissing an existing detected book sets status to dismissed and returns True."""
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="dismiss-existing", title="D", progress_percentage=0.1)
        )

        self.assertTrue(self.db_service.dismiss_detected_book("dismiss-existing", source="abs"))

        row = self.db_service.get_detected_book("dismiss-existing", source="abs")
        self.assertEqual(row.status, "dismissed")

    def test_resolve_detected_book_sets_status_on_existing_row(self):
        """Resolving an existing detected book sets status to resolved and returns True."""
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="resolve-existing", title="R", progress_percentage=0.1)
        )

        self.assertTrue(self.db_service.resolve_detected_book("resolve-existing", source="abs"))

        row = self.db_service.get_detected_book("resolve-existing", source="abs")
        self.assertEqual(row.status, "resolved")

    def test_dismiss_detected_book_missing_returns_false_and_creates_nothing(self):
        """Dismissing a missing detected book returns False without inserting a row."""
        self.assertFalse(self.db_service.dismiss_detected_book("dismiss-missing", source="abs"))
        self.assertIsNone(self.db_service.get_detected_book("dismiss-missing", source="abs"))

    def test_resolve_detected_book_missing_returns_false_and_creates_nothing(self):
        """Resolving a missing detected book returns False without inserting a row."""
        self.assertFalse(self.db_service.resolve_detected_book("resolve-missing", source="abs"))
        self.assertIsNone(self.db_service.get_detected_book("resolve-missing", source="abs"))

    def test_dismiss_detected_book_scoped_by_source(self):
        """Dismissing a detected book only affects the matching source row."""
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="dismiss-shared", title="ABS", progress_percentage=0.2)
        )
        self.db_service.save_detected_book(
            DetectedBook(source="kosync", source_id="dismiss-shared", title="KOSync", progress_percentage=0.3)
        )

        self.assertTrue(self.db_service.dismiss_detected_book("dismiss-shared", source="abs"))

        dismissed = self.db_service.get_detected_book("dismiss-shared", source="abs")
        still_active = self.db_service.get_detected_book("dismiss-shared", source="kosync")
        self.assertEqual(dismissed.status, "dismissed")
        self.assertEqual(still_active.status, "detected")

    def test_dismiss_and_resolve_advance_last_seen_at(self):
        """Both dismiss and resolve refresh last_seen_at on the existing row."""
        from datetime import UTC, datetime

        from src.db.models import DetectedBook

        old_seen = datetime(2020, 1, 1, tzinfo=UTC)

        dismiss_book = DetectedBook(source="abs", source_id="dismiss-seen", title="D", progress_percentage=0.1)
        dismiss_book.last_seen_at = old_seen
        self.db_service.save_detected_book(dismiss_book)

        resolve_book = DetectedBook(source="abs", source_id="resolve-seen", title="R", progress_percentage=0.1)
        resolve_book.last_seen_at = old_seen
        self.db_service.save_detected_book(resolve_book)

        self.assertTrue(self.db_service.dismiss_detected_book("dismiss-seen", source="abs"))
        self.assertTrue(self.db_service.resolve_detected_book("resolve-seen", source="abs"))

        dismissed = self.db_service.get_detected_book("dismiss-seen", source="abs")
        resolved = self.db_service.get_detected_book("resolve-seen", source="abs")
        self.assertGreater(dismissed.last_seen_at.replace(tzinfo=UTC), old_seen)
        self.assertGreater(resolved.last_seen_at.replace(tzinfo=UTC), old_seen)

    def test_save_detected_book_repeat_save_does_not_duplicate(self):
        """Re-saving the same (source_id, source) updates in place, never inserts a duplicate."""
        from src.db.models import DetectedBook

        for _ in range(3):
            self.db_service.save_detected_book(
                DetectedBook(source="abs", source_id="dup-check", title="Dup", progress_percentage=0.1)
            )

        all_for_source = [b for b in self.db_service.get_active_detected_books() if b.source_id == "dup-check"]
        self.assertEqual(len(all_for_source), 1)

    def test_save_detected_book_keeps_dismissed_when_incoming_detected(self):
        """A dismissed row stays dismissed when a later 'detected' save arrives."""
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="dismiss-keep", title="Keep", progress_percentage=0.1)
        )
        self.assertTrue(self.db_service.dismiss_detected_book("dismiss-keep", source="abs"))

        self.db_service.save_detected_book(
            DetectedBook(
                source="abs", source_id="dismiss-keep", title="Keep", progress_percentage=0.4, status="detected"
            )
        )

        row = self.db_service.get_detected_book("dismiss-keep", source="abs")
        self.assertEqual(row.status, "dismissed")
        self.assertAlmostEqual(row.progress_percentage, 0.4)

    def test_save_detected_book_resolved_status_is_applied(self):
        """An incoming non-detected status (e.g. resolved) is applied over a detected row."""
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="status-apply", title="S", progress_percentage=0.1)
        )
        self.db_service.save_detected_book(
            DetectedBook(
                source="abs", source_id="status-apply", title="S", progress_percentage=0.2, status="resolved"
            )
        )

        row = self.db_service.get_detected_book("status-apply", source="abs")
        self.assertEqual(row.status, "resolved")

    def test_save_detected_book_preserves_truthy_only_fields(self):
        """Falsy incoming title/author/cover_url/device/ebook_filename do not overwrite existing values."""
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(
                source="abs",
                source_id="truthy",
                title="Original Title",
                author="Original Author",
                cover_url="/cover/orig",
                progress_percentage=0.1,
                device="OriginalDevice",
                ebook_filename="orig.epub",
            )
        )

        self.db_service.save_detected_book(
            DetectedBook(
                source="abs",
                source_id="truthy",
                title="",
                author=None,
                cover_url="",
                progress_percentage=0.5,
                device="",
                ebook_filename=None,
            )
        )

        row = self.db_service.get_detected_book("truthy", source="abs")
        self.assertEqual(row.title, "Original Title")
        self.assertEqual(row.author, "Original Author")
        self.assertEqual(row.cover_url, "/cover/orig")
        self.assertEqual(row.device, "OriginalDevice")
        self.assertEqual(row.ebook_filename, "orig.epub")
        self.assertAlmostEqual(row.progress_percentage, 0.5)

    def test_save_detected_book_updates_truthy_fields(self):
        """Truthy incoming values for the conditional fields do overwrite existing values."""
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="truthy-upd", title="Old", progress_percentage=0.1)
        )
        self.db_service.save_detected_book(
            DetectedBook(
                source="abs",
                source_id="truthy-upd",
                title="New",
                author="New Author",
                cover_url="/cover/new",
                progress_percentage=0.2,
                device="NewDevice",
                ebook_filename="new.epub",
            )
        )

        row = self.db_service.get_detected_book("truthy-upd", source="abs")
        self.assertEqual(row.title, "New")
        self.assertEqual(row.author, "New Author")
        self.assertEqual(row.cover_url, "/cover/new")
        self.assertEqual(row.device, "NewDevice")
        self.assertEqual(row.ebook_filename, "new.epub")

    def test_save_detected_book_matches_json_none_does_not_overwrite(self):
        """matches_json updates only when incoming is not None; None preserves existing matches."""
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(
                source="abs",
                source_id="matches",
                title="M",
                progress_percentage=0.1,
                matches_json='[{"filename": "a.epub"}]',
            )
        )

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="matches", title="M", progress_percentage=0.2, matches_json=None)
        )
        preserved = self.db_service.get_detected_book("matches", source="abs")
        self.assertEqual(preserved.matches_json, '[{"filename": "a.epub"}]')

        self.db_service.save_detected_book(
            DetectedBook(source="abs", source_id="matches", title="M", progress_percentage=0.3, matches_json="[]")
        )
        replaced = self.db_service.get_detected_book("matches", source="abs")
        self.assertEqual(replaced.matches_json, "[]")

    def test_save_detected_book_last_seen_and_first_detected_behavior(self):
        """last_seen_at advances to incoming value; first_detected_at stays fixed once set."""
        from datetime import UTC, datetime

        from src.db.models import DetectedBook

        original_first = datetime(2020, 1, 1, tzinfo=UTC)
        original_last = datetime(2020, 1, 2, tzinfo=UTC)
        first = DetectedBook(source="abs", source_id="times", title="T", progress_percentage=0.1)
        first.first_detected_at = original_first
        first.last_seen_at = original_last
        self.db_service.save_detected_book(first)

        new_last = datetime(2021, 6, 15, tzinfo=UTC)
        second = DetectedBook(source="abs", source_id="times", title="T", progress_percentage=0.2)
        second.first_detected_at = datetime(2099, 1, 1, tzinfo=UTC)
        second.last_seen_at = new_last
        self.db_service.save_detected_book(second)

        row = self.db_service.get_detected_book("times", source="abs")
        self.assertEqual(row.first_detected_at.replace(tzinfo=UTC), original_first)
        self.assertEqual(row.last_seen_at.replace(tzinfo=UTC), new_last)

    def test_save_detected_book_rejects_stale_source_progress(self):
        from datetime import UTC, datetime

        from src.db.models import DetectedBook

        newer = datetime(2026, 7, 15, 12, tzinfo=UTC)
        older = datetime(2026, 7, 14, 12, tzinfo=UTC)
        self.db_service.save_detected_book(
            DetectedBook(
                source="kosync",
                source_id="stale-progress",
                title="Book",
                progress_percentage=0.8,
                source_updated_at=newer,
            )
        )

        self.db_service.save_detected_book(
            DetectedBook(
                source="kosync",
                source_id="stale-progress",
                title="Book",
                progress_percentage=0.2,
                source_updated_at=older,
            )
        )

        row = self.db_service.get_detected_book("stale-progress", source="kosync")
        self.assertAlmostEqual(row.progress_percentage, 0.8)
        self.assertEqual(row.source_updated_at.replace(tzinfo=UTC), newer)

    def test_save_detected_book_compares_aware_timestamps_in_utc(self):
        from datetime import UTC, datetime

        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(
                source="kosync",
                source_id="offset-progress",
                title="Book",
                progress_percentage=0.8,
                source_updated_at=datetime(2026, 7, 15, 12, tzinfo=UTC),
            )
        )
        self.db_service.save_detected_book(
            DetectedBook(
                source="kosync",
                source_id="offset-progress",
                title="Book",
                progress_percentage=0.2,
                source_updated_at=datetime.fromisoformat("2026-07-15T08:30:00-04:00"),
            )
        )

        row = self.db_service.get_detected_book("offset-progress", source="kosync")
        self.assertAlmostEqual(row.progress_percentage, 0.2)
        self.assertEqual(row.source_updated_at, datetime(2026, 7, 15, 12, 30))

    def test_save_detected_book_missing_source_timestamp_preserves_progress(self):
        from datetime import UTC, datetime

        from src.db.models import DetectedBook

        timestamp = datetime(2026, 7, 15, 12, tzinfo=UTC)
        self.db_service.save_detected_book(
            DetectedBook(
                source="kosync",
                source_id="missing-timestamp",
                title="Book",
                progress_percentage=0.8,
                source_updated_at=timestamp,
            )
        )
        self.db_service.save_detected_book(
            DetectedBook(
                source="kosync",
                source_id="missing-timestamp",
                title="Book",
                progress_percentage=0.1,
                source_updated_at=None,
            )
        )

        row = self.db_service.get_detected_book("missing-timestamp", source="kosync")
        self.assertAlmostEqual(row.progress_percentage, 0.8)
        self.assertEqual(row.source_updated_at, datetime(2026, 7, 15, 12))

    def test_save_detected_book_normalizes_against_concurrently_inserted_row(self):
        """Regression: a row that appears only inside the upsert transaction (not at
        any earlier pre-read) must still get detected-book normalization applied.

        Simulates the race the old code lost: a writer's pre-read sees no row, then a
        concurrent insert lands a dismissed row before the update transaction runs.
        Normalization must run against the row found inside _upsert, so the dismissed
        status is preserved instead of being clobbered back to 'detected'. The
        pre-read is simulated as returning None to prove normalization no longer
        depends on it."""
        from unittest.mock import patch

        from src.db.detected_repository import DetectedRepository
        from src.db.models import DetectedBook

        repo = self.db_service._detected

        # A concurrent writer inserts a dismissed row after the (simulated) pre-read.
        with repo.get_session() as session:
            session.add(
                DetectedBook(
                    source="abs",
                    source_id="race-update",
                    title="Existing Title",
                    author="Existing Author",
                    progress_percentage=0.1,
                    status="dismissed",
                    matches_json='[{"filename": "x.epub"}]',
                )
            )

        # Pre-read returns None: the concurrent insert had not yet landed when an
        # earlier-design caller would have read. The fix must not rely on it.
        with patch.object(DetectedRepository, "get_detected_book", return_value=None):
            repo.save_detected_book(
                DetectedBook(
                    source="abs",
                    source_id="race-update",
                    title="",
                    author=None,
                    progress_percentage=0.6,
                    status="detected",
                    matches_json=None,
                )
            )

        row = self.db_service.get_detected_book("race-update", source="abs")
        self.assertEqual(row.status, "dismissed")
        self.assertEqual(row.title, "Existing Title")
        self.assertEqual(row.author, "Existing Author")
        self.assertEqual(row.matches_json, '[{"filename": "x.epub"}]')
        self.assertAlmostEqual(row.progress_percentage, 0.6)

    def test_save_detected_book_normalizes_in_integrity_error_recovery(self):
        """Regression: the IntegrityError race-recovery branch of _upsert must also
        apply detected-book normalization.

        Forces _upsert down its insert path (existing lookup returns None) while a
        concurrent insert already committed the same (source_id, source). The insert
        raises IntegrityError, recovery re-finds the row, and normalization must run
        against it so the dismissed status survives."""
        from src.db.models import DetectedBook

        repo = self.db_service._detected

        # Pre-existing dismissed row that the recovery lookup will find.
        with repo.get_session() as session:
            session.add(
                DetectedBook(
                    source="abs",
                    source_id="race-integrity",
                    title="Existing Title",
                    author="Existing Author",
                    progress_percentage=0.1,
                    status="dismissed",
                )
            )

        # Force _upsert's initial lookup to miss so it attempts an insert, hitting
        # the unique constraint and dropping into IntegrityError recovery.
        from sqlalchemy.orm import Query

        real_first = Query.first
        state = {"calls": 0}

        def fake_first(self):
            # Only intercept the very first lookup inside _upsert for this model.
            if state["calls"] == 0 and self.column_descriptions and self.column_descriptions[0]["type"] is DetectedBook:
                state["calls"] += 1
                return None
            return real_first(self)

        Query.first = fake_first
        try:
            repo.save_detected_book(
                DetectedBook(
                    source="abs",
                    source_id="race-integrity",
                    title="",
                    author=None,
                    progress_percentage=0.7,
                    status="detected",
                )
            )
        finally:
            Query.first = real_first

        row = self.db_service.get_detected_book("race-integrity", source="abs")
        self.assertEqual(row.status, "dismissed")
        self.assertEqual(row.title, "Existing Title")
        self.assertEqual(row.author, "Existing Author")
        self.assertAlmostEqual(row.progress_percentage, 0.7)

    def _make_detected(self, source_id, last_seen_at, status="detected"):
        from src.db.models import DetectedBook

        book = DetectedBook(
            source="abs",
            source_id=source_id,
            title=source_id,
            progress_percentage=0.1,
            status=status,
        )
        book.last_seen_at = last_seen_at
        return book

    def test_get_active_detected_books_returns_only_detected_status(self):
        """Only rows with status 'detected' are returned; dismissed/resolved are excluded."""
        from datetime import UTC, datetime

        base = datetime(2023, 1, 1, tzinfo=UTC)
        for source_id, status in (
            ("active-a", "detected"),
            ("active-b", "detected"),
            ("gone-dismissed", "detected"),
            ("gone-resolved", "detected"),
        ):
            self.db_service.save_detected_book(self._make_detected(source_id, base, status))

        self.assertTrue(self.db_service.dismiss_detected_book("gone-dismissed", source="abs"))
        self.assertTrue(self.db_service.resolve_detected_book("gone-resolved", source="abs"))

        active_ids = {b.source_id for b in self.db_service.get_active_detected_books()}
        self.assertEqual(active_ids, {"active-a", "active-b"})
        self.assertEqual(self.db_service.get_active_detected_book_count(), 2)
        self.assertEqual(self.db_service.get_detected_book_count(), 4)

    def test_get_active_detected_books_orders_by_last_seen_desc(self):
        """Results are ordered by last_seen_at descending."""
        from datetime import UTC, datetime

        self.db_service.save_detected_book(
            self._make_detected("oldest", datetime(2020, 1, 1, tzinfo=UTC))
        )
        self.db_service.save_detected_book(
            self._make_detected("newest", datetime(2024, 1, 1, tzinfo=UTC))
        )
        self.db_service.save_detected_book(
            self._make_detected("middle", datetime(2022, 1, 1, tzinfo=UTC))
        )

        ordered = [b.source_id for b in self.db_service.get_active_detected_books()]
        self.assertEqual(ordered, ["newest", "middle", "oldest"])

    def test_get_active_detected_books_honors_limit(self):
        """A positive limit caps the number of returned rows, keeping DESC order."""
        from datetime import UTC, datetime

        for i in range(5):
            self.db_service.save_detected_book(
                self._make_detected(f"limit-{i}", datetime(2020, 1, 1 + i, tzinfo=UTC))
            )

        limited = self.db_service.get_active_detected_books(limit=2)
        self.assertEqual([b.source_id for b in limited], ["limit-4", "limit-3"])

    def test_get_active_detected_books_limit_zero_returns_empty(self):
        """limit=0 applies .limit(0) and returns an empty list."""
        from datetime import UTC, datetime

        self.db_service.save_detected_book(
            self._make_detected("present", datetime(2023, 1, 1, tzinfo=UTC))
        )

        self.assertEqual(self.db_service.get_active_detected_books(limit=0), [])

    def test_get_active_detected_books_empty_when_none_active(self):
        """Returns an empty list when no active detected rows exist."""
        from datetime import UTC, datetime

        self.db_service.save_detected_book(
            self._make_detected("only-dismissed", datetime(2023, 1, 1, tzinfo=UTC))
        )
        self.assertTrue(self.db_service.dismiss_detected_book("only-dismissed", source="abs"))

        self.assertEqual(self.db_service.get_active_detected_books(), [])

    def test_get_active_detected_books_rows_detached_after_session_close(self):
        """Returned rows are expunged, so their attributes remain usable after the session closes."""
        from datetime import UTC, datetime

        self.db_service.save_detected_book(
            self._make_detected("detached", datetime(2023, 5, 5, tzinfo=UTC))
        )

        rows = self.db_service.get_active_detected_books()
        self.assertEqual(len(rows), 1)
        # Accessing attributes outside any open session must not raise DetachedInstanceError.
        self.assertEqual(rows[0].source_id, "detached")
        self.assertEqual(rows[0].status, "detected")
        self.assertEqual(rows[0].title, "detached")

    def _save_detected_with_matches(self, source_id, matches_json, status="detected"):
        """Persist a detected row carrying an explicit matches_json string.

        Built directly (not via _make_detected) because that helper does not set
        matches_json. Non-active statuses are applied through the dismiss/resolve
        mutators so normalization does not clobber them back to 'detected'.
        """
        from src.db.models import DetectedBook

        self.db_service.save_detected_book(
            DetectedBook(
                source="abs",
                source_id=source_id,
                title=source_id,
                progress_percentage=0.1,
                matches_json=matches_json,
            )
        )
        if status == "dismissed":
            self.db_service.dismiss_detected_book(source_id, source="abs")
        elif status == "resolved":
            self.db_service.resolve_detected_book(source_id, source="abs")

    def test_get_all_ebook_filenames_returns_filenames_from_active_rows(self):
        """Filenames are collected from active detected rows' matches_json."""
        self._save_detected_with_matches(
            "active-one", '[{"filename": "alpha.epub"}, {"filename": "beta.epub"}]'
        )

        self.assertEqual(
            self.db_service.get_all_ebook_filenames(), {"alpha.epub", "beta.epub"}
        )

    def test_get_all_ebook_filenames_excludes_dismissed_and_resolved(self):
        """Dismissed and resolved rows are excluded even when they carry matches."""
        self._save_detected_with_matches("active", '[{"filename": "active.epub"}]')
        self._save_detected_with_matches(
            "dismissed", '[{"filename": "dismissed.epub"}]', status="dismissed"
        )
        self._save_detected_with_matches(
            "resolved", '[{"filename": "resolved.epub"}]', status="resolved"
        )

        self.assertEqual(self.db_service.get_all_ebook_filenames(), {"active.epub"})

    def test_get_all_ebook_filenames_ignores_matches_json_none(self):
        """Rows with matches_json=None are filtered out by the DB query."""
        self._save_detected_with_matches("has-matches", '[{"filename": "keep.epub"}]')
        self._save_detected_with_matches("no-matches", None)

        self.assertEqual(self.db_service.get_all_ebook_filenames(), {"keep.epub"})

    def test_get_all_ebook_filenames_corrupt_json_contributes_nothing(self):
        """Corrupt JSON yields [] from the matches property and adds no filenames."""
        self._save_detected_with_matches("good", '[{"filename": "good.epub"}]')
        self._save_detected_with_matches("corrupt", "{not valid json")

        self.assertEqual(self.db_service.get_all_ebook_filenames(), {"good.epub"})

    def test_get_all_ebook_filenames_ignores_entries_without_filename(self):
        """Match dicts missing filename or with falsey filename values are skipped."""
        self._save_detected_with_matches(
            "mixed",
            '[{"filename": "kept.epub"}, {"author": "no filename"}, '
            '{"filename": ""}, {"filename": null}]',
        )

        self.assertEqual(self.db_service.get_all_ebook_filenames(), {"kept.epub"})

    def test_get_all_ebook_filenames_collapses_duplicates(self):
        """Identical filenames across rows collapse to a single set entry."""
        self._save_detected_with_matches("first", '[{"filename": "dup.epub"}]')
        self._save_detected_with_matches(
            "second", '[{"filename": "dup.epub"}, {"filename": "dup.epub"}]'
        )

        self.assertEqual(self.db_service.get_all_ebook_filenames(), {"dup.epub"})

    def test_get_all_ebook_filenames_empty_set_when_no_usable_filenames(self):
        """Returns an empty set (not None/list) when nothing usable exists."""
        self._save_detected_with_matches("empty-matches", "[]")
        self._save_detected_with_matches("no-filenames", '[{"author": "x"}]')

        result = self.db_service.get_all_ebook_filenames()
        self.assertEqual(result, set())
        self.assertIsInstance(result, set)

    def test_get_all_ebook_filenames_extracts_after_session_close(self):
        """The matches property is read after _query_and_expunge detaches the rows.

        Reading matches_json post-expunge must not raise DetachedInstanceError;
        a non-empty result proves the scalar column was available after detach.
        """
        self._save_detected_with_matches(
            "detached", '[{"filename": "detached.epub"}]'
        )

        self.assertEqual(
            self.db_service.get_all_ebook_filenames(), {"detached.epub"}
        )

    def test_upsert_without_normalize_hook_is_unaffected(self):
        """Default _upsert callers (no normalize hook) keep blind attribute-copy
        semantics: the existing-row update path overwrites every update attr."""
        from src.db.detected_repository import DetectedRepository
        from src.db.models import DetectedBook

        repo = self.db_service._detected

        with repo.get_session() as session:
            session.add(
                DetectedBook(
                    source="abs",
                    source_id="no-hook",
                    title="Existing Title",
                    author="Existing Author",
                    progress_percentage=0.1,
                    status="dismissed",
                )
            )

        incoming = DetectedBook(
            source="abs",
            source_id="no-hook",
            title="Replaced",
            author="Replaced Author",
            progress_percentage=0.9,
            status="detected",
        )
        repo._upsert(
            DetectedBook,
            [
                DetectedBook.source_id == "no-hook",
                DetectedBook.source == "abs",
            ],
            incoming,
            DetectedRepository.UPSERT_ATTRS,
        )

        row = self.db_service.get_detected_book("no-hook", source="abs")
        self.assertEqual(row.status, "detected")
        self.assertEqual(row.title, "Replaced")
        self.assertEqual(row.author, "Replaced Author")
        self.assertAlmostEqual(row.progress_percentage, 0.9)

    def test_delete_book(self):
        """Test deleting a book record with cascading deletes for states and hardcover details."""
        test_abs_id = "test-book-delete"

        # Create book
        book = self.Book(
            abs_id=test_abs_id,
            title="Test Book Deletion",
            ebook_filename="test-delete.epub",
            kosync_doc_id="test-delete-doc",
            status="active",
            duration=7200.0,  # 2 hour test duration
        )

        book = self.db_service.save_book(book)

        # Create multiple states for the book
        states_data = [
            ("kosync", 0.45, {"xpath": "/delete/test/xpath"}),
            ("abs", 0.42, {"timestamp": 1500.0}),
            ("storyteller", 0.40, {"xpath": "/html/body/section[1]/p[3]"}),
            ("grimmory", 0.38, {"cfi": "epubcfi(/6/6[chapter3]!/4/2/8/1:25)"}),
        ]

        created_states = []
        for client_name, percentage, extra_data in states_data:
            state = self.State(
                abs_id=test_abs_id,
                book_id=book.id,
                client_name=client_name,
                last_updated=time.time(),
                percentage=percentage,
                **extra_data,
            )
            saved_state = self.db_service.save_state(state)
            created_states.append(saved_state)

        # Create hardcover details for the book
        hardcover = self.HardcoverDetails(
            abs_id=test_abs_id,
            book_id=book.id,
            hardcover_book_id="hc-delete-test-123",
            hardcover_edition_id="hc-edition-delete-456",
            hardcover_pages=280,
            isbn="978-9876543210",
            asin="B08DELETETEST",
            matched_by="title",
        )

        self.db_service.save_hardcover_details(hardcover)

        # Create a job for the book
        job = self.Job(
            abs_id=test_abs_id, book_id=book.id, last_attempt=time.time(), retry_count=3, last_error="Delete test error"
        )

        self.db_service.save_job(job)

        # Verify all data exists before deletion
        retrieved_book = self.db_service.get_book_by_abs_id(test_abs_id)
        self.assertIsNotNone(retrieved_book)

        retrieved_states = self.db_service.get_states_for_book(book.id)
        self.assertEqual(len(retrieved_states), 4)

        retrieved_hardcover = self.db_service.get_hardcover_details(book.id)
        self.assertIsNotNone(retrieved_hardcover)

        retrieved_job = self.db_service.get_latest_job(book.id)
        self.assertIsNotNone(retrieved_job)

        # Delete the book - this should cascade delete all related data
        success = self.db_service.delete_book(book.id)
        self.assertTrue(success)

        # Verify book is gone
        deleted_book = self.db_service.get_book_by_abs_id(test_abs_id)
        self.assertIsNone(deleted_book)

        # Verify states are gone (cascade delete)
        deleted_states = self.db_service.get_states_for_book(book.id)
        self.assertEqual(len(deleted_states), 0)

        # Verify hardcover details are gone (cascade delete)
        deleted_hardcover = self.db_service.get_hardcover_details(book.id)
        self.assertIsNone(deleted_hardcover)

        # Verify job is gone (cascade delete)
        deleted_job = self.db_service.get_latest_job(book.id)
        self.assertIsNone(deleted_job)

    def test_create_states(self):
        """Test creating state records for multiple clients."""
        test_abs_id = "test-book-states"

        # Create book first
        book = self.Book(
            abs_id=test_abs_id,
            title="Test Book States",
            ebook_filename="test-states.epub",
            kosync_doc_id="test-states-doc",
            status="active",
        )
        book = self.db_service.save_book(book)

        # Create states for different clients
        states_data = [
            ("kosync", 0.35, {"xpath": "/test/xpath"}),
            ("abs", 0.32, {"timestamp": 1200.5}),
            ("storyteller", 0.30, {"xpath": "/html/body/section[2]/p[5]"}),
            ("grimmory", 0.28, {"cfi": "epubcfi(/6/4[chapter2]!/4/2/6/1:15)"}),
        ]

        for client_name, percentage, extra_data in states_data:
            state = self.State(
                abs_id=test_abs_id,
                book_id=book.id,
                client_name=client_name,
                last_updated=time.time(),
                percentage=percentage,
                **extra_data,
            )
            saved_state = self.db_service.save_state(state)
            self.assertEqual(saved_state.client_name, client_name)
            self.assertEqual(saved_state.percentage, percentage)

        # Retrieve and verify all states
        states = self.db_service.get_states_for_book(book.id)
        self.assertEqual(len(states), len(states_data))

        # Verify each client has correct data
        state_by_client = {s.client_name: s for s in states}

        self.assertIn("kosync", state_by_client)
        self.assertEqual(state_by_client["kosync"].xpath, "/test/xpath")

        self.assertIn("abs", state_by_client)
        self.assertEqual(state_by_client["abs"].timestamp, 1200.5)

        self.assertIn("storyteller", state_by_client)
        self.assertEqual(state_by_client["storyteller"].xpath, "/html/body/section[2]/p[5]")

        self.assertIn("grimmory", state_by_client)
        self.assertEqual(state_by_client["grimmory"].cfi, "epubcfi(/6/4[chapter2]!/4/2/6/1:15)")

    def test_save_state_updates_existing_book_client_pair(self):
        """save_state updates a book/client state instead of inserting duplicates."""
        book = self.db_service.save_book(self.Book(abs_id="state-upsert-book", title="State Upsert"))

        first = self.db_service.save_state(
            self.State(
                abs_id=book.abs_id,
                book_id=book.id,
                client_name="kosync",
                last_updated=100.0,
                percentage=0.25,
            )
        )
        second = self.db_service.save_state(
            self.State(
                abs_id=book.abs_id,
                book_id=book.id,
                client_name="kosync",
                last_updated=200.0,
                percentage=0.75,
                xpath="/updated",
            )
        )

        states = self.db_service.get_states_for_book(book.id)
        self.assertEqual(len(states), 1)
        self.assertEqual(first.id, second.id)
        self.assertEqual(states[0].percentage, 0.75)
        self.assertEqual(states[0].xpath, "/updated")

    def test_save_state_dedupes_existing_duplicate_book_client_rows(self):
        """save_state collapses dirty pre-existing duplicates before updating."""
        import sqlite3

        book = self.db_service.save_book(self.Book(abs_id="dirty-state-book", title="Dirty State"))
        self.db_service.db_manager.close()

        conn = sqlite3.connect(self.test_db_path)
        conn.execute("DROP INDEX IF EXISTS uq_states_book_id_client_name")
        conn.executemany("""
            INSERT INTO states (abs_id, book_id, client_name, last_updated, percentage)
            VALUES (?, ?, ?, ?, ?)
        """, [
            (book.abs_id, book.id, "kosync", 100.0, 0.10),
            (book.abs_id, book.id, "kosync", 200.0, 0.20),
        ])
        conn.commit()
        conn.close()

        self.db_service = self.DatabaseService(self.test_db_path)
        saved = self.db_service.save_state(
            self.State(
                abs_id=book.abs_id,
                book_id=book.id,
                client_name="kosync",
                last_updated=300.0,
                percentage=0.90,
            )
        )

        states = self.db_service.get_states_for_book(book.id)
        self.assertEqual(len(states), 1)
        self.assertEqual(saved.id, states[0].id)
        self.assertEqual(states[0].percentage, 0.90)

    def test_save_state_retries_after_unique_conflict(self):
        """save_state recovers when a unique conflict appears after lookup."""
        book = self.db_service.save_book(self.Book(abs_id="state-race-book", title="State Race"))
        existing = self.db_service.save_state(
            self.State(
                abs_id=book.abs_id,
                book_id=book.id,
                client_name="kosync",
                last_updated=100.0,
                percentage=0.10,
            )
        )

        repo = self.db_service._books
        original_dedupe = repo._dedupe_existing_states
        calls = 0

        def miss_once(session, lookup_filters):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original_dedupe(session, lookup_filters)

        repo._dedupe_existing_states = miss_once
        try:
            saved = self.db_service.save_state(
                self.State(
                    abs_id=book.abs_id,
                    book_id=book.id,
                    client_name="kosync",
                    last_updated=250.0,
                    percentage=0.65,
                )
            )
        finally:
            repo._dedupe_existing_states = original_dedupe

        states = self.db_service.get_states_for_book(book.id)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(len(states), 1)
        self.assertEqual(saved.id, existing.id)
        self.assertEqual(states[0].percentage, 0.65)

    def test_save_state_recovers_from_genuine_unique_index_conflict(self):
        """A real partial unique index conflict on the second save_state is
        recovered by updating the existing row (no raise, single row)."""
        book = self.db_service.save_book(self.Book(abs_id="genuine-conflict-book", title="Genuine Conflict"))
        existing = self.db_service.save_state(
            self.State(
                abs_id=book.abs_id,
                book_id=book.id,
                client_name="kosync",
                last_updated=100.0,
                percentage=0.10,
            )
        )

        repo = self.db_service._books
        original_dedupe = repo._dedupe_existing_states
        calls = 0

        def miss_first_lookup(session, lookup_filters):
            nonlocal calls
            calls += 1
            if calls == 1:
                # Force the INSERT path so the real uq_states_book_id_client_name
                # partial unique index raises a genuine IntegrityError.
                return None
            return original_dedupe(session, lookup_filters)

        repo._dedupe_existing_states = miss_first_lookup
        try:
            saved = self.db_service.save_state(
                self.State(
                    abs_id=book.abs_id,
                    book_id=book.id,
                    client_name="kosync",
                    last_updated=250.0,
                    percentage=0.65,
                )
            )
        finally:
            repo._dedupe_existing_states = original_dedupe

        states = self.db_service.get_states_for_book(book.id)
        self.assertGreaterEqual(calls, 2)
        self.assertIsNotNone(saved)
        self.assertEqual(len(states), 1)
        self.assertEqual(saved.id, existing.id)
        self.assertEqual(states[0].percentage, 0.65)

    def test_save_state_recovery_never_reads_orm_state_after_rollback(self):
        """The conflict-recovery branch must operate on the snapshot, never the
        ORM state object (whose post-rollback lifecycle is unreliable)."""
        book = self.db_service.save_book(self.Book(abs_id="armed-state-book", title="Armed State"))
        existing = self.db_service.save_state(
            self.State(
                abs_id=book.abs_id,
                book_id=book.id,
                client_name="kosync",
                last_updated=100.0,
                percentage=0.10,
            )
        )

        repo = self.db_service._books
        original_dedupe = repo._dedupe_existing_states
        original_snapshot = repo._snapshot_state_scalars
        calls = 0
        snapshot_taken = False
        incoming_state = self.State(
            abs_id=book.abs_id,
            book_id=book.id,
            client_name="kosync",
            last_updated=250.0,
            percentage=0.65,
        )

        def miss_first_lookup(session, lookup_filters):
            nonlocal calls
            calls += 1
            if calls == 1:
                # Force the INSERT path so the real unique index conflicts.
                return None
            return original_dedupe(session, lookup_filters)

        def snapshot_then_mark(state, update_attrs):
            # Snapshot is captured from the live ORM object (allowed). After this
            # point — i.e. in the post-rollback recovery branch — the ORM state
            # object must never be read again. We don't subclass the mapped State
            # model (that would register a stray entity in the global declarative
            # registry and corrupt later metadata/migration tests); instead we
            # spy on the helpers that take the ORM `state`, which the fixed
            # recovery branch must avoid.
            nonlocal snapshot_taken
            snapshot_taken = True
            return original_snapshot(state, update_attrs)

        def guard_state_helper(name, original):
            def wrapper(*args, **kwargs):
                if snapshot_taken:
                    raise AssertionError(f"recovery branch used ORM-state helper '{name}' after snapshot")
                return original(*args, **kwargs)

            return wrapper

        original_hydrate = repo._hydrate_state_book_reference
        original_state_lookup = repo._state_lookup_filters
        original_apply_state = repo._apply_state_attrs

        repo._dedupe_existing_states = miss_first_lookup
        repo._snapshot_state_scalars = snapshot_then_mark
        repo._hydrate_state_book_reference = guard_state_helper(
            "_hydrate_state_book_reference", original_hydrate
        )
        repo._state_lookup_filters = guard_state_helper("_state_lookup_filters", original_state_lookup)
        repo._apply_state_attrs = guard_state_helper("_apply_state_attrs", original_apply_state)
        try:
            saved = self.db_service.save_state(incoming_state)
        finally:
            repo._dedupe_existing_states = original_dedupe
            repo._snapshot_state_scalars = original_snapshot
            repo._hydrate_state_book_reference = original_hydrate
            repo._state_lookup_filters = original_state_lookup
            repo._apply_state_attrs = original_apply_state

        states = self.db_service.get_states_for_book(book.id)
        self.assertGreaterEqual(calls, 2)
        self.assertIsNotNone(saved)
        self.assertEqual(len(states), 1)
        self.assertEqual(saved.id, existing.id)
        self.assertEqual(states[0].percentage, 0.65)

    def test_save_state_resolves_legacy_abs_id_only_input(self):
        """Legacy callers that only pass abs_id still update the canonical book state."""
        book = self.db_service.save_book(self.Book(abs_id="legacy-abs-state", title="Legacy State"))

        first = self.db_service.save_state(
            self.State(abs_id=book.abs_id, client_name="kosync", last_updated=100.0, percentage=0.10)
        )
        second = self.db_service.save_state(
            self.State(abs_id=book.abs_id, client_name="kosync", last_updated=200.0, percentage=0.55)
        )

        states = self.db_service.get_states_for_book(book.id)
        self.assertEqual(len(states), 1)
        self.assertEqual(first.id, second.id)
        self.assertEqual(states[0].book_id, book.id)
        self.assertEqual(states[0].percentage, 0.55)

    def test_save_state_skips_unresolvable_abs_id_only_input(self):
        """Legacy abs_id-only saves skip cleanly when no book can be resolved."""
        saved = self.db_service.save_state(
            self.State(abs_id="missing-book", client_name="kosync", last_updated=100.0, percentage=0.10)
        )

        self.assertIsNone(saved)
        matching = [state for state in self.db_service.get_all_states() if state.abs_id == "missing-book"]
        self.assertEqual(matching, [])

    def test_get_books_by_status(self):
        """Test querying books by status."""
        # Create books with different statuses
        active_book = self.Book(
            abs_id="active-book",
            title="Active Book",
            ebook_filename="active.epub",
            kosync_doc_id="active-doc",
            status="active",
        )

        paused_book = self.Book(
            abs_id="paused-book",
            title="Paused Book",
            ebook_filename="paused.epub",
            kosync_doc_id="paused-doc",
            status="paused",
        )

        self.db_service.save_book(active_book)
        self.db_service.save_book(paused_book)

        # Test active books query
        active_books = self.db_service.get_books_by_status("active")
        active_ids = [book.abs_id for book in active_books]
        self.assertIn("active-book", active_ids)
        self.assertNotIn("paused-book", active_ids)

        # Test paused books query
        paused_books = self.db_service.get_books_by_status("paused")
        paused_ids = [book.abs_id for book in paused_books]
        self.assertIn("paused-book", paused_ids)
        self.assertNotIn("active-book", paused_ids)

    def test_statistics(self):
        """Test database statistics functionality."""
        initial_stats = self.db_service.get_statistics()
        initial_books = initial_stats["total_books"]
        initial_states = initial_stats["total_states"]

        # Add test data
        test_abs_id = "test-stats-book"
        book = self.Book(
            abs_id=test_abs_id,
            title="Statistics Test Book",
            ebook_filename="stats.epub",
            kosync_doc_id="stats-doc",
            status="active",
        )
        book = self.db_service.save_book(book)

        # Add states
        state = self.State(
            abs_id=test_abs_id, book_id=book.id, client_name="kosync", last_updated=time.time(), percentage=0.5
        )
        self.db_service.save_state(state)

        # Check updated statistics
        updated_stats = self.db_service.get_statistics()
        self.assertEqual(updated_stats["total_books"], initial_books + 1)
        self.assertEqual(updated_stats["total_states"], initial_states + 1)

    def test_migration_should_migrate(self):
        """Test migration detection logic."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test JSON files
            mapping_json = temp_path / "mapping.json"
            state_json = temp_path / "state.json"

            # Create empty JSON files
            mapping_json.write_text('{"mappings": []}')
            state_json.write_text("{}")

            # Create fresh database for migration test
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                migrator = self.DatabaseMigrator(migration_db_service, str(mapping_json), str(state_json))

                # Should migrate when database is empty and JSON files exist
                self.assertTrue(migrator.should_migrate())

                # Add a book to database
                book = self.Book(abs_id="existing-book", title="Existing Book")
                migration_db_service.save_book(book)

                # Should not migrate when database has data
                self.assertFalse(migrator.should_migrate())
            finally:
                migration_db_service.db_manager.close()

    def test_migration_mapping_json(self):
        """Test migration of mapping JSON data."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test mapping JSON
            mapping_json_path = temp_path / "mapping.json"
            mapping_data = {
                "mappings": [
                    {
                        "abs_id": "migration-book-1",
                        "title": "Migration Test Book 1",
                        "ebook_filename": "migration1.epub",
                        "kosync_doc_id": "migration-kosync-1",
                        "transcript_file": "transcript1.json",
                        "status": "active",
                        "hardcover_book_id": "hc-123",
                        "hardcover_pages": 350,
                        "isbn": "978-1234567890",
                        "retry_count": 2,
                        "last_error": "Migration test error",
                    }
                ]
            }

            with open(mapping_json_path, "w") as f:
                json.dump(mapping_data, f)

            # Create empty state JSON
            state_json_path = temp_path / "state.json"
            with open(state_json_path, "w") as f:
                json.dump({}, f)

            # Create database for migration
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                # Perform migration
                migrator = self.DatabaseMigrator(migration_db_service, str(mapping_json_path), str(state_json_path))

                migrator.migrate()

                # Verify book was migrated
                migrated_book = migration_db_service.get_book_by_abs_id("migration-book-1")
                self.assertIsNotNone(migrated_book)
                self.assertEqual(migrated_book.title, "Migration Test Book 1")
                self.assertEqual(migrated_book.status, "active")

                # Verify hardcover details were migrated
                hardcover = migration_db_service.get_hardcover_details(migrated_book.id)
                self.assertIsNotNone(hardcover)
                self.assertEqual(hardcover.hardcover_book_id, "hc-123")
                self.assertEqual(hardcover.hardcover_pages, 350)
                self.assertEqual(hardcover.isbn, "978-1234567890")

                # Verify job data was migrated
                job = migration_db_service.get_latest_job(migrated_book.id)
                self.assertIsNotNone(job)
                self.assertEqual(job.retry_count, 2)
                self.assertIn("Migration test error", job.last_error)
            finally:
                migration_db_service.db_manager.close()

    def test_migration_state_json(self):
        """Test migration of state JSON data."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test mapping JSON (minimal)
            mapping_json_path = temp_path / "mapping.json"
            mapping_data = {
                "mappings": [{"abs_id": "state-migration-book", "title": "State Migration Test", "status": "active"}]
            }

            with open(mapping_json_path, "w") as f:
                json.dump(mapping_data, f)

            # Create test state JSON
            state_json_path = temp_path / "state.json"
            state_data = {
                "state-migration-book": {
                    "last_updated": time.time() - 3600,
                    "kosync_pct": 0.45,
                    "kosync_xpath": "/html/body/div[1]/p[12]",
                    "abs_pct": 0.42,
                    "abs_ts": 1250.5,
                    "absebook_pct": 0.46,
                    "absebook_cfi": "epubcfi(/6/10[chapter5]!/4/2/8/1:45)",
                    "storyteller_pct": 0.44,
                    "storyteller_xpath": "/html/body/section[3]/p[8]",
                    "grimmory_pct": 0.43,
                    "grimmory_xpath": "/html/body/article[2]/div[1]/p[15]",
                }
            }

            with open(state_json_path, "w") as f:
                json.dump(state_data, f)

            # Create database for migration
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                # Perform migration
                migrator = self.DatabaseMigrator(migration_db_service, str(mapping_json_path), str(state_json_path))

                migrator.migrate()

                # Verify states were migrated
                migrated_book = migration_db_service.get_book_by_abs_id("state-migration-book")
                states = migration_db_service.get_states_for_book(migrated_book.id)
                self.assertEqual(len(states), 5)  # kosync, abs, absebook, storyteller, grimmory

                state_by_client = {s.client_name: s for s in states}

                # Check kosync state
                kosync_state = state_by_client["kosync"]
                self.assertEqual(kosync_state.percentage, 0.45)
                self.assertEqual(kosync_state.xpath, "/html/body/div[1]/p[12]")

                # Check ABS state
                abs_state = state_by_client["abs"]
                self.assertEqual(abs_state.percentage, 0.42)
                self.assertEqual(abs_state.timestamp, 1250.5)

                # Check ABS eBook state
                absebook_state = state_by_client["absebook"]
                self.assertEqual(absebook_state.percentage, 0.46)
                self.assertEqual(absebook_state.cfi, "epubcfi(/6/10[chapter5]!/4/2/8/1:45)")

                # Check Storyteller state
                storyteller_state = state_by_client["storyteller"]
                self.assertEqual(storyteller_state.percentage, 0.44)
                self.assertEqual(storyteller_state.xpath, "/html/body/section[3]/p[8]")

                # Check Grimmory state
                grimmory_state = state_by_client["grimmory"]
                self.assertEqual(grimmory_state.percentage, 0.43)
                self.assertEqual(grimmory_state.xpath, "/html/body/article[2]/div[1]/p[15]")
            finally:
                migration_db_service.db_manager.close()

    def test_migration_idempotency(self):
        """Test that migration doesn't create duplicates when run multiple times."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test JSON files
            mapping_json_path = temp_path / "mapping.json"
            mapping_data = {
                "mappings": [{"abs_id": "idempotency-test", "title": "Idempotency Test Book", "status": "active"}]
            }

            with open(mapping_json_path, "w") as f:
                json.dump(mapping_data, f)

            state_json_path = temp_path / "state.json"
            state_data = {"idempotency-test": {"kosync_pct": 0.5, "abs_pct": 0.5}}

            with open(state_json_path, "w") as f:
                json.dump(state_data, f)

            # Create database for migration
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                migrator = self.DatabaseMigrator(migration_db_service, str(mapping_json_path), str(state_json_path))

                # First migration
                self.assertTrue(migrator.should_migrate())
                migrator.migrate()

                # Check initial counts
                stats_after_first = migration_db_service.get_statistics()
                books_after_first = stats_after_first["total_books"]
                states_after_first = stats_after_first["total_states"]

                # Second migration should not be needed
                self.assertFalse(migrator.should_migrate())

                # Force second migration anyway
                migrator.migrate()

                # Check counts haven't changed (no duplicates)
                stats_after_second = migration_db_service.get_statistics()
                self.assertEqual(stats_after_second["total_books"], books_after_first)
                self.assertEqual(stats_after_second["total_states"], states_after_first)
            finally:
                migration_db_service.db_manager.close()

    def test_clear_stale_suggestions(self):
        """Test clearing suggestions that are not for active books."""
        from src.db.models import PendingSuggestion

        # 1. Setup Active Books
        active_id = "active-book-id"
        book = self.Book(abs_id=active_id, title="Active Book", status="active")
        self.db_service.save_book(book)

        # 2. Setup Suggestions
        # Suggestion for the active book (should be preserved)
        s1 = PendingSuggestion(source_id=active_id, title="Active Book Title", author="Author A", matches_json="[]")
        self.db_service.save_pending_suggestion(s1)

        # Suggestion for a non-existent book (should be cleared)
        stale_id = "stale-book-id"
        s2 = PendingSuggestion(source_id=stale_id, title="Stale Book Title", author="Author B", matches_json="[]")
        self.db_service.save_pending_suggestion(s2)

        # Verify initial state
        all_suggestions = self.db_service.get_all_actionable_suggestions()
        self.assertEqual(len(all_suggestions), 2)

        # 3. Clear Stale Suggestions
        cleared_count = self.db_service.clear_stale_suggestions()
        self.assertEqual(cleared_count, 1)

        # 4. Verify Final State
        remaining = self.db_service.get_all_actionable_suggestions()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].source_id, active_id)

    def test_hide_unhide_and_resolve_suggestion(self):
        """Test suggestion status transitions for hide, unhide, and resolve."""
        from src.db.models import PendingSuggestion

        suggestion = PendingSuggestion(
            source_id="suggestion-123", title="Test Suggestion", author="Author", matches_json="[]"
        )
        self.db_service.save_pending_suggestion(suggestion)

        actionable = self.db_service.get_all_actionable_suggestions()
        self.assertEqual(len(actionable), 1)
        self.assertEqual(actionable[0].status, "pending")

        self.assertTrue(self.db_service.hide_suggestion("suggestion-123"))
        hidden = self.db_service.get_hidden_suggestions()
        self.assertEqual(len(hidden), 1)
        self.assertEqual(hidden[0].status, "hidden")

        self.assertTrue(self.db_service.unhide_suggestion("suggestion-123"))
        pending = self.db_service.get_all_pending_suggestions()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].status, "pending")

        self.assertTrue(self.db_service.resolve_suggestion("suggestion-123"))
        self.assertEqual(self.db_service.get_all_actionable_suggestions(), [])

    def test_migration_partial_data(self):
        """Test migration with partial/missing data scenarios."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create mapping with book that has no states
            mapping_json_path = temp_path / "mapping.json"
            mapping_data = {
                "mappings": [
                    {"abs_id": "no-states-book", "title": "Book Without States", "status": "active"},
                    {"abs_id": "partial-states-book", "title": "Book With Partial States", "status": "active"},
                ]
            }

            with open(mapping_json_path, "w") as f:
                json.dump(mapping_data, f)

            # State JSON only has data for one book, and only some clients
            state_json_path = temp_path / "state.json"
            state_data = {
                "partial-states-book": {
                    "kosync_pct": 0.3,
                    "abs_pct": 0.25,
                    # Missing storyteller, grimmory, absebook
                }
                # Missing no-states-book entirely
            }

            with open(state_json_path, "w") as f:
                json.dump(state_data, f)

            # Perform migration
            migration_db_path = temp_path / "migration.db"
            migration_db_service = self.DatabaseService(str(migration_db_path))

            try:
                migrator = self.DatabaseMigrator(migration_db_service, str(mapping_json_path), str(state_json_path))

                migrator.migrate()

                # Verify both books were migrated
                book1 = migration_db_service.get_book_by_abs_id("no-states-book")
                self.assertIsNotNone(book1)

                book2 = migration_db_service.get_book_by_abs_id("partial-states-book")
                self.assertIsNotNone(book2)

                # Verify states
                states1 = migration_db_service.get_states_for_book(book1.id)
                self.assertEqual(len(states1), 0)  # No states

                states2 = migration_db_service.get_states_for_book(book2.id)
                self.assertEqual(len(states2), 2)  # Only kosync and abs

                state_clients = [s.client_name for s in states2]
                self.assertIn("kosync", state_clients)
                self.assertIn("abs", state_clients)
                self.assertNotIn("storyteller", state_clients)
                self.assertNotIn("grimmory", state_clients)
            finally:
                migration_db_service.db_manager.close()

    # ── T1: get_book_by_ref() resolution — all 5 branches ──

    def test_get_book_by_ref_none(self):
        """get_book_by_ref(None) returns None."""
        self.assertIsNone(self.db_service.get_book_by_ref(None))

    def test_get_book_by_ref_integer_id(self):
        """get_book_by_ref with an integer returns the book by primary key."""
        book = self.Book(abs_id="ref-int-test", title="Ref Int", status="active")
        saved = self.db_service.save_book(book)
        result = self.db_service.get_book_by_ref(saved.id)
        self.assertIsNotNone(result)
        self.assertEqual(result.id, saved.id)

    def test_get_book_by_ref_abs_id_string(self):
        """get_book_by_ref with a string abs_id returns the matching book."""
        book = self.Book(abs_id="abs-ref-test", title="Ref Abs", status="active")
        self.db_service.save_book(book)
        result = self.db_service.get_book_by_ref("abs-ref-test")
        self.assertIsNotNone(result)
        self.assertEqual(result.abs_id, "abs-ref-test")

    def test_get_book_by_ref_numeric_string_fallthrough(self):
        """get_book_by_ref with a numeric string falls through to book_id lookup."""
        book = self.Book(abs_id="numeric-fall", title="Numeric Fallthrough", status="active")
        saved = self.db_service.save_book(book)
        # Use the numeric book ID as a string — should fall through abs_id miss to int lookup
        result = self.db_service.get_book_by_ref(str(saved.id))
        self.assertIsNotNone(result)
        self.assertEqual(result.id, saved.id)

    def test_get_book_by_ref_nonexistent_string(self):
        """get_book_by_ref with a non-numeric string that doesn't match any abs_id returns None."""
        result = self.db_service.get_book_by_ref("does-not-exist")
        self.assertIsNone(result)

    # ── T2: Book without abs_id — full lifecycle ──

    def test_book_without_abs_id_lifecycle(self):
        """Create a Book with no abs_id, verify auto-id, retrieve, attach states."""
        book = self.Book(title="Standalone Ebook", status="not_started", sync_mode="ebook_only")
        saved = self.db_service.save_book(book, is_new=True)
        self.assertIsNotNone(saved.id)
        self.assertIsNone(saved.abs_id)

        retrieved = self.db_service.get_book_by_ref(saved.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.title, "Standalone Ebook")

        # Attach a state
        state = self.State(
            book_id=saved.id,
            client_name="KoSync",
            timestamp=100.0,
            percentage=0.25,
            last_updated=1000.0,
        )
        self.db_service.save_state(state)
        states = self.db_service.get_states_by_book()
        self.assertIn(saved.id, states)
        self.assertEqual(len(states[saved.id]), 1)

    # ── T3: save_book() three-way upsert ──

    def test_save_book_with_id(self):
        """save_book with book.id set updates the existing record."""
        book = self.Book(abs_id="upsert-by-id", title="Original", status="active")
        saved = self.db_service.save_book(book, is_new=True)
        saved.title = "Updated by ID"
        self.db_service.save_book(saved)
        result = self.db_service.get_book_by_ref(saved.id)
        self.assertEqual(result.title, "Updated by ID")

    def test_save_book_with_abs_id(self):
        """save_book with book.abs_id set (no id) upserts by abs_id."""
        book = self.Book(abs_id="upsert-by-abs", title="Upserted", status="active")
        self.db_service.save_book(book, is_new=True)
        book2 = self.Book(abs_id="upsert-by-abs", title="Re-upserted", status="active")
        self.db_service.save_book(book2)
        all_books = [b for b in self.db_service.get_all_books() if b.abs_id == "upsert-by-abs"]
        self.assertEqual(len(all_books), 1)
        self.assertEqual(all_books[0].title, "Re-upserted")

    def test_save_book_new_no_abs_id(self):
        """save_book with neither id nor abs_id creates a new book."""
        book = self.Book(title="Brand New", status="not_started")
        saved = self.db_service.save_book(book, is_new=True)
        self.assertIsNotNone(saved.id)
        self.assertIsNone(saved.abs_id)

    # ── T4: AlignmentService with book_id ──

    def test_alignment_save_retrieve_delete_by_book_id(self):
        """Save, retrieve, and delete alignment by book_id."""
        from unittest.mock import MagicMock

        from src.services.alignment_service import AlignmentService

        book = self.Book(abs_id="align-test", title="Alignment Test", status="active")
        saved = self.db_service.save_book(book)

        polisher = MagicMock()
        svc = AlignmentService(self.db_service, polisher)

        # Save alignment
        svc._save_alignment(saved.id, [{"char": 0, "ts": 0.0}, {"char": 1000, "ts": 60.0}], source="test")

        # has_alignment
        self.assertTrue(svc.has_alignment(saved.id))
        self.assertFalse(svc.has_alignment(999999))

        # get_alignment_info
        info = svc.get_alignment_info(saved.id)
        self.assertIsNotNone(info)
        self.assertEqual(info["num_points"], 2)

        # get_time_for_text
        ts = svc.get_time_for_text(saved.id, char_offset_hint=500)
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(ts, 30.0, delta=1.0)

        # get_char_for_time
        char = svc.get_char_for_time(saved.id, timestamp=30.0)
        self.assertIsNotNone(char)
        self.assertAlmostEqual(char, 500, delta=10)

        # get_book_duration
        dur = svc.get_book_duration(saved.id)
        self.assertAlmostEqual(dur, 60.0, delta=0.1)

        # delete_alignment
        svc.delete_alignment(saved.id)
        self.assertFalse(svc.has_alignment(saved.id))


class TestLegacyDatabaseMigration(unittest.TestCase):
    """
    Tests that simulate a pre-Alembic (legacy) database to verify the
    baseline-stamp upgrade path works for users who skip multiple releases.

    The crash scenario:
      1. User has a database created before Alembic was introduced.
      2. The database has a 'books' table but NO 'alembic_version' table.
      3. On startup, DatabaseService calls command.upgrade("head").
      4. Alembic tries to run initial_database_schema which calls op.create_table("books").
      5. SQLite raises OperationalError: table books already exists → container crashes.

    The fix:
      Before running upgrade, detect this state and stamp the DB at the initial
      revision (76886bc89d6e), then run the remaining migrations on top. This
      preserves the original tables while still executing later schema changes.
    """

    def _make_legacy_db(self, db_path: str):
        """
        Create a bare SQLite database that mimics a pre-Alembic installation.
        Manually creates all tables that initial_database_schema (76886bc89d6e)
        would have created, but WITHOUT an alembic_version table. This is the
        exact state a legacy user's database would be in before upgrading.

        We must create all tables from that migration (books, hardcover_details,
        states, jobs) because stamping at 76886bc89d6e tells Alembic those tables
        already exist. Only creating 'books' would cause subsequent ADD COLUMN
        migrations to fail with 'no such table: hardcover_details'.
        """
        import sqlite3

        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                abs_id TEXT NOT NULL UNIQUE,
                abs_title TEXT,
                ebook_filename TEXT,
                kosync_doc_id TEXT,
                transcript_file TEXT,
                status TEXT DEFAULT 'active',
                duration REAL
            );

            CREATE TABLE hardcover_details (
                abs_id TEXT PRIMARY KEY,
                hardcover_book_id TEXT,
                hardcover_edition_id TEXT,
                hardcover_pages INTEGER,
                isbn TEXT,
                asin TEXT,
                matched_by TEXT,
                FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
            );

            CREATE TABLE states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                abs_id TEXT NOT NULL,
                client_name TEXT NOT NULL,
                last_updated REAL,
                percentage REAL,
                timestamp REAL,
                xpath TEXT,
                cfi TEXT,
                FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
            );

            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                abs_id TEXT NOT NULL,
                last_attempt REAL,
                retry_count INTEGER DEFAULT 0,
                last_error TEXT,
                FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
            );
        """)
        # Insert a row so we can verify data is preserved after migration
        conn.execute("""
            INSERT INTO books (abs_id, abs_title, status)
            VALUES ('legacy-book-1', 'My Legacy Book', 'active')
        """)
        conn.commit()
        conn.close()

    def _alembic_config(self, db_path: str):
        from alembic.config import Config

        alembic_dir = Path(__file__).parent.parent / "alembic"
        alembic_ini = alembic_dir.parent / "alembic.ini"
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option("script_location", str(alembic_dir))
        alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        return alembic_cfg

    def test_detected_identity_migration_downgrade_has_deterministic_collision_policy(self):
        import sqlite3

        from alembic import command

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "detected_identity.db")
            alembic_cfg = self._alembic_config(db_path)
            command.upgrade(alembic_cfg, "head")

            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO detected_books (source, source_id, title, progress_percentage) VALUES (?, ?, ?, ?)",
                ("grimmory", "2:same.epub", "Secondary", 0.7),
            )
            conn.execute(
                "INSERT INTO detected_books (source, source_id, title, progress_percentage) VALUES (?, ?, ?, ?)",
                ("grimmory", "default:same.epub", "Primary", 0.3),
            )
            conn.execute(
                "INSERT INTO detected_books (source, source_id, title, progress_percentage) VALUES (?, ?, ?, ?)",
                ("grimmory", "2:other.epub", "Other", 0.4),
            )
            conn.commit()
            conn.close()

            command.downgrade(alembic_cfg, "x5y6z7a8b9c0")

            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT source_id, title FROM detected_books WHERE source = 'grimmory' ORDER BY source_id"
            ).fetchall()
            conn.close()
            self.assertEqual(rows, [("other.epub", "Other"), ("same.epub", "Primary")])

            command.upgrade(alembic_cfg, "head")
            conn = sqlite3.connect(db_path)
            source_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT source_id FROM detected_books WHERE source = 'grimmory' ORDER BY source_id"
                ).fetchall()
            ]
            conn.close()
            self.assertEqual(source_ids, ["default:other.epub", "default:same.epub"])

    def test_legacy_db_does_not_crash_on_startup(self):
        """
        Scenario: legacy database with 'books' but no 'alembic_version'.
        DatabaseService.__init__ must complete without raising any exception.
        Previously this would crash with: OperationalError: table books already exists
        """
        import sqlite3

        from src.db.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "legacy.db")
            self._make_legacy_db(db_path)

            # Verify precondition: books exists, alembic_version does not
            conn = sqlite3.connect(db_path)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            conn.close()
            self.assertIn("books", tables)
            self.assertNotIn("alembic_version", tables)

            # This must not raise — previously it would crash here
            try:
                db_service = DatabaseService(db_path)
                db_service.db_manager.close()
            except Exception as e:
                self.fail(
                    f"DatabaseService raised {type(e).__name__} on legacy database: {e}\n"
                    "This means the legacy stamp fix is not working."
                )

    def test_legacy_db_runs_remaining_migrations_after_baseline_stamp(self):
        """
        After DatabaseService starts up against a legacy database, the alembic_version
        table must exist and the database must reflect newer migrations that ran
        after the baseline stamp. This is the contract that makes skipped-version
        upgrades safe.
        """
        import sqlite3

        from src.db.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "legacy_stamp.db")
            self._make_legacy_db(db_path)

            db_service = DatabaseService(db_path)
            db_service.db_manager.close()

            # alembic_version must now exist and hold a revision
            conn = sqlite3.connect(db_path)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("alembic_version", tables, "alembic_version table was not created after stamp")

            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            self.assertIsNotNone(version, "alembic_version table is empty after startup")
            self.assertTrue(len(version[0]) > 0, f"Unexpected empty version_num: {version[0]!r}")

            books_cols = {row[1]: row for row in conn.execute("PRAGMA table_info(books)").fetchall()}
            self.assertIn("id", books_cols, "books.id was not added by later migrations")
            self.assertIn("sync_mode", books_cols, "books.sync_mode was not added by later migrations")
            self.assertIn("storyteller_uuid", books_cols, "books.storyteller_uuid was not added by later migrations")

            reading_journal_tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertIn(
                "reading_journals",
                reading_journal_tables,
                "Later migrations did not create reading_journals on a legacy database",
            )
            self.assertIn(
                "kosync_documents",
                reading_journal_tables,
                "Later migrations did not create kosync_documents on a legacy database",
            )
            conn.close()

    def test_legacy_db_preserves_existing_data(self):
        """
        Data that existed in the legacy database before migration must survive intact.
        The stamp+upgrade process must never destroy existing rows.
        """
        import sqlite3

        from src.db.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "legacy_data.db")
            self._make_legacy_db(db_path)  # inserts 'legacy-book-1'

            db_service = DatabaseService(db_path)

            # The pre-existing book row must still be readable via the service
            book = db_service.get_book_by_abs_id("legacy-book-1")
            db_service.db_manager.close()

            self.assertIsNotNone(book, "Pre-existing legacy book was lost after migration")
            self.assertEqual(book.title, "My Legacy Book")
            self.assertEqual(book.status, "active")

    def test_intermediate_revision_upgrades_to_head(self):
        """
        Scenario: a user who upgraded once (got to initial Alembic revision)
        but then skipped several releases. Their DB is stamped at 76886bc89d6e
        with the original 4-table schema. Calling upgrade("head") must produce
        the full modern schema including books.id PK and states.book_id FK.
        """
        import sqlite3

        from alembic.config import Config

        from alembic import command
        from src.db.database_service import LEGACY_BASELINE_REVISION

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "intermediate.db")

            # Create the exact schema from the initial migration (76886bc89d6e)
            # NOTE: uses abs_title (the original column name at that revision)
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    abs_id TEXT NOT NULL UNIQUE,
                    abs_title TEXT,
                    ebook_filename TEXT,
                    kosync_doc_id TEXT,
                    transcript_file TEXT,
                    status TEXT DEFAULT 'active',
                    duration REAL
                );

                CREATE TABLE hardcover_details (
                    abs_id TEXT PRIMARY KEY,
                    hardcover_book_id TEXT,
                    hardcover_edition_id TEXT,
                    hardcover_pages INTEGER,
                    isbn TEXT,
                    asin TEXT,
                    matched_by TEXT,
                    FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
                );

                CREATE TABLE states (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    abs_id TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    last_updated REAL,
                    percentage REAL,
                    timestamp REAL,
                    xpath TEXT,
                    cfi TEXT,
                    FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
                );

                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    abs_id TEXT NOT NULL,
                    last_attempt REAL,
                    retry_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    FOREIGN KEY (abs_id) REFERENCES books(abs_id) ON DELETE CASCADE
                );
            """)
            # Insert test data to verify it survives
            conn.execute("""
                INSERT INTO books (abs_id, abs_title, status)
                VALUES ('intermediate-book', 'Intermediate Test', 'active')
            """)
            conn.execute("""
                INSERT INTO states (abs_id, client_name, percentage)
                VALUES ('intermediate-book', 'kosync', 0.5)
            """)
            conn.commit()
            conn.close()

            # Stamp at the initial revision (simulating a user who already ran
            # the initial migration but hasn't upgraded since)
            alembic_dir = Path(__file__).parent.parent / "alembic"
            alembic_ini = alembic_dir.parent / "alembic.ini"
            alembic_cfg = Config(str(alembic_ini))
            alembic_cfg.set_main_option("script_location", str(alembic_dir))
            alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
            command.stamp(alembic_cfg, LEGACY_BASELINE_REVISION)

            # Now let DatabaseService do its normal startup (which calls upgrade("head"))
            from src.db.database_service import DatabaseService

            db_service = DatabaseService(db_path)
            db_service.db_manager.close()

            # Verify the modern schema was applied
            conn = sqlite3.connect(db_path)

            # books.id should still exist (was already there)
            books_cols = {row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()}
            self.assertIn("id", books_cols)
            self.assertIn("sync_mode", books_cols, "Later migration columns not applied")

            # states.book_id must exist (added by Phase 2)
            states_cols = {row[1] for row in conn.execute("PRAGMA table_info(states)").fetchall()}
            self.assertIn("book_id", states_cols, "states.book_id not added by Phase 2")

            # reading_journals table must exist (created by later migration)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("reading_journals", tables)
            self.assertIn("kosync_documents", tables)

            # Verify data survived
            book = conn.execute("SELECT title FROM books WHERE abs_id = 'intermediate-book'").fetchone()
            self.assertIsNotNone(book, "Test book was lost during migration")
            self.assertEqual(book[0], "Intermediate Test")

            # Verify states.book_id was populated correctly
            state = conn.execute("SELECT book_id FROM states WHERE abs_id = 'intermediate-book'").fetchone()
            self.assertIsNotNone(state, "Test state was lost during migration")
            self.assertIsNotNone(state[0], "states.book_id was not populated")

            conn.close()

    def test_startup_script_delegates_migrations_to_app(self):
        """start.sh must not run Alembic directly. On a pre-Alembic DB a direct
        `alembic upgrade head` fails with 'table books already exists' and
        leaves an empty alembic_version table behind, which used to poison the
        app's legacy recovery path. DatabaseService is the single migration
        authority for container startup.
        """
        start_sh = (Path(__file__).parent.parent / "start.sh").read_text()
        command_lines = [line for line in start_sh.splitlines() if not line.lstrip().startswith("#")]
        self.assertNotIn("alembic", "\n".join(command_lines))

    def test_legacy_db_poisoned_by_direct_alembic_recovers_on_startup(self):
        """Simulates the released container startup path against a pre-Alembic DB:
        start.sh used to run `alembic upgrade head` before the app started. That
        fails on 'table books already exists' and leaves an EMPTY alembic_version
        table. DatabaseService startup must recover that exact state instead of
        failing and continuing on the old schema.
        """
        import sqlite3

        from sqlalchemy.exc import OperationalError

        from alembic import command
        from src.db.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "poisoned.db")
            self._make_legacy_db(db_path)

            # Step 1: what start.sh's direct `alembic upgrade head` did
            with self.assertRaises(OperationalError):
                command.upgrade(self._alembic_config(db_path), "head")

            # Precondition: poisoned state — empty alembic_version + legacy schema
            conn = sqlite3.connect(db_path)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("alembic_version", tables)
            self.assertEqual(conn.execute("SELECT count(*) FROM alembic_version").fetchone()[0], 0)
            conn.close()

            # Step 2: app startup must fully recover, not continue degraded
            db_service = DatabaseService(db_path)
            self.assertEqual(db_service.startup_ready(), (True, "ok"))
            book = db_service.get_book_by_abs_id("legacy-book-1")
            db_service.db_manager.close()
            self.assertIsNotNone(book, "Pre-existing legacy book was lost during recovery")
            self.assertEqual(book.title, "My Legacy Book")

            conn = sqlite3.connect(db_path)
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            self.assertIsNotNone(version, "alembic_version still empty after recovery")
            books_cols = {row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()}
            self.assertIn("title", books_cols, "abs_title→title migration did not run during recovery")
            states_cols = {row[1] for row in conn.execute("PRAGMA table_info(states)").fetchall()}
            self.assertIn("book_id", states_cols, "states.book_id not added during recovery")
            conn.close()

    def test_empty_alembic_version_on_unknown_db_fails_closed(self):
        """A populated DB with an empty alembic_version table that does NOT match
        the pre-Alembic baseline is an interrupted migration we cannot resume.
        Startup must abort loudly instead of stamping or patching the schema.
        """
        import sqlite3

        from src.db.database_service import DatabaseSchemaError, DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "unknown.db")
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE books (abs_id TEXT PRIMARY KEY, abs_title TEXT);
                CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
            """)
            conn.execute("INSERT INTO books (abs_id, abs_title) VALUES ('b1', 'Book')")
            conn.commit()
            conn.close()

            with self.assertRaises(DatabaseSchemaError):
                DatabaseService(db_path)

            # The DB must not have been stamped or mutated
            conn = sqlite3.connect(db_path)
            self.assertEqual(conn.execute("SELECT count(*) FROM alembic_version").fetchone()[0], 0)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            conn.close()
            self.assertEqual(tables, {"books", "alembic_version"})

    def test_interrupted_migration_recovery_failure_fails_closed(self):
        """If baseline-stamp recovery itself fails, startup must abort rather than
        continue with a partially migrated schema.
        """
        import sqlite3
        from unittest.mock import patch

        from src.db.database_service import DatabaseSchemaError, DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "broken_recovery.db")
            self._make_legacy_db(db_path)
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            conn.commit()
            conn.close()

            with patch("alembic.command.upgrade", side_effect=RuntimeError("boom")):
                with self.assertRaises(DatabaseSchemaError):
                    DatabaseService(db_path)

    def test_state_uniqueness_migration_dedupes_before_index(self):
        """Duplicate states from the previous head are collapsed before adding uniqueness."""
        import sqlite3

        from alembic import command

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "state_dedupe.db")
            alembic_cfg = self._alembic_config(db_path)

            command.upgrade(alembic_cfg, "v2w3x4y5z6a7")

            conn = sqlite3.connect(db_path)
            cursor = conn.execute("""
                INSERT INTO books (abs_id, title, status)
                VALUES ('dedupe-book', 'Dedupe Book', 'active')
            """)
            book_id = cursor.lastrowid
            conn.executescript(f"""
                INSERT INTO states (abs_id, book_id, client_name, last_updated, percentage)
                VALUES
                    ('dedupe-book', {book_id}, 'kosync', 100.0, 0.10),
                    ('dedupe-book', {book_id}, 'kosync', 300.0, 0.30),
                    ('dedupe-book', {book_id}, 'kosync', 200.0, 0.20),
                    ('dedupe-book', {book_id}, 'abs', 50.0, 0.05);
            """)
            conn.commit()
            conn.close()

            command.upgrade(alembic_cfg, "head")

            conn = sqlite3.connect(db_path)
            rows = conn.execute("""
                SELECT client_name, percentage
                FROM states
                WHERE book_id = ?
                ORDER BY client_name
            """, (book_id,)).fetchall()
            index_names = {row[1] for row in conn.execute("PRAGMA index_list(states)").fetchall()}
            state_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(states)").fetchall()}
            self.assertIn("uq_states_book_id_client_name", index_names)
            self.assertEqual(state_columns["book_id"][3], 0)
            self.assertEqual(rows, [("abs", 0.05), ("kosync", 0.30)])

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""
                    INSERT INTO states (abs_id, book_id, client_name, percentage)
                    VALUES ('dedupe-book', ?, 'kosync', 0.99)
                """, (book_id,))
                conn.commit()
            conn.close()

    def test_state_uniqueness_migration_treats_null_last_updated_as_oldest(self):
        """A duplicate with NULL last_updated loses to any row with a real timestamp.

        The dedupe SQL coalesces last_updated to -1, so a NULL-timestamped row
        must be discarded in favour of a sibling that has an actual timestamp,
        regardless of insertion order.
        """
        import sqlite3

        from alembic import command

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "state_null_ts.db")
            alembic_cfg = self._alembic_config(db_path)

            command.upgrade(alembic_cfg, "v2w3x4y5z6a7")

            conn = sqlite3.connect(db_path)
            cursor = conn.execute("""
                INSERT INTO books (abs_id, title, status)
                VALUES ('null-ts-book', 'Null Timestamp Book', 'active')
            """)
            book_id = cursor.lastrowid
            # The NULL-timestamp row is inserted LAST (higher id) to prove the
            # tiebreak is driven by last_updated, not by insertion order.
            conn.executescript(f"""
                INSERT INTO states (abs_id, book_id, client_name, last_updated, percentage)
                VALUES
                    ('null-ts-book', {book_id}, 'kosync', 100.0, 0.60),
                    ('null-ts-book', {book_id}, 'kosync', NULL, 0.99);
            """)
            conn.commit()
            conn.close()

            command.upgrade(alembic_cfg, "head")

            conn = sqlite3.connect(db_path)
            rows = conn.execute("""
                SELECT last_updated, percentage
                FROM states
                WHERE book_id = ? AND client_name = 'kosync'
            """, (book_id,)).fetchall()
            conn.close()

            self.assertEqual(len(rows), 1, "Duplicate kosync rows were not collapsed")
            self.assertEqual(rows[0], (100.0, 0.60),
                             "NULL last_updated row should have lost to the timestamped row")

    def test_state_uniqueness_migration_downgrade_drops_unique_index(self):
        """Downgrading the head revision removes the unique index but keeps the rows."""
        import sqlite3

        from alembic import command

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "state_downgrade.db")
            alembic_cfg = self._alembic_config(db_path)

            command.upgrade(alembic_cfg, "head")

            conn = sqlite3.connect(db_path)
            cursor = conn.execute("""
                INSERT INTO books (abs_id, title, status)
                VALUES ('downgrade-book', 'Downgrade Book', 'active')
            """)
            book_id = cursor.lastrowid
            conn.execute("""
                INSERT INTO states (abs_id, book_id, client_name, last_updated, percentage)
                VALUES ('downgrade-book', ?, 'kosync', 100.0, 0.10)
            """, (book_id,))
            conn.commit()
            indexes_before = {row[1] for row in conn.execute("PRAGMA index_list(states)").fetchall()}
            conn.close()

            self.assertIn("uq_states_book_id_client_name", indexes_before)

            command.downgrade(alembic_cfg, "v2w3x4y5z6a7")

            conn = sqlite3.connect(db_path)
            indexes_after = {row[1] for row in conn.execute("PRAGMA index_list(states)").fetchall()}
            surviving = conn.execute(
                "SELECT percentage FROM states WHERE book_id = ?", (book_id,)
            ).fetchone()
            conn.close()

            self.assertNotIn("uq_states_book_id_client_name", indexes_after,
                             "Downgrade did not drop the unique index")
            self.assertIsNotNone(surviving, "Downgrade should not delete state rows")
            self.assertEqual(surviving[0], 0.10)

    def test_state_rebuild_column_list_matches_state_model(self):
        """The states table rebuild must copy every State column.

        ``w3x4y5z6a7b8`` rebuilds the states table to relax book_id nullability
        by copying a hardcoded column list. If a future migration adds a column
        to the State model without updating that copy, the rebuild would
        silently drop the new column's data. This guard fails loudly at that
        point instead.
        """
        import importlib.util
        import inspect as _inspect

        from src.db.models import State

        migration_path = (
            Path(__file__).parent.parent
            / "alembic"
            / "versions"
            / "w3x4y5z6a7b8_add_unique_state_book_client_index.py"
        )
        spec = importlib.util.spec_from_file_location("_w3x4_migration", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        # Read the actual copy statement out of the migration source so the test
        # tracks the real INSERT...SELECT, not a duplicated literal.
        source = _inspect.getsource(migration._relax_states_book_id_nullability)
        self.assertIn("INSERT INTO _states_nullable_new", source)

        model_columns = {column.name for column in State.__table__.columns}
        for column_name in model_columns:
            self.assertIn(
                column_name,
                source,
                f"State.{column_name} is missing from the states rebuild copy in "
                f"w3x4y5z6a7b8; the migration would silently drop its data.",
            )

    def test_fresh_db_still_initializes_correctly(self):
        """
        Regression guard: a brand-new (empty) database must still initialize cleanly.
        The legacy detection must not interfere with normal first-run behaviour.
        """
        from src.db.database_service import DatabaseService

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "fresh.db")

            # File must not exist yet — genuine first run
            self.assertFalse(Path(db_path).exists())

            try:
                db_service = DatabaseService(db_path)
                db_service.db_manager.close()
            except Exception as e:
                self.fail(f"DatabaseService raised {type(e).__name__} on fresh database: {e}")

            self.assertTrue(Path(db_path).exists(), "Database file was not created")


class TestUnsafeSchemaFailsClosed(unittest.TestCase):
    """
    Populated databases that cannot be verified as a known upgrade path must
    fail closed: DatabaseService must raise DatabaseSchemaError without
    stamping alembic_version or mutating the schema. Silently stamping or
    patching such databases can permanently corrupt user data.
    """

    LEGACY_TABLE_DDL = {
        "books": """
            CREATE TABLE books (
                abs_id TEXT PRIMARY KEY,
                abs_title TEXT,
                ebook_filename TEXT,
                kosync_doc_id TEXT,
                transcript_file TEXT,
                status TEXT DEFAULT 'active',
                duration REAL
            );
        """,
        "hardcover_details": """
            CREATE TABLE hardcover_details (
                abs_id TEXT PRIMARY KEY,
                hardcover_book_id TEXT,
                hardcover_edition_id TEXT,
                hardcover_pages INTEGER,
                isbn TEXT,
                asin TEXT,
                matched_by TEXT
            );
        """,
        "states": """
            CREATE TABLE states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                abs_id TEXT NOT NULL,
                client_name TEXT NOT NULL,
                last_updated REAL,
                percentage REAL,
                timestamp REAL,
                xpath TEXT,
                cfi TEXT
            );
        """,
        "jobs": """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                abs_id TEXT NOT NULL,
                last_attempt REAL,
                retry_count INTEGER DEFAULT 0,
                last_error TEXT
            );
        """,
    }

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "unsafe.db")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_db(self, tables, with_empty_alembic_version=False):
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        for table in tables:
            conn.executescript(self.LEGACY_TABLE_DDL[table])
        if "books" in tables:
            conn.execute("INSERT INTO books (abs_id, abs_title) VALUES ('user-book-1', 'Precious Data')")
        if with_empty_alembic_version:
            conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        conn.commit()
        conn.close()

    def _table_names(self):
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        return tables

    def _assert_fails_closed(self, expected_tables):
        """DatabaseService must raise and leave the database untouched."""
        from src.db.database_service import DatabaseSchemaError, DatabaseService

        with self.assertRaises(DatabaseSchemaError):
            DatabaseService(self.db_path)

        tables = self._table_names()
        self.assertEqual(
            tables - {"sqlite_sequence"},
            expected_tables,
            "Fail-closed startup must not create or drop tables",
        )
        self.assertNotIn("alembic_version", tables, "Unsafe database must not be stamped")

    def test_partial_legacy_books_and_states_only_fails_closed(self):
        """A legacy DB missing hardcover_details and jobs must not be baseline-stamped:
        replaying migrations on it fails midway and leaves a half-upgraded schema."""
        self._make_db(["books", "states"])
        self._assert_fails_closed({"books", "states"})

    def test_partial_legacy_missing_hardcover_details_fails_closed(self):
        self._make_db(["books", "states", "jobs"])
        self._assert_fails_closed({"books", "states", "jobs"})

    def test_partial_legacy_missing_jobs_fails_closed(self):
        self._make_db(["books", "states", "hardcover_details"])
        self._assert_fails_closed({"books", "states", "hardcover_details"})

    def test_unknown_populated_books_only_fails_closed(self):
        """An unknown populated schema must never be stamped at head."""
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE books (abs_id TEXT, abs_title TEXT)")
        conn.execute("INSERT INTO books VALUES ('user-book-1', 'Precious Data')")
        conn.commit()
        conn.close()

        self._assert_fails_closed({"books"})

    def test_baseline_table_missing_column_fails_closed(self):
        """All four tables present but a baseline column missing must not be stamped."""
        import sqlite3

        self._make_db(["states", "hardcover_details", "jobs"])
        conn = sqlite3.connect(self.db_path)
        # books without abs_title — e.g. a manually repaired or renamed schema
        conn.execute("CREATE TABLE books (abs_id TEXT PRIMARY KEY, title TEXT)")
        conn.commit()
        conn.close()

        self._assert_fails_closed({"books", "states", "hardcover_details", "jobs"})

    def test_empty_alembic_version_with_verified_baseline_recovers(self):
        """An empty alembic_version table (left by an interrupted direct
        `alembic upgrade head`) on a fully verified baseline must recover:
        baseline-stamp, upgrade to head, and preserve data."""
        import sqlite3

        from src.db.database_service import DatabaseService

        self._make_db(["books", "states", "hardcover_details", "jobs"], with_empty_alembic_version=True)

        db_service = DatabaseService(self.db_path)
        book = db_service.get_book_by_abs_id("user-book-1")
        db_service.db_manager.close()

        self.assertIsNotNone(book, "Pre-existing book was lost during recovery")
        self.assertEqual(book.title, "Precious Data")

        conn = sqlite3.connect(self.db_path)
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        books_cols = {r[1] for r in conn.execute("PRAGMA table_info(books)").fetchall()}
        conn.close()
        self.assertIsNotNone(version, "alembic_version was not stamped during recovery")
        self.assertIn("id", books_cols, "Later migrations did not run after recovery stamp")

    def test_empty_alembic_version_with_unknown_schema_fails_closed(self):
        """An empty alembic_version table on an unverified schema must not be stamped."""
        import sqlite3

        from src.db.database_service import DatabaseSchemaError, DatabaseService

        self._make_db(["books"], with_empty_alembic_version=True)

        with self.assertRaises(DatabaseSchemaError):
            DatabaseService(self.db_path)

        conn = sqlite3.connect(self.db_path)
        versions = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        conn.close()
        self.assertEqual(versions, [], "Unverified database must not be stamped")

    def test_stale_alembic_version_fails_closed(self):
        """A revision unknown to the migration history must abort startup unchanged."""
        import sqlite3

        from src.db.database_service import DatabaseSchemaError, DatabaseService

        db_service = DatabaseService(self.db_path)
        db_service.db_manager.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE alembic_version SET version_num = 'deadbeef12345'")
        conn.commit()
        conn.close()

        with self.assertRaises(DatabaseSchemaError):
            DatabaseService(self.db_path)

        conn = sqlite3.connect(self.db_path)
        versions = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        conn.close()
        self.assertEqual(versions, [("deadbeef12345",)], "Stale revision must be left untouched")

    def test_missing_model_column_after_migrations_fails_closed(self):
        """A model column missing from an up-to-date DB must abort startup instead
        of being patched in with a raw ALTER TABLE (the previous behavior)."""
        import sqlite3

        from src.db.database_service import DatabaseSchemaError, DatabaseService

        db_service = DatabaseService(self.db_path)
        db_service.db_manager.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE books DROP COLUMN subtitle")
        conn.commit()
        conn.close()

        with self.assertRaises(DatabaseSchemaError):
            DatabaseService(self.db_path)

        conn = sqlite3.connect(self.db_path)
        books_cols = {r[1] for r in conn.execute("PRAGMA table_info(books)").fetchall()}
        conn.close()
        self.assertNotIn("subtitle", books_cols, "Missing model column must not be silently patched in")


class TestSuggestionSourceScoping(unittest.TestCase):
    """Tests that suggestion operations are scoped by (source_id, source)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / "test_database.db")
        from src.db.database_service import DatabaseService
        from src.db.models import PendingSuggestion

        self.db_service = DatabaseService(self.test_db_path)
        self.PendingSuggestion = PendingSuggestion

    def tearDown(self):
        if hasattr(self, "db_service") and hasattr(self.db_service, "db_manager"):
            self.db_service.db_manager.close()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_suggestion_exists_scoped_by_source(self):
        """suggestion_exists returns False when the source_id exists under a different source."""
        suggestion = self.PendingSuggestion(
            source_id="id1",
            title="Test",
            source="kosync",
        )
        self.db_service.save_pending_suggestion(suggestion)
        self.assertTrue(self.db_service.suggestion_exists("id1", source="kosync"))
        self.assertFalse(self.db_service.suggestion_exists("id1", source="abs"))

    def test_suggestion_exists_true_regardless_of_status(self):
        """suggestion_exists returns True for any row, including hidden and ignored."""
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="Test", source="abs")
        )
        self.assertTrue(self.db_service.suggestion_exists("id1", source="abs"))

        self.assertTrue(self.db_service.hide_suggestion("id1", source="abs"))
        self.assertEqual(self.db_service.get_suggestion("id1", source="abs").status, "hidden")
        self.assertTrue(self.db_service.suggestion_exists("id1", source="abs"))

        self.assertTrue(self.db_service.ignore_suggestion("id1", source="abs"))
        self.assertEqual(self.db_service.get_suggestion("id1", source="abs").status, "ignored")
        self.assertTrue(self.db_service.suggestion_exists("id1", source="abs"))

    def test_suggestion_exists_false_when_missing(self):
        """suggestion_exists returns False when no matching row exists."""
        self.assertFalse(self.db_service.suggestion_exists("missing", source="abs"))

    def test_is_suggestion_ignored_only_for_ignored_status(self):
        """is_suggestion_ignored returns True only when the row's status is exactly 'ignored'."""
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="Test", source="abs")
        )
        self.assertFalse(self.db_service.is_suggestion_ignored("id1", source="abs"))

        self.assertTrue(self.db_service.hide_suggestion("id1", source="abs"))
        self.assertFalse(self.db_service.is_suggestion_ignored("id1", source="abs"))

        self.assertTrue(self.db_service.ignore_suggestion("id1", source="abs"))
        self.assertTrue(self.db_service.is_suggestion_ignored("id1", source="abs"))

    def test_is_suggestion_ignored_scoped_by_source(self):
        """is_suggestion_ignored only matches the row under the given source."""
        s_abs = self.PendingSuggestion(source_id="id1", title="ABS", source="abs")
        s_kosync = self.PendingSuggestion(source_id="id1", title="KOSync", source="kosync")
        self.db_service.save_pending_suggestion(s_abs)
        self.db_service.save_pending_suggestion(s_kosync)

        self.assertTrue(self.db_service.ignore_suggestion("id1", source="kosync"))

        self.assertTrue(self.db_service.is_suggestion_ignored("id1", source="kosync"))
        self.assertFalse(self.db_service.is_suggestion_ignored("id1", source="abs"))

    def test_is_suggestion_ignored_false_when_missing(self):
        """is_suggestion_ignored returns False when no matching row exists."""
        self.assertFalse(self.db_service.is_suggestion_ignored("missing", source="abs"))

    def test_upsert_different_source_creates_two_rows(self):
        """save_pending_suggestion with same source_id but different source creates distinct rows."""
        s1 = self.PendingSuggestion(source_id="id1", title="ABS Title", source="abs")
        s2 = self.PendingSuggestion(source_id="id1", title="KOSync Title", source="kosync")
        self.db_service.save_pending_suggestion(s1)
        self.db_service.save_pending_suggestion(s2)

        abs_suggestion = self.db_service.get_suggestion("id1", source="abs")
        kosync_suggestion = self.db_service.get_suggestion("id1", source="kosync")
        self.assertIsNotNone(abs_suggestion)
        self.assertIsNotNone(kosync_suggestion)
        self.assertEqual(abs_suggestion.title, "ABS Title")
        self.assertEqual(kosync_suggestion.title, "KOSync Title")

    def test_resolve_scoped_by_source(self):
        """resolve_suggestion only deletes the row matching the given source."""
        s1 = self.PendingSuggestion(source_id="id1", title="ABS Title", source="abs")
        s2 = self.PendingSuggestion(source_id="id1", title="KOSync Title", source="kosync")
        self.db_service.save_pending_suggestion(s1)
        self.db_service.save_pending_suggestion(s2)

        self.db_service.resolve_suggestion("id1", source="abs")

        self.assertFalse(self.db_service.suggestion_exists("id1", source="abs"))
        self.assertTrue(self.db_service.suggestion_exists("id1", source="kosync"))

    def test_save_pending_suggestion_insert_returns_persisted_row(self):
        """First save inserts a row and returns it with the given fields."""
        suggestion = self.PendingSuggestion(
            source_id="id1",
            title="Title",
            author="Author",
            cover_url="http://example/c.jpg",
            matches_json="[1]",
            source="abs",
        )
        saved = self.db_service.save_pending_suggestion(suggestion)

        self.assertIsNotNone(saved)
        self.assertEqual(saved.title, "Title")
        self.assertEqual(saved.status, "pending")

        fetched = self.db_service.get_suggestion("id1", source="abs")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.author, "Author")
        self.assertEqual(fetched.cover_url, "http://example/c.jpg")
        self.assertEqual(fetched.matches_json, "[1]")

    def test_save_pending_suggestion_update_mutates_existing_fields(self):
        """Second save for the same (source_id, source) updates the existing row's fields."""
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="Old", author="A1", matches_json="[1]", source="abs")
        )
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="New", author="A2", matches_json="[2]", source="abs")
        )

        fetched = self.db_service.get_suggestion("id1", source="abs")
        self.assertEqual(fetched.title, "New")
        self.assertEqual(fetched.author, "A2")
        self.assertEqual(fetched.matches_json, "[2]")

    def test_save_pending_suggestion_repeated_save_creates_no_duplicate(self):
        """Repeated saves for the same (source_id, source) keep exactly one row."""
        from src.db.models import PendingSuggestion

        for _ in range(3):
            self.db_service.save_pending_suggestion(
                self.PendingSuggestion(source_id="id1", title="Title", source="abs")
            )

        with self.db_service.db_manager.get_session() as session:
            count = (
                session.query(PendingSuggestion)
                .filter(PendingSuggestion.source_id == "id1", PendingSuggestion.source == "abs")
                .count()
            )
        self.assertEqual(count, 1)

    def test_fresh_db_enforces_suggestion_source_uniqueness(self):
        """A fresh DB created via create_all must have the unique (source_id, source) index.

        Fresh databases are created with Base.metadata.create_all + stamp head,
        skipping migrations. The unique index added by revision p6q7r8s9t0u1
        must therefore also exist in the ORM metadata, or fresh installs would
        silently allow duplicate suggestions that migrated installs reject.
        """
        import sqlite3

        conn = sqlite3.connect(self.test_db_path)
        try:
            indexes = conn.execute("PRAGMA index_list(pending_suggestions)").fetchall()
            unique_by_name = {row[1]: row[2] for row in indexes}
            self.assertIn("ix_pending_suggestions_source_id_source", unique_by_name)
            self.assertEqual(unique_by_name["ix_pending_suggestions_source_id_source"], 1,
                             "Index exists but is not UNIQUE")

            columns = [
                row[2]
                for row in conn.execute(
                    "PRAGMA index_info(ix_pending_suggestions_source_id_source)"
                ).fetchall()
            ]
            self.assertEqual(columns, ["source_id", "source"])

            conn.execute(
                "INSERT INTO pending_suggestions (source, source_id, title, status)"
                " VALUES ('abs', 'dup-id', 'First', 'pending')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pending_suggestions (source, source_id, title, status)"
                    " VALUES ('abs', 'dup-id', 'Second', 'pending')"
                )
        finally:
            conn.close()

    def test_save_pending_suggestion_hidden_stays_hidden_when_incoming_pending(self):
        """A hidden suggestion must remain hidden when re-saved as pending."""
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="Title", source="abs")
        )
        self.assertTrue(self.db_service.hide_suggestion("id1", source="abs"))

        saved = self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="Updated", status="pending", source="abs")
        )

        self.assertEqual(saved.status, "hidden")
        fetched = self.db_service.get_suggestion("id1", source="abs")
        self.assertEqual(fetched.status, "hidden")
        self.assertEqual(fetched.title, "Updated")

    def test_save_pending_suggestion_hidden_overwritten_by_incoming_non_pending(self):
        """An incoming non-pending status replaces an existing hidden status as before."""
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="Title", source="abs")
        )
        self.assertTrue(self.db_service.hide_suggestion("id1", source="abs"))

        saved = self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="Title", status="ignored", source="abs")
        )

        self.assertEqual(saved.status, "ignored")
        self.assertEqual(self.db_service.get_suggestion("id1", source="abs").status, "ignored")

    def test_save_pending_suggestion_hidden_preservation_scoped_by_source(self):
        """Hidden preservation only applies within the same source, not across sources."""
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="ABS", source="abs")
        )
        self.assertTrue(self.db_service.hide_suggestion("id1", source="abs"))

        kosync_saved = self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="KOSync", status="pending", source="kosync")
        )

        self.assertEqual(kosync_saved.status, "pending")
        self.assertEqual(self.db_service.get_suggestion("id1", source="abs").status, "hidden")

    def test_save_pending_suggestion_preserves_hidden_from_row_inside_upsert(self):
        """Regression: hidden preservation must be decided by the row found inside
        the upsert transaction, not an earlier pre-read.

        Simulates the race the old code lost: a pre-read sees no hidden row, then a
        concurrent hide lands before the update transaction runs. The pre-read is
        simulated as returning None to prove preservation no longer depends on it."""
        from unittest.mock import patch

        from src.db.suggestion_repository import SuggestionRepository

        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="race-hide", title="Title", source="abs")
        )
        self.assertTrue(self.db_service.hide_suggestion("race-hide", source="abs"))

        # Pre-read returns None: the concurrent hide had not yet landed when an
        # earlier-design caller would have read. The fix must not rely on it.
        with patch.object(SuggestionRepository, "get_suggestion", return_value=None):
            saved = self.db_service._suggestions.save_pending_suggestion(
                self.PendingSuggestion(source_id="race-hide", title="Updated", status="pending", source="abs")
            )

        self.assertEqual(saved.status, "hidden")
        fetched = self.db_service.get_suggestion("race-hide", source="abs")
        self.assertEqual(fetched.status, "hidden")
        self.assertEqual(fetched.title, "Updated")

    def test_save_pending_suggestion_preserves_hidden_in_integrity_error_recovery(self):
        """Regression: the IntegrityError race-recovery branch of _upsert must also
        preserve hidden status.

        Forces _upsert down its insert path (initial lookup returns None) and makes
        the insert flush raise IntegrityError, as a concurrent insert of the same
        (source_id, source) does under the unique index. Recovery re-finds the
        hidden row and preservation must run against it."""
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy.orm import Query, Session

        from src.db.models import PendingSuggestion

        repo = self.db_service._suggestions

        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="race-integrity", title="Existing", source="abs")
        )
        self.assertTrue(self.db_service.hide_suggestion("race-integrity", source="abs"))

        real_first = Query.first
        real_flush = Session.flush
        state = {"first_calls": 0, "flush_calls": 0}

        def fake_first(query_self):
            # Only intercept the very first lookup inside _upsert for this model.
            if (
                state["first_calls"] == 0
                and query_self.column_descriptions
                and query_self.column_descriptions[0]["type"] is PendingSuggestion
            ):
                state["first_calls"] += 1
                return None
            return real_first(query_self)

        def fake_flush(session_self, *args, **kwargs):
            # Only the insert flush fails, simulating a lost concurrent-insert race.
            state["flush_calls"] += 1
            if state["flush_calls"] == 1:
                raise IntegrityError("INSERT", {}, Exception("simulated unique violation"))
            return real_flush(session_self, *args, **kwargs)

        Query.first = fake_first
        Session.flush = fake_flush
        try:
            saved = repo.save_pending_suggestion(
                self.PendingSuggestion(source_id="race-integrity", title="Updated", status="pending", source="abs")
            )
        finally:
            Query.first = real_first
            Session.flush = real_flush

        self.assertEqual(saved.status, "hidden")
        fetched = self.db_service.get_suggestion("race-integrity", source="abs")
        self.assertEqual(fetched.status, "hidden")
        self.assertEqual(fetched.title, "Updated")

    def test_pending_suggestion_count_empty_database_returns_zero(self):
        """get_pending_suggestion_count returns 0 when no suggestions exist."""
        self.assertEqual(self.db_service.get_pending_suggestion_count(), 0)

    def test_pending_suggestion_count_counts_pending_rows(self):
        """get_pending_suggestion_count counts suggestions whose status is pending."""
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="A", source="abs")
        )
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id2", title="B", source="abs")
        )
        self.assertEqual(self.db_service.get_pending_suggestion_count(), 2)

    def test_pending_suggestion_count_excludes_non_pending_statuses(self):
        """Hidden, ignored, and dismissed suggestions are excluded from the pending count."""
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="pending", title="P", source="abs")
        )
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="hidden", title="H", source="abs")
        )
        self.assertTrue(self.db_service.hide_suggestion("hidden", source="abs"))
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="ignored", title="I", source="abs")
        )
        self.assertTrue(self.db_service.ignore_suggestion("ignored", source="abs"))
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="dismissed", title="D", source="abs", status="dismissed")
        )

        self.assertEqual(self.db_service.get_pending_suggestion_count(), 1)

    def test_pending_suggestion_count_ignores_source_when_pending(self):
        """Source value does not matter for the pending count as long as status is pending."""
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="ABS", source="abs")
        )
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="KOSync", source="kosync")
        )
        self.db_service.save_pending_suggestion(
            self.PendingSuggestion(source_id="id1", title="Storyteller", source="storyteller")
        )
        self.assertEqual(self.db_service.get_pending_suggestion_count(), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
