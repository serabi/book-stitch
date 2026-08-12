"""Tests for Phase 1 reading tracker: models, CRUD, auto-journal on status transitions."""

import logging
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["DATA_DIR"] = "test_data"
os.environ["BOOKS_DIR"] = "test_data"

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

from src.db.models import State
from src.services.reading_stats_service import ReadingStatsService


class TestReadingTrackerModels(unittest.TestCase):
    """Test ReadingJournal, ReadingGoal models and Book reading fields."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / "test_reading.db")
        from src.db.database_service import DatabaseService

        self.db = DatabaseService(self.test_db_path)

    def tearDown(self):
        if hasattr(self, "db") and hasattr(self.db, "db_manager"):
            self.db.db_manager.close()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_book(self, abs_id="test-book-1", title="Test Book", status="active", author=None):
        from src.db.models import Book

        book = Book(abs_id=abs_id, title=title, status=status, author=author)
        return self.db.save_book(book)

    # -- Book reading fields --

    def test_book_reading_fields_default(self):
        """New books have null reading fields and read_count=1."""
        book = self._create_book()
        self.assertIsNone(book.started_at)
        self.assertIsNone(book.finished_at)
        self.assertIsNone(book.rating)
        self.assertEqual(book.read_count, 1)

    def test_book_metadata_override_defaults(self):
        """New books fall back to imported title/author when no overrides exist."""
        book = self._create_book(author="Source Author")
        self.assertIsNone(book.title_override)
        self.assertIsNone(book.author_override)
        self.assertEqual(book.display_title, "Test Book")
        self.assertEqual(book.display_author, "Source Author")

    def test_update_book_metadata_overrides(self):
        """Metadata overrides persist separately from imported metadata."""
        book = self._create_book(author="Source Author")
        updated = self.db.update_book_metadata_overrides(
            book.id,
            title_override="Clean Title",
            author_override="Clean Author",
        )
        self.assertEqual(updated.title_override, "Clean Title")
        self.assertEqual(updated.author_override, "Clean Author")
        self.assertEqual(updated.display_title, "Clean Title")
        self.assertEqual(updated.display_author, "Clean Author")

    def test_clearing_book_metadata_override_falls_back_to_source(self):
        """Clearing one override restores source metadata for that field."""
        book = self._create_book(author="Source Author")
        self.db.update_book_metadata_overrides(
            book.id,
            title_override="Clean Title",
            author_override="Clean Author",
        )

        updated = self.db.update_book_metadata_overrides(book.id, title_override=None)

        self.assertIsNone(updated.title_override)
        self.assertEqual(updated.author_override, "Clean Author")
        self.assertEqual(updated.display_title, "Test Book")
        self.assertEqual(updated.display_author, "Clean Author")

    def test_update_book_metadata_overrides_nonexistent(self):
        """Returns None for a missing book."""
        result = self.db.update_book_metadata_overrides(99999, title_override="Missing")
        self.assertIsNone(result)

    def test_save_book_source_refresh_preserves_metadata_overrides(self):
        """Source metadata refreshes should not clear PageKeeper overrides."""
        from src.db.models import Book

        book = self._create_book(title="Imported Title", author="Imported Author")
        self.db.update_book_metadata_overrides(
            book.id,
            title_override="Override Title",
            author_override="Override Author",
        )

        self.db.save_book(Book(abs_id=book.abs_id, title="Refreshed Title", author="Refreshed Author"))
        refreshed = self.db.get_book_by_abs_id(book.abs_id)

        self.assertEqual(refreshed.title, "Refreshed Title")
        self.assertEqual(refreshed.author, "Refreshed Author")
        self.assertEqual(refreshed.title_override, "Override Title")
        self.assertEqual(refreshed.author_override, "Override Author")
        self.assertEqual(refreshed.display_title, "Override Title")
        self.assertEqual(refreshed.display_author, "Override Author")

    def test_update_book_reading_fields(self):
        """update_book_reading_fields sets only reading fields."""
        book = self._create_book()
        updated = self.db.update_book_reading_fields(book.id, started_at="2026-03-01", rating=4.5)
        self.assertEqual(updated.started_at, "2026-03-01")
        self.assertEqual(updated.rating, 4.5)
        self.assertIsNone(updated.finished_at)

    def test_update_book_reading_fields_rejects_non_reading(self):
        """update_book_reading_fields ignores non-reading kwargs."""
        book = self._create_book()
        updated = self.db.update_book_reading_fields(book.id, started_at="2026-03-01", title="HACKED")
        self.assertEqual(updated.started_at, "2026-03-01")
        # title should not have changed
        refreshed = self.db.get_book_by_abs_id("test-book-1")
        self.assertEqual(refreshed.title, "Test Book")

    def test_update_book_reading_fields_all_allowed_fields(self):
        """Updates every allowed reading field in one call."""
        book = self._create_book()
        updated = self.db.update_book_reading_fields(
            book.id,
            started_at="2026-03-01",
            finished_at="2026-03-20",
            rating=4.0,
            read_count=3,
        )
        self.assertEqual(updated.started_at, "2026-03-01")
        self.assertEqual(updated.finished_at, "2026-03-20")
        self.assertEqual(updated.rating, 4.0)
        self.assertEqual(updated.read_count, 3)

    def test_update_book_reading_fields_writes_none_for_nullable_fields(self):
        """Passing None for allowed nullable fields writes the None through."""
        book = self._create_book()
        self.db.update_book_reading_fields(
            book.id,
            started_at="2026-03-01",
            finished_at="2026-03-20",
            rating=4.0,
        )

        cleared = self.db.update_book_reading_fields(
            book.id,
            started_at=None,
            finished_at=None,
            rating=None,
        )
        self.assertIsNone(cleared.started_at)
        self.assertIsNone(cleared.finished_at)
        self.assertIsNone(cleared.rating)

        refreshed = self.db.get_book_by_abs_id("test-book-1")
        self.assertIsNone(refreshed.started_at)
        self.assertIsNone(refreshed.finished_at)
        self.assertIsNone(refreshed.rating)

    def test_update_book_reading_fields_unknown_only_returns_unchanged_book(self):
        """Unknown-only kwargs return the unchanged detached book without mutating it."""
        book = self._create_book()
        self.db.update_book_reading_fields(book.id, started_at="2026-03-01", rating=4.0)

        result = self.db.update_book_reading_fields(book.id, title="HACKED", author="HACKED")
        self.assertIsNotNone(result)
        # Pre-existing reading fields untouched, ignored kwargs not applied.
        self.assertEqual(result.started_at, "2026-03-01")
        self.assertEqual(result.rating, 4.0)
        self.assertEqual(result.title, "Test Book")

        refreshed = self.db.get_book_by_abs_id("test-book-1")
        self.assertEqual(refreshed.title, "Test Book")
        self.assertEqual(refreshed.started_at, "2026-03-01")

    def test_update_book_reading_fields_detached_after_session_close(self):
        """Returned book is usable after the session has closed."""
        book = self._create_book()
        updated = self.db.update_book_reading_fields(book.id, started_at="2026-03-01", rating=4.0)
        # Accessing attributes after the session closed must not raise DetachedInstanceError.
        self.assertEqual(updated.started_at, "2026-03-01")
        self.assertEqual(updated.rating, 4.0)
        self.assertEqual(updated.id, book.id)

    def test_update_book_reading_fields_rejects_invalid_rating(self):
        """An out-of-range rating raises ValueError."""
        book = self._create_book()
        for invalid in (-0.5, 5.5):
            with self.subTest(rating=invalid):
                with self.assertRaises(ValueError):
                    self.db.update_book_reading_fields(book.id, rating=invalid)

    def test_update_book_reading_fields_rejects_invalid_read_count(self):
        """A read_count below 1 raises ValueError."""
        book = self._create_book()
        with self.assertRaises(ValueError):
            self.db.update_book_reading_fields(book.id, read_count=0)

    def test_update_book_reading_fields_validates_before_lookup(self):
        """Validation runs before the DB lookup, so invalid input raises even for a missing book."""
        with self.assertRaises(ValueError):
            self.db.update_book_reading_fields(99999, rating=9.0)
        with self.assertRaises(ValueError):
            self.db.update_book_reading_fields(99999, read_count=0)

    def test_update_book_reading_fields_nonexistent(self):
        """Returns None for a missing book."""
        result = self.db.update_book_reading_fields(99999, rating=3.0)
        self.assertIsNone(result)

    def test_save_book_does_not_overwrite_reading_fields(self):
        """save_book should not null out reading fields set separately."""
        book = self._create_book()
        self.db.update_book_reading_fields(book.id, rating=4.0, started_at="2026-01-15")

        # Re-save via save_book (simulating sync path)
        book = self.db.get_book_by_abs_id("test-book-1")
        book.status = "active"
        self.db.save_book(book)

        refreshed = self.db.get_book_by_abs_id("test-book-1")
        self.assertEqual(refreshed.rating, 4.0)
        self.assertEqual(refreshed.started_at, "2026-01-15")

    # -- ReadingJournal CRUD --

    def test_add_and_get_journals(self):
        """Create journal entries and retrieve them newest-first."""
        book = self._create_book()
        self.db.add_reading_journal(book.id, event="started")
        self.db.add_reading_journal(book.id, event="progress", percentage=0.5)
        self.db.add_reading_journal(book.id, event="note", entry="Great chapter!")

        journals = self.db.get_reading_journals(book.id)
        self.assertEqual(len(journals), 3)
        # Newest first
        self.assertEqual(journals[0].event, "note")
        self.assertEqual(journals[0].entry, "Great chapter!")
        self.assertEqual(journals[1].event, "progress")
        self.assertAlmostEqual(journals[1].percentage, 0.5)
        self.assertEqual(journals[2].event, "started")

    def test_delete_journal(self):
        """Delete a specific journal entry by ID."""
        book = self._create_book()
        j = self.db.add_reading_journal(book.id, event="note", entry="delete me")
        self.assertTrue(self.db.delete_reading_journal(j.id))
        self.assertEqual(len(self.db.get_reading_journals(book.id)), 0)

    def test_delete_journal_nonexistent(self):
        """Deleting a missing journal returns False."""
        self.assertFalse(self.db.delete_reading_journal(9999))

    def test_journals_cascade_on_book_delete(self):
        """Deleting a book cascades to its journal entries."""
        book = self._create_book()
        self.db.add_reading_journal(book.id, event="started")
        self.db.add_reading_journal(book.id, event="note", entry="cascade test")

        self.db.delete_book(book.id)
        journals = self.db.get_reading_journals(book.id)
        self.assertEqual(len(journals), 0)

    def test_journals_empty_for_unknown_book(self):
        """get_reading_journals returns empty list for unknown book_id."""
        self.assertEqual(self.db.get_reading_journals(99999), [])

    # -- get_reading_journal_entries_for_book --

    def test_journal_entries_for_book_returns_only_that_book_newest_first(self):
        """Returns all entries for the given book only, ordered newest-first."""
        book = self._create_book(abs_id="book-a", title="Book A")
        other = self._create_book(abs_id="book-b", title="Book B")
        self.db.add_reading_journal(book.id, event="started", created_at=datetime(2026, 1, 1, 0, 0, 0))
        self.db.add_reading_journal(book.id, event="note", entry="middle", created_at=datetime(2026, 1, 2, 0, 0, 0))
        self.db.add_reading_journal(book.id, event="finished", created_at=datetime(2026, 1, 3, 0, 0, 0))
        self.db.add_reading_journal(other.id, event="started", created_at=datetime(2026, 1, 2, 12, 0, 0))

        entries = self.db.get_reading_journal_entries_for_book(book.id)
        self.assertEqual(len(entries), 3)
        self.assertEqual([e.event for e in entries], ["finished", "note", "started"])

    def test_journal_entries_for_book_filters_by_event(self):
        """Passing an event restricts results to that event type."""
        book = self._create_book()
        self.db.add_reading_journal(book.id, event="highlight", entry="h1", created_at=datetime(2026, 1, 1, 0, 0, 0))
        self.db.add_reading_journal(book.id, event="note", entry="n1", created_at=datetime(2026, 1, 2, 0, 0, 0))
        self.db.add_reading_journal(book.id, event="highlight", entry="h2", created_at=datetime(2026, 1, 3, 0, 0, 0))

        highlights = self.db.get_reading_journal_entries_for_book(book.id, "highlight")
        self.assertEqual([e.entry for e in highlights], ["h2", "h1"])

    def test_journal_entries_for_book_no_event_filter_when_falsy(self):
        """A falsy event (None or empty string) applies no event filter."""
        book = self._create_book()
        self.db.add_reading_journal(book.id, event="started", created_at=datetime(2026, 1, 1, 0, 0, 0))
        self.db.add_reading_journal(book.id, event="note", entry="n1", created_at=datetime(2026, 1, 2, 0, 0, 0))

        self.assertEqual(len(self.db.get_reading_journal_entries_for_book(book.id, None)), 2)
        self.assertEqual(len(self.db.get_reading_journal_entries_for_book(book.id, "")), 2)

    def test_journal_entries_for_book_empty_for_no_matches(self):
        """Returns an empty list when nothing matches."""
        book = self._create_book()
        self.assertEqual(self.db.get_reading_journal_entries_for_book(book.id), [])
        self.assertEqual(self.db.get_reading_journal_entries_for_book(99999), [])

    def test_journal_entries_for_book_detached_after_session_close(self):
        """Returned rows are usable after the session has closed."""
        book = self._create_book()
        self.db.add_reading_journal(book.id, event="note", entry="detached", created_at=datetime(2026, 1, 1, 0, 0, 0))

        entries = self.db.get_reading_journal_entries_for_book(book.id)
        # Accessing attributes after the session closed must not raise DetachedInstanceError.
        self.assertEqual(entries[0].entry, "detached")
        self.assertEqual(entries[0].book_id, book.id)

    # -- find_journal_by_event --

    def test_find_journal_by_event_returns_newest_match(self):
        """Returns the most recent journal of the requested event for the book."""
        book = self._create_book()
        self.db.add_reading_journal(book.id, event="progress", percentage=0.2, created_at=datetime(2026, 1, 1, 0, 0, 0))
        self.db.add_reading_journal(book.id, event="progress", percentage=0.8, created_at=datetime(2026, 1, 5, 0, 0, 0))
        self.db.add_reading_journal(book.id, event="progress", percentage=0.5, created_at=datetime(2026, 1, 3, 0, 0, 0))

        journal = self.db.find_journal_by_event(book.id, "progress")
        self.assertIsNotNone(journal)
        self.assertAlmostEqual(journal.percentage, 0.8)

    def test_find_journal_by_event_ignores_other_books_and_events(self):
        """Only matches the given book and event."""
        book = self._create_book(abs_id="book-a", title="Book A")
        other = self._create_book(abs_id="book-b", title="Book B")
        self.db.add_reading_journal(book.id, event="started", created_at=datetime(2026, 1, 1, 0, 0, 0))
        self.db.add_reading_journal(other.id, event="finished", created_at=datetime(2026, 1, 2, 0, 0, 0))

        self.assertIsNone(self.db.find_journal_by_event(book.id, "finished"))
        self.assertIsNotNone(self.db.find_journal_by_event(book.id, "started"))

    def test_find_journal_by_event_none_when_no_match(self):
        """Returns None when no journal matches."""
        book = self._create_book()
        self.assertIsNone(self.db.find_journal_by_event(book.id, "finished"))

    def test_find_journal_by_event_detached_after_session_close(self):
        """Returned object is usable after the session has closed."""
        book = self._create_book()
        self.db.add_reading_journal(book.id, event="note", entry="detached", created_at=datetime(2026, 1, 1, 0, 0, 0))

        journal = self.db.find_journal_by_event(book.id, "note")
        self.assertEqual(journal.entry, "detached")
        self.assertEqual(journal.book_id, book.id)

    # -- update_reading_journal --

    def test_update_reading_journal_updates_entry_only(self):
        """Passing only entry updates the entry and leaves created_at intact."""
        book = self._create_book()
        created = datetime(2026, 1, 1, 0, 0, 0)
        j = self.db.add_reading_journal(book.id, event="note", entry="old", created_at=created)

        updated = self.db.update_reading_journal(j.id, entry="new")
        self.assertEqual(updated.entry, "new")
        self.assertEqual(updated.created_at, created)

    def test_update_reading_journal_updates_created_at_only(self):
        """Passing only created_at updates the timestamp and leaves entry intact."""
        book = self._create_book()
        j = self.db.add_reading_journal(
            book.id, event="note", entry="keep", created_at=datetime(2026, 1, 1, 0, 0, 0)
        )

        new_dt = datetime(2026, 2, 2, 12, 30, 0)
        updated = self.db.update_reading_journal(j.id, created_at=new_dt)
        self.assertEqual(updated.entry, "keep")
        self.assertEqual(updated.created_at, new_dt)

    def test_update_reading_journal_updates_both(self):
        """Passing both entry and created_at updates both fields."""
        book = self._create_book()
        j = self.db.add_reading_journal(
            book.id, event="note", entry="old", created_at=datetime(2026, 1, 1, 0, 0, 0)
        )

        new_dt = datetime(2026, 3, 3, 9, 0, 0)
        updated = self.db.update_reading_journal(j.id, entry="new", created_at=new_dt)
        self.assertEqual(updated.entry, "new")
        self.assertEqual(updated.created_at, new_dt)

    def test_update_reading_journal_none_does_not_clear_fields(self):
        """Passing None for entry/created_at leaves the existing values intact."""
        book = self._create_book()
        created = datetime(2026, 1, 1, 0, 0, 0)
        j = self.db.add_reading_journal(book.id, event="note", entry="keep", created_at=created)

        updated = self.db.update_reading_journal(j.id, entry=None, created_at=None)
        self.assertEqual(updated.entry, "keep")
        self.assertEqual(updated.created_at, created)

    def test_update_reading_journal_no_kwargs_returns_unchanged(self):
        """Calling with no update kwargs returns the unchanged journal."""
        book = self._create_book()
        created = datetime(2026, 1, 1, 0, 0, 0)
        j = self.db.add_reading_journal(book.id, event="note", entry="keep", created_at=created)

        updated = self.db.update_reading_journal(j.id)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.entry, "keep")
        self.assertEqual(updated.created_at, created)

    def test_update_reading_journal_nonexistent(self):
        """Returns None for a missing journal."""
        self.assertIsNone(self.db.update_reading_journal(99999, entry="x"))

    def test_update_reading_journal_detached_after_session_close(self):
        """Returned journal is usable after the session has closed."""
        book = self._create_book()
        j = self.db.add_reading_journal(
            book.id, event="note", entry="old", created_at=datetime(2026, 1, 1, 0, 0, 0)
        )

        updated = self.db.update_reading_journal(j.id, entry="detached")
        # Accessing attributes after the session closed must not raise DetachedInstanceError.
        self.assertEqual(updated.entry, "detached")
        self.assertEqual(updated.book_id, book.id)

    # -- ReadingGoal CRUD --

    def test_save_and_get_goal(self):
        """Create and retrieve a reading goal."""
        goal = self.db.save_reading_goal(2026, 24)
        self.assertEqual(goal.year, 2026)
        self.assertEqual(goal.target_books, 24)

        retrieved = self.db.get_reading_goal(2026)
        self.assertEqual(retrieved.target_books, 24)

    def test_update_goal(self):
        """Updating an existing goal overwrites target_books."""
        self.db.save_reading_goal(2026, 24)
        updated = self.db.save_reading_goal(2026, 50)
        self.assertEqual(updated.target_books, 50)

        retrieved = self.db.get_reading_goal(2026)
        self.assertEqual(retrieved.target_books, 50)

    def test_get_goal_nonexistent(self):
        """Returns None for a year with no goal."""
        self.assertIsNone(self.db.get_reading_goal(1999))

    def test_save_goal_no_duplicate_row_on_repeated_save(self):
        """Repeated saves for the same year update in place, never inserting a duplicate."""
        from src.db.models import ReadingGoal

        self.db.save_reading_goal(2026, 24)
        self.db.save_reading_goal(2026, 50)
        self.db.save_reading_goal(2026, 7)

        with self.db._reading.get_session() as session:
            rows = session.query(ReadingGoal).filter(ReadingGoal.year == 2026).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].target_books, 7)

    def test_save_goal_inserts_then_updates(self):
        """First save inserts, second save for the same year updates the same row."""
        inserted = self.db.save_reading_goal(2026, 24)
        self.assertEqual(inserted.target_books, 24)

        updated = self.db.save_reading_goal(2026, 30)
        self.assertEqual(updated.target_books, 30)
        self.assertEqual(updated.year, 2026)
        self.assertEqual(self.db.get_reading_goal(2026).target_books, 30)

    def test_save_goal_allows_zero(self):
        """A target of zero is a valid non-negative goal."""
        goal = self.db.save_reading_goal(2026, 0)
        self.assertEqual(goal.target_books, 0)

    def test_save_goal_rejects_invalid_targets(self):
        """Invalid target_books values raise ValueError without persisting a goal."""
        for invalid in (None, True, False, 3.5, "12", -1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.db.save_reading_goal(2030, invalid)
        self.assertIsNone(self.db.get_reading_goal(2030))

    # -- Reading stats --

    def test_reading_stats(self):
        """get_reading_stats counts finished books and active books."""
        from src.db.models import Book

        # Create books with various statuses
        b1 = self.db.save_book(Book(abs_id="b1", title="Finished 1", status="completed"))
        self.db.update_book_reading_fields(b1.id, finished_at="2026-06-01", rating=4.5)

        b2 = self.db.save_book(Book(abs_id="b2", title="Finished 2", status="completed"))
        self.db.update_book_reading_fields(b2.id, finished_at="2026-11-15", rating=3.5)

        b3 = self.db.save_book(Book(abs_id="b3", title="Still Reading", status="active"))
        self.db.save_state(State(abs_id="b3", book_id=b3.id, client_name="manual", percentage=0.45))
        self.db.save_book(Book(abs_id="b4", title="Paused", status="paused"))
        b5 = self.db.save_book(Book(abs_id="b5", title="Last Year", status="completed"))
        self.db.update_book_reading_fields(b5.id, finished_at="2025-12-31")
        b6 = self.db.save_book(Book(abs_id="b6", title="DNF but dated", status="dnf"))
        self.db.update_book_reading_fields(b6.id, finished_at="2026-04-21", rating=1.5)

        self.db.save_reading_goal(2026, 12)

        stats = ReadingStatsService(self.db).get_year_stats(2026)
        self.assertEqual(stats["books_finished"], 2)  # b1, b2 (finished in 2026)
        self.assertEqual(stats["currently_reading"], 1)  # b3
        self.assertEqual(stats["total_tracked"], 6)  # b1-b6 (all have reading statuses)
        self.assertEqual(stats["goal_target"], 12)
        self.assertEqual(stats["goal_completed"], 2)
        self.assertEqual(stats["monthly_finished"][5], 1)  # June
        self.assertEqual(stats["monthly_finished"][10], 1)  # November
        self.assertAlmostEqual(stats["average_rating"], 4.0)
        self.assertAlmostEqual(stats["goal_percent"], 16.7)

    def test_reading_stats_no_goal(self):
        """Stats work fine without a goal set."""
        stats = ReadingStatsService(self.db).get_year_stats(2026)
        self.assertEqual(stats["books_finished"], 0)
        self.assertIsNone(stats["goal_target"])
        self.assertEqual(stats["monthly_finished"], [0] * 12)
        self.assertIsNone(stats["average_rating"])

    # -- Journal migration --

    def test_migrate_book_data_includes_journals(self):
        """migrate_book_data moves journal entries to the new book ID."""
        from src.db.models import Book

        old_book = self.db.save_book(Book(abs_id="old-id", title="Old Book", status="active"))
        self.db.add_reading_journal(old_book.id, event="started")
        self.db.add_reading_journal(old_book.id, event="note", entry="migrate me")

        new_book = self.db.save_book(Book(abs_id="new-id", title="New Book", status="active"))
        self.db.migrate_book_data("old-id", "new-id")

        # After migration, old book's journals move to the merged book (now with abs_id='new-id')
        # The old book_id still has the journals since migrate_book_data updates abs_id but keeps book_id
        migrated_book = self.db.get_book_by_abs_id("new-id")
        old_journals = self.db.get_reading_journals(new_book.id)
        new_journals = self.db.get_reading_journals(migrated_book.id)
        self.assertEqual(len(old_journals), 0)
        self.assertEqual(len(new_journals), 2)


if __name__ == "__main__":
    unittest.main()
