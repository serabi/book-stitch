"""Repository for TBR (To Be Read) list management."""

import logging

from .base_repository import BaseRepository
from .models import TbrItem

logger = logging.getLogger(__name__)

# Fields that can be passed to add/update — keeps validation in one place.
ENRICHMENT_FIELDS = (
    "description",
    "page_count",
    "rating",
    "ratings_count",
    "release_year",
    "genres",
    "subtitle",
)


class TbrRepository(BaseRepository):
    def get_tbr_items(self, source=None):
        """Get all TBR items, optionally filtered by source.

        Sorted by priority descending (up-next first), then by recency.
        """
        with self.get_session() as session:
            query = session.query(TbrItem)
            if source:
                query = query.filter(TbrItem.source == source)
            query = query.order_by(TbrItem.priority.desc(), TbrItem.added_at.desc())
            return self._query_and_expunge(session, query, one=False)

    def get_tbr_item(self, item_id):
        """Get a single TBR item by ID."""
        return self._get_one(TbrItem, TbrItem.id == item_id)

    def add_tbr_item(
        self,
        title,
        author=None,
        cover_url=None,
        notes=None,
        source="manual",
        hardcover_book_id=None,
        hardcover_slug=None,
        ol_work_key=None,
        isbn=None,
        hardcover_list_id=None,
        hardcover_list_name=None,
        book_abs_id=None,
        book_id=None,
        **enrichment,
    ):
        """Add a TBR item, deduplicating by hardcover_book_id or ol_work_key.

        Returns (item, created) tuple — created=False if duplicate found.
        Extra keyword arguments matching ENRICHMENT_FIELDS are stored on the item.
        """
        with self.get_session() as session:
            # Dedup by Hardcover book ID
            if hardcover_book_id:
                query = session.query(TbrItem).filter(TbrItem.hardcover_book_id == hardcover_book_id)
                existing = self._query_and_expunge(session, query, one=True)
                if existing:
                    return existing, False

            # Dedup by Open Library work key
            if ol_work_key:
                query = session.query(TbrItem).filter(TbrItem.ol_work_key == ol_work_key)
                existing = self._query_and_expunge(session, query, one=True)
                if existing:
                    return existing, False

            extras = {k: v for k, v in enrichment.items() if k in ENRICHMENT_FIELDS and v is not None}

            item = TbrItem(
                title=title,
                author=author,
                cover_url=cover_url,
                notes=notes,
                source=source,
                hardcover_book_id=hardcover_book_id,
                hardcover_slug=hardcover_slug,
                ol_work_key=ol_work_key,
                isbn=isbn,
                hardcover_list_id=hardcover_list_id,
                hardcover_list_name=hardcover_list_name,
                book_abs_id=book_abs_id,
                book_id=book_id,
                **extras,
            )
            session.add(item)
            session.flush()
            session.refresh(item)
            session.expunge(item)
            return item, True

    def _mutate_tbr_item(self, item_id, mutate):
        """Load a TBR item by ID, apply ``mutate`` to it, and return it detached.

        Returns None without mutating or flushing when no item matches the ID.
        Shared by update_tbr_item and link_tbr_to_book to keep the
        load -> mutate -> flush/refresh/expunge sequence in one place.
        """
        with self.get_session() as session:
            item = session.query(TbrItem).filter(TbrItem.id == item_id).first()
            if not item:
                return None
            mutate(item)
            session.flush()
            session.refresh(item)
            session.expunge(item)
            return item

    def update_tbr_item(self, item_id, **fields):
        """Update arbitrary fields on a TBR item. Returns the updated item or None."""
        ALLOWED = {
            "title",
            "author",
            "cover_url",
            "notes",
            "priority",
            "hardcover_book_id",
            "hardcover_slug",
            *ENRICHMENT_FIELDS,
        }

        def apply_fields(item):
            for key, value in fields.items():
                if key in ALLOWED:
                    setattr(item, key, value)

        return self._mutate_tbr_item(item_id, apply_fields)

    def delete_tbr_item(self, item_id):
        """Remove a TBR item. Returns True if deleted."""
        return self._delete_one(TbrItem, TbrItem.id == item_id)

    def link_tbr_to_book(self, item_id, book_id):
        """Set book_id on a TBR item (linking it to an owned book)."""

        def set_book_id(item):
            item.book_id = book_id

        return self._mutate_tbr_item(item_id, set_book_id)

    def find_tbr_by_hardcover_id(self, hc_book_id):
        """Find a TBR item by its Hardcover book ID."""
        return self._get_one(TbrItem, TbrItem.hardcover_book_id == hc_book_id)

    def get_tbr_count(self):
        """Return the total number of TBR items."""
        return self._count(TbrItem)

    def find_by_book_id(self, book_id):
        """Find a TBR item linked to a given library book."""
        return self._get_one(TbrItem, TbrItem.book_id == book_id)

    def delete_by_book_id(self, book_id):
        """Delete any TBR items linked to the given book_id. Returns count deleted."""
        with self.get_session() as session:
            count = session.query(TbrItem).filter(TbrItem.book_id == book_id).delete()
            return count

    def get_unlinked_items(self):
        """Return TBR items where book_id is NULL (not linked to a library book)."""
        with self.get_session() as session:
            query = session.query(TbrItem).filter(TbrItem.book_id.is_(None))
            return self._query_and_expunge(session, query, one=False)

    def auto_link_by_title(self, book):
        """Auto-link unlinked TBR items by normalized title match."""
        if not book.title:
            return
        try:
            unlinked = self.get_unlinked_items()
            if not unlinked:
                return
            norm_title = book.title.lower().strip()
            for item in unlinked:
                if item.title and item.title.lower().strip() == norm_title:
                    self.link_tbr_to_book(item.id, book.id)
                    logger.info(f"Auto-linked TBR item '{item.title}' to book {book.id}")
                    break
        except (AttributeError, TypeError) as e:
            logger.warning(f"TBR auto-link failed: {e}")
