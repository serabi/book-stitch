"""Tests for Phase 1 reading tracker: models, CRUD, auto-journal on status transitions."""

import logging
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ['DATA_DIR'] = 'test_data'
os.environ['BOOKS_DIR'] = 'test_data'

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')


class TestReadingTrackerModels(unittest.TestCase):
    """Test ReadingJournal, ReadingGoal models and Book reading fields."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / 'test_reading.db')
        from src.db.database_service import DatabaseService
        self.db = DatabaseService(self.test_db_path)

    def tearDown(self):
        if hasattr(self, 'db') and hasattr(self.db, 'db_manager'):
            self.db.db_manager.close()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_book(self, abs_id='test-book-1', title='Test Book', status='active'):
        from src.db.models import Book
        book = Book(abs_id=abs_id, abs_title=title, status=status)
        return self.db.save_book(book)

    # -- Book reading fields --

    def test_book_reading_fields_default(self):
        """New books have null reading fields and read_count=1."""
        book = self._create_book()
        self.assertIsNone(book.started_at)
        self.assertIsNone(book.finished_at)
        self.assertIsNone(book.rating)
        self.assertEqual(book.read_count, 1)

    def test_update_book_reading_fields(self):
        """update_book_reading_fields sets only reading fields."""
        self._create_book()
        updated = self.db.update_book_reading_fields(
            'test-book-1', started_at='2026-03-01', rating=4.5
        )
        self.assertEqual(updated.started_at, '2026-03-01')
        self.assertEqual(updated.rating, 4.5)
        self.assertIsNone(updated.finished_at)

    def test_update_book_reading_fields_rejects_non_reading(self):
        """update_book_reading_fields ignores non-reading kwargs."""
        self._create_book()
        updated = self.db.update_book_reading_fields(
            'test-book-1', started_at='2026-03-01', abs_title='HACKED'
        )
        self.assertEqual(updated.started_at, '2026-03-01')
        # abs_title should not have changed
        book = self.db.get_book('test-book-1')
        self.assertEqual(book.abs_title, 'Test Book')

    def test_update_book_reading_fields_nonexistent(self):
        """Returns None for a missing book."""
        result = self.db.update_book_reading_fields('no-such-book', rating=3.0)
        self.assertIsNone(result)

    def test_save_book_does_not_overwrite_reading_fields(self):
        """save_book should not null out reading fields set separately."""
        book = self._create_book()
        self.db.update_book_reading_fields('test-book-1', rating=4.0, started_at='2026-01-15')

        # Re-save via save_book (simulating sync path)
        book = self.db.get_book('test-book-1')
        book.status = 'active'
        self.db.save_book(book)

        refreshed = self.db.get_book('test-book-1')
        self.assertEqual(refreshed.rating, 4.0)
        self.assertEqual(refreshed.started_at, '2026-01-15')

    # -- ReadingJournal CRUD --

    def test_add_and_get_journals(self):
        """Create journal entries and retrieve them newest-first."""
        self._create_book()
        self.db.add_reading_journal('test-book-1', event='started')
        self.db.add_reading_journal('test-book-1', event='progress', percentage=0.5)
        self.db.add_reading_journal('test-book-1', event='note', entry='Great chapter!')

        journals = self.db.get_reading_journals('test-book-1')
        self.assertEqual(len(journals), 3)
        # Newest first
        self.assertEqual(journals[0].event, 'note')
        self.assertEqual(journals[0].entry, 'Great chapter!')
        self.assertEqual(journals[1].event, 'progress')
        self.assertAlmostEqual(journals[1].percentage, 0.5)
        self.assertEqual(journals[2].event, 'started')

    def test_delete_journal(self):
        """Delete a specific journal entry by ID."""
        self._create_book()
        j = self.db.add_reading_journal('test-book-1', event='note', entry='delete me')
        self.assertTrue(self.db.delete_reading_journal(j.id))
        self.assertEqual(len(self.db.get_reading_journals('test-book-1')), 0)

    def test_delete_journal_nonexistent(self):
        """Deleting a missing journal returns False."""
        self.assertFalse(self.db.delete_reading_journal(9999))

    def test_journals_cascade_on_book_delete(self):
        """Deleting a book cascades to its journal entries."""
        self._create_book()
        self.db.add_reading_journal('test-book-1', event='started')
        self.db.add_reading_journal('test-book-1', event='note', entry='cascade test')

        self.db.delete_book('test-book-1')
        journals = self.db.get_reading_journals('test-book-1')
        self.assertEqual(len(journals), 0)

    def test_journals_empty_for_unknown_book(self):
        """get_reading_journals returns empty list for unknown abs_id."""
        self.assertEqual(self.db.get_reading_journals('no-such-book'), [])

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

    # -- Reading stats --

    def test_reading_stats(self):
        """get_reading_stats counts finished books and active books."""
        from src.db.models import Book
        # Create books with various statuses
        self.db.save_book(Book(abs_id='b1', abs_title='Finished 1', status='completed'))
        self.db.update_book_reading_fields('b1', finished_at='2026-06-01')

        self.db.save_book(Book(abs_id='b2', abs_title='Finished 2', status='completed'))
        self.db.update_book_reading_fields('b2', finished_at='2026-11-15')

        self.db.save_book(Book(abs_id='b3', abs_title='Still Reading', status='active'))
        self.db.save_book(Book(abs_id='b4', abs_title='Paused', status='paused'))
        self.db.save_book(Book(abs_id='b5', abs_title='Last Year', status='completed'))
        self.db.update_book_reading_fields('b5', finished_at='2025-12-31')

        self.db.save_reading_goal(2026, 12)

        stats = self.db.get_reading_stats(2026)
        self.assertEqual(stats['books_finished'], 2)   # b1, b2 (finished in 2026)
        self.assertEqual(stats['currently_reading'], 1)  # b3
        self.assertEqual(stats['total_tracked'], 5)      # b1-b5 (all have reading statuses)
        self.assertEqual(stats['goal_target'], 12)

    def test_reading_stats_no_goal(self):
        """Stats work fine without a goal set."""
        stats = self.db.get_reading_stats(2026)
        self.assertEqual(stats['books_finished'], 0)
        self.assertIsNone(stats['goal_target'])

    # -- Journal migration --

    def test_migrate_book_data_includes_journals(self):
        """migrate_book_data moves journal entries to the new book ID."""
        from src.db.models import Book
        self.db.save_book(Book(abs_id='old-id', abs_title='Old Book', status='active'))
        self.db.add_reading_journal('old-id', event='started')
        self.db.add_reading_journal('old-id', event='note', entry='migrate me')

        self.db.save_book(Book(abs_id='new-id', abs_title='New Book', status='active'))
        self.db.migrate_book_data('old-id', 'new-id')

        old_journals = self.db.get_reading_journals('old-id')
        new_journals = self.db.get_reading_journals('new-id')
        self.assertEqual(len(old_journals), 0)
        self.assertEqual(len(new_journals), 2)


if __name__ == '__main__':
    unittest.main()
