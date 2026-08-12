"""Integration tests for TBR repository — real SQLite in /tmp."""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["DATA_DIR"] = "test_data"
os.environ["BOOKS_DIR"] = "test_data"


class TestTbrRepository(unittest.TestCase):
    """Tests TbrRepository via DatabaseService against a real temp SQLite DB."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = str(Path(self.temp_dir) / "test_tbr.db")
        from src.db.database_service import DatabaseService

        self.db = DatabaseService(self.test_db_path)

    def tearDown(self):
        if hasattr(self, "db") and hasattr(self.db, "db_manager"):
            self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -- CRUD basics --

    def test_add_and_get_item(self):
        """Add a TBR item and retrieve it by ID."""
        item, created = self.db.add_tbr_item("Dune", author="Frank Herbert")
        self.assertTrue(created)
        self.assertIsNotNone(item.id)
        self.assertEqual(item.title, "Dune")
        self.assertEqual(item.author, "Frank Herbert")

        retrieved = self.db.get_tbr_item(item.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.title, "Dune")

    def test_get_all_items(self):
        """get_tbr_items returns all items."""
        self.db.add_tbr_item("Book A")
        self.db.add_tbr_item("Book B")
        self.db.add_tbr_item("Book C")

        items = self.db.get_tbr_items()
        self.assertEqual(len(items), 3)

    def test_delete_item(self):
        """delete_tbr_item removes the item and returns True."""
        item, _ = self.db.add_tbr_item("Temp Book")
        self.assertTrue(self.db.delete_tbr_item(item.id))
        self.assertIsNone(self.db.get_tbr_item(item.id))

    def test_delete_nonexistent(self):
        """delete_tbr_item returns False for missing ID."""
        self.assertFalse(self.db.delete_tbr_item(9999))

    def test_count(self):
        """get_tbr_count returns the total number of items."""
        self.assertEqual(self.db.get_tbr_count(), 0)
        self.db.add_tbr_item("Book A")
        self.db.add_tbr_item("Book B")
        self.assertEqual(self.db.get_tbr_count(), 2)

    def test_get_item_nonexistent(self):
        """get_tbr_item returns None for missing ID."""
        self.assertIsNone(self.db.get_tbr_item(9999))

    # -- Atomic dedup --

    def test_dedup_by_hardcover_book_id(self):
        """Same hardcover_book_id returns existing item with created=False."""
        item1, created1 = self.db.add_tbr_item("Dune", hardcover_book_id=42)
        item2, created2 = self.db.add_tbr_item("Dune (dup)", hardcover_book_id=42)

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(item1.id, item2.id)
        self.assertEqual(item2.title, "Dune")  # original title preserved

    def test_dedup_by_ol_work_key(self):
        """Same ol_work_key returns existing item with created=False."""
        item1, created1 = self.db.add_tbr_item("Neuromancer", ol_work_key="/works/OL123")
        item2, created2 = self.db.add_tbr_item("Neuromancer dup", ol_work_key="/works/OL123")

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(item1.id, item2.id)

    def test_different_keys_both_create(self):
        """Different dedup keys create separate items."""
        item1, created1 = self.db.add_tbr_item("Book A", hardcover_book_id=1)
        item2, created2 = self.db.add_tbr_item("Book B", hardcover_book_id=2)

        self.assertTrue(created1)
        self.assertTrue(created2)
        self.assertNotEqual(item1.id, item2.id)

    def test_no_dedup_without_keys(self):
        """Manual items without dedup keys always create new entries."""
        item1, created1 = self.db.add_tbr_item("My Book")
        item2, created2 = self.db.add_tbr_item("My Book")

        self.assertTrue(created1)
        self.assertTrue(created2)
        self.assertNotEqual(item1.id, item2.id)

    def test_dedup_by_hardcover_id_returns_detached_existing(self):
        """A Hardcover-ID duplicate returns a detached existing row usable after the session closes."""
        self.db.add_tbr_item("Dune", author="Frank Herbert", hardcover_book_id=42)
        existing, created = self.db.add_tbr_item("Dune (dup)", hardcover_book_id=42)

        self.assertFalse(created)
        # Accessing attributes must not raise DetachedInstanceError.
        self.assertEqual(existing.title, "Dune")
        self.assertEqual(existing.author, "Frank Herbert")
        self.assertEqual(existing.hardcover_book_id, 42)

    def test_dedup_by_ol_work_key_returns_detached_existing(self):
        """An Open Library duplicate returns a detached existing row usable after the session closes."""
        self.db.add_tbr_item("Neuromancer", author="William Gibson", ol_work_key="/works/OL123")
        existing, created = self.db.add_tbr_item("Neuromancer dup", ol_work_key="/works/OL123")

        self.assertFalse(created)
        # Accessing attributes must not raise DetachedInstanceError.
        self.assertEqual(existing.title, "Neuromancer")
        self.assertEqual(existing.author, "William Gibson")
        self.assertEqual(existing.ol_work_key, "/works/OL123")

    def test_dedup_hardcover_wins_over_ol_work_key(self):
        """When both keys match different existing rows, the Hardcover match wins."""
        hc_row, _ = self.db.add_tbr_item("By Hardcover", hardcover_book_id=42)
        ol_row, _ = self.db.add_tbr_item("By Open Library", ol_work_key="/works/OL123")
        self.assertNotEqual(hc_row.id, ol_row.id)

        existing, created = self.db.add_tbr_item(
            "Matches Both", hardcover_book_id=42, ol_work_key="/works/OL123"
        )

        self.assertFalse(created)
        self.assertEqual(existing.id, hc_row.id)
        self.assertEqual(existing.title, "By Hardcover")

    def test_falsey_hardcover_id_does_not_trigger_dedup(self):
        """hardcover_book_id=0 is falsey, so it never triggers a duplicate lookup."""
        item1, created1 = self.db.add_tbr_item("Zero One", hardcover_book_id=0)
        item2, created2 = self.db.add_tbr_item("Zero Two", hardcover_book_id=0)

        self.assertTrue(created1)
        self.assertTrue(created2)
        self.assertNotEqual(item1.id, item2.id)

    def test_falsey_ol_work_key_does_not_trigger_dedup(self):
        """ol_work_key="" is falsey, so it never triggers a duplicate lookup."""
        item1, created1 = self.db.add_tbr_item("Empty One", ol_work_key="")
        item2, created2 = self.db.add_tbr_item("Empty Two", ol_work_key="")

        self.assertTrue(created1)
        self.assertTrue(created2)
        self.assertNotEqual(item1.id, item2.id)

    # -- Linking --

    def test_link_tbr_to_book(self):
        """link_tbr_to_book sets book_id on the item."""
        from src.db.models import Book

        book = self.db.save_book(Book(abs_id="abs-123", title="Owned Copy", status="active"))

        item, _ = self.db.add_tbr_item("Dune")
        self.assertIsNone(item.book_id)

        linked = self.db.link_tbr_to_book(item.id, book.id)
        self.assertIsNotNone(linked)
        self.assertEqual(linked.book_id, book.id)

        # Verify persisted
        refreshed = self.db.get_tbr_item(item.id)
        self.assertEqual(refreshed.book_id, book.id)

    def test_link_tbr_not_found(self):
        """link_tbr_to_book returns None for missing item ID."""
        result = self.db.link_tbr_to_book(9999, 1)
        self.assertIsNone(result)

    def test_link_tbr_to_book_unlink_with_none(self):
        """link_tbr_to_book accepts None to unlink an item from its book."""
        from src.db.models import Book

        book = self.db.save_book(Book(abs_id="abs-unlink", title="Owned", status="active"))
        item, _ = self.db.add_tbr_item("Dune")
        self.db.link_tbr_to_book(item.id, book.id)

        unlinked = self.db.link_tbr_to_book(item.id, None)
        self.assertIsNotNone(unlinked)
        self.assertIsNone(unlinked.book_id)

        refreshed = self.db.get_tbr_item(item.id)
        self.assertIsNone(refreshed.book_id)

    def test_link_tbr_to_book_detached_after_session(self):
        """link_tbr_to_book returns a detached item usable after the session closes."""
        from src.db.models import Book

        book = self.db.save_book(Book(abs_id="abs-detach", title="Owned", status="active"))
        item, _ = self.db.add_tbr_item("Detached Link", author="Author Y")

        linked = self.db.link_tbr_to_book(item.id, book.id)
        # Accessing attributes must not raise DetachedInstanceError.
        self.assertEqual(linked.book_id, book.id)
        self.assertEqual(linked.title, "Detached Link")
        self.assertEqual(linked.author, "Author Y")

    # -- Updating --

    def test_update_tbr_item_regular_fields(self):
        """update_tbr_item updates allowed regular fields."""
        item, _ = self.db.add_tbr_item("Original", author="Old Author")
        updated = self.db.update_tbr_item(
            item.id,
            title="New Title",
            author="New Author",
            cover_url="http://example.com/cover.jpg",
            priority=7,
            hardcover_book_id=123,
            hardcover_slug="new-title",
        )
        self.assertEqual(updated.title, "New Title")
        self.assertEqual(updated.author, "New Author")
        self.assertEqual(updated.cover_url, "http://example.com/cover.jpg")
        self.assertEqual(updated.priority, 7)
        self.assertEqual(updated.hardcover_book_id, 123)
        self.assertEqual(updated.hardcover_slug, "new-title")

    def test_update_tbr_item_enrichment_fields(self):
        """update_tbr_item updates enrichment fields."""
        item, _ = self.db.add_tbr_item("Enrich Me")
        updated = self.db.update_tbr_item(
            item.id,
            description="A great book",
            page_count=321,
            rating=4.5,
            ratings_count=1000,
            release_year=1965,
            genres='["sci-fi"]',
            subtitle="A Subtitle",
        )
        self.assertEqual(updated.description, "A great book")
        self.assertEqual(updated.page_count, 321)
        self.assertEqual(updated.rating, 4.5)
        self.assertEqual(updated.ratings_count, 1000)
        self.assertEqual(updated.release_year, 1965)
        self.assertEqual(updated.genres, '["sci-fi"]')
        self.assertEqual(updated.subtitle, "A Subtitle")

    def test_update_tbr_item_ignores_unknown_fields(self):
        """update_tbr_item silently ignores fields not in the allowlist."""
        item, _ = self.db.add_tbr_item("Keep Me", author="Real Author")
        updated = self.db.update_tbr_item(
            item.id,
            title="Updated Title",
            bogus_field="should be ignored",
            source="manipulated",
        )
        self.assertEqual(updated.title, "Updated Title")
        # source is not in the allowlist, so it stays unchanged.
        self.assertEqual(updated.source, "manual")
        self.assertFalse(hasattr(updated, "bogus_field"))

    def test_update_tbr_item_writes_none_for_nullable_field(self):
        """update_tbr_item writes None through for an allowed nullable field."""
        item, _ = self.db.add_tbr_item("Has Notes", notes="some notes", cover_url="http://x/y.jpg")
        updated = self.db.update_tbr_item(item.id, notes=None, cover_url=None)
        self.assertIsNone(updated.notes)
        self.assertIsNone(updated.cover_url)

        refreshed = self.db.get_tbr_item(item.id)
        self.assertIsNone(refreshed.notes)
        self.assertIsNone(refreshed.cover_url)

    def test_update_tbr_item_only_unknown_fields_returns_unchanged(self):
        """update_tbr_item returns the unchanged item when only unknown fields are passed."""
        item, _ = self.db.add_tbr_item("Untouched", author="Author Z", notes="keep")
        updated = self.db.update_tbr_item(item.id, not_a_field=1, source="hack")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.title, "Untouched")
        self.assertEqual(updated.author, "Author Z")
        self.assertEqual(updated.notes, "keep")
        self.assertEqual(updated.source, "manual")

    def test_update_tbr_item_not_found(self):
        """update_tbr_item returns None for a missing item ID."""
        self.assertIsNone(self.db.update_tbr_item(9999, title="Nope"))

    def test_update_tbr_item_detached_after_session(self):
        """update_tbr_item returns a detached item usable after the session closes."""
        item, _ = self.db.add_tbr_item("Detached Update")
        updated = self.db.update_tbr_item(item.id, title="Detached New", author="Author D")
        # Accessing attributes must not raise DetachedInstanceError.
        self.assertEqual(updated.title, "Detached New")
        self.assertEqual(updated.author, "Author D")

    # -- Lookup --

    def test_find_tbr_by_hardcover_id(self):
        """find_tbr_by_hardcover_id returns the matching item."""
        self.db.add_tbr_item("Dune", hardcover_book_id=42)
        found = self.db.find_tbr_by_hardcover_id(42)
        self.assertIsNotNone(found)
        self.assertEqual(found.title, "Dune")

    def test_find_tbr_by_hardcover_id_not_found(self):
        """find_tbr_by_hardcover_id returns None for unknown ID."""
        result = self.db.find_tbr_by_hardcover_id(9999)
        self.assertIsNone(result)

    # -- Ordering --

    def test_items_ordered_newest_first(self):
        """get_tbr_items returns items newest-first by added_at."""
        import time

        self.db.add_tbr_item("Oldest")
        time.sleep(0.05)
        self.db.add_tbr_item("Middle")
        time.sleep(0.05)
        self.db.add_tbr_item("Newest")

        items = self.db.get_tbr_items()
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].title, "Newest")
        self.assertEqual(items[1].title, "Middle")
        self.assertEqual(items[2].title, "Oldest")

    def test_items_ordered_priority_first(self):
        """get_tbr_items sorts by priority descending before added_at."""
        self.db.add_tbr_item("Low Priority")
        high, _ = self.db.add_tbr_item("High Priority")
        self.db.update_tbr_item(high.id, priority=5)

        items = self.db.get_tbr_items()
        self.assertEqual(items[0].title, "High Priority")
        self.assertEqual(items[1].title, "Low Priority")

    def test_items_priority_tiebreak_by_added_at(self):
        """Within the same priority, newest added_at comes first."""
        import time

        a, _ = self.db.add_tbr_item("Older Same Priority")
        time.sleep(0.05)
        b, _ = self.db.add_tbr_item("Newer Same Priority")
        self.db.update_tbr_item(a.id, priority=3)
        self.db.update_tbr_item(b.id, priority=3)

        items = self.db.get_tbr_items()
        self.assertEqual(items[0].title, "Newer Same Priority")
        self.assertEqual(items[1].title, "Older Same Priority")

    def test_get_tbr_items_empty(self):
        """get_tbr_items returns an empty list when no items exist."""
        self.assertEqual(self.db.get_tbr_items(), [])

    def test_get_tbr_items_detached_after_session(self):
        """Returned items are detached and usable after the session closes."""
        self.db.add_tbr_item("Detached Book", author="Some Author")
        items = self.db.get_tbr_items()
        # Accessing attributes must not raise DetachedInstanceError.
        self.assertEqual(items[0].title, "Detached Book")
        self.assertEqual(items[0].author, "Some Author")

    # -- Unlinked items --

    def test_get_unlinked_items_returns_only_null_book_id(self):
        """get_unlinked_items returns only items where book_id is None."""
        from src.db.models import Book

        book = self.db.save_book(Book(abs_id="abs-u1", title="Owned", status="active"))

        linked, _ = self.db.add_tbr_item("Linked Book")
        self.db.link_tbr_to_book(linked.id, book.id)
        self.db.add_tbr_item("Unlinked Book")

        unlinked = self.db.get_unlinked_tbr_items()
        self.assertEqual(len(unlinked), 1)
        self.assertEqual(unlinked[0].title, "Unlinked Book")
        self.assertIsNone(unlinked[0].book_id)

    def test_get_unlinked_items_empty_when_all_linked(self):
        """get_unlinked_items returns an empty list when every item is linked."""
        from src.db.models import Book

        book = self.db.save_book(Book(abs_id="abs-u2", title="Owned", status="active"))
        item, _ = self.db.add_tbr_item("Linked Book")
        self.db.link_tbr_to_book(item.id, book.id)

        self.assertEqual(self.db.get_unlinked_tbr_items(), [])

    def test_get_unlinked_items_detached_after_session(self):
        """Unlinked items are detached and usable after the session closes."""
        self.db.add_tbr_item("Unlinked Detached", author="Author X")
        unlinked = self.db.get_unlinked_tbr_items()
        self.assertEqual(unlinked[0].title, "Unlinked Detached")
        self.assertEqual(unlinked[0].author, "Author X")

    # -- Auto-link via save_hardcover_details --

    def test_save_hardcover_details_auto_links_tbr(self):
        """save_hardcover_details auto-links a TBR item with matching hardcover_book_id."""
        from src.db.models import Book, HardcoverDetails

        # Create a book in the library
        book = self.db.save_book(Book(abs_id="abs-1", title="Dune", status="active"))

        # Create a TBR item with hardcover_book_id=42 (not yet linked)
        item, created = self.db.add_tbr_item("Dune", hardcover_book_id=42)
        self.assertTrue(created)
        self.assertIsNone(item.book_id)

        # Save HardcoverDetails linking abs-1 to hardcover_book_id=42
        hc = HardcoverDetails(abs_id="abs-1", book_id=book.id, hardcover_book_id="42")
        self.db.save_hardcover_details(hc)

        # Verify TBR item is now linked
        refreshed = self.db.get_tbr_item(item.id)
        self.assertEqual(refreshed.book_id, book.id)

    def test_save_hardcover_details_no_tbr_match(self):
        """save_hardcover_details with no matching TBR item is a no-op."""
        from src.db.models import Book, HardcoverDetails

        book = self.db.save_book(Book(abs_id="abs-1", title="Dune", status="active"))
        hc = HardcoverDetails(abs_id="abs-1", book_id=book.id, hardcover_book_id="99")
        # Should not raise — no TBR item with hardcover_book_id=99
        self.db.save_hardcover_details(hc)

    def test_save_hardcover_details_skips_already_linked(self):
        """save_hardcover_details does not overwrite an already-linked TBR item."""
        from src.db.models import Book, HardcoverDetails

        book1 = self.db.save_book(Book(abs_id="abs-1", title="Dune", status="active"))
        book2 = self.db.save_book(Book(abs_id="abs-2", title="Dune Messiah", status="active"))

        item, _ = self.db.add_tbr_item("Dune", hardcover_book_id=42)
        self.db.link_tbr_to_book(item.id, book2.id)  # pre-linked to book2

        hc = HardcoverDetails(abs_id="abs-1", book_id=book1.id, hardcover_book_id="42")
        self.db.save_hardcover_details(hc)

        # Should still be linked to book2, not overwritten to book1
        refreshed = self.db.get_tbr_item(item.id)
        self.assertEqual(refreshed.book_id, book2.id)

    # -- Source filtering --

    def test_filter_by_source(self):
        """get_tbr_items with source filter returns only matching items."""
        self.db.add_tbr_item("Manual Book", source="manual")
        self.db.add_tbr_item("HC Book", source="hardcover_wtr")
        self.db.add_tbr_item("OL Book", source="open_library")

        manual_items = self.db.get_tbr_items(source="manual")
        self.assertEqual(len(manual_items), 1)
        self.assertEqual(manual_items[0].title, "Manual Book")

        hc_items = self.db.get_tbr_items(source="hardcover_wtr")
        self.assertEqual(len(hc_items), 1)


if __name__ == "__main__":
    unittest.main()
