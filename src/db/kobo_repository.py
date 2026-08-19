"""Repository for the Kobo integration: device books and bookmarks."""

import logging
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .base_repository import BaseRepository
from .models import KoboBook, KoboBookmark

logger = logging.getLogger(__name__)


class KoboRepository(BaseRepository):
    # ── Kobo Books (device library snapshot) ──

    def save_kobo_books(self, books):
        """Upsert device-book snapshots dicts (from KoboService parsing)."""
        saved = 0
        updated = 0
        with self.get_session() as session:
            for b in books:
                content_id = b.get("content_id")
                if not content_id:
                    continue
                existing = session.query(KoboBook).filter(KoboBook.content_id == content_id).first()
                if existing:
                    existing.title = b.get("title") or existing.title
                    existing.author = b.get("author") or existing.author
                    existing.isbn = b.get("isbn") or existing.isbn
                    existing.percent = b.get("percent", existing.percent)
                    existing.read_status = b.get("read_status", existing.read_status)
                    existing.date_last_read = b.get("date_last_read") or existing.date_last_read
                    existing.time_spent_seconds = b.get("time_spent_seconds", existing.time_spent_seconds)
                    existing.last_updated = datetime.now(UTC)
                    updated += 1
                else:
                    session.add(
                        KoboBook(
                            content_id=content_id,
                            title=b.get("title"),
                            author=b.get("author"),
                            isbn=b.get("isbn"),
                            percent=b.get("percent", 0),
                            read_status=b.get("read_status", 0),
                            date_last_read=b.get("date_last_read"),
                            time_spent_seconds=b.get("time_spent_seconds", 0),
                        )
                    )
                    saved += 1
        return {"saved": saved, "updated": updated}

    def get_kobo_books(self, include_hidden=False):
        with self.get_session() as session:
            query = session.query(KoboBook).order_by(KoboBook.title)
            if not include_hidden:
                query = query.filter(KoboBook.hidden.is_(False))
            return self._query_and_expunge(session, query)

    def get_kobo_book(self, content_id):
        return self._get_one(KoboBook, KoboBook.content_id == content_id)

    def get_kobo_books_by_matched_book_id(self, book_id):
        with self.get_session() as session:
            query = session.query(KoboBook).filter(KoboBook.matched_book_id == book_id)
            return self._query_and_expunge(session, query)

    def set_kobo_book_match(self, content_id, book_id):
        with self.get_session() as session:
            kobo_book = session.query(KoboBook).filter(KoboBook.content_id == content_id).first()
            if kobo_book:
                kobo_book.matched_book_id = book_id

    def unlink_kobo_book(self, content_id):
        with self.get_session() as session:
            session.query(KoboBook).filter(KoboBook.content_id == content_id).update(
                {KoboBook.matched_book_id: None}, synchronize_session=False
            )
            session.query(KoboBookmark).filter(KoboBookmark.content_id == content_id).update(
                {KoboBookmark.matched_book_id: None}, synchronize_session=False
            )

    def set_kobo_books_hidden(self, content_ids, hidden):
        with self.get_session() as session:
            session.query(KoboBook).filter(KoboBook.content_id.in_(content_ids)).update(
                {KoboBook.hidden: hidden}, synchronize_session=False
            )

    def get_kobo_linked_book_ids(self):
        with self.get_session() as session:
            return {
                r[0]
                for r in session.query(KoboBook.matched_book_id)
                .filter(KoboBook.matched_book_id.isnot(None))
                .all()
            }

    @staticmethod
    def _naive(dt):
        """SQLAlchemy's SQLite DateTime round-trips drop tzinfo; compare naive."""
        return dt.replace(tzinfo=None) if dt else None

    def save_kobo_open_events(self, first_open_by_content_id):
        """Store earliest first-open timestamps (dict content_id -> datetime).

        Earliest wins across all ingested copies and across re-ingests, so a
        book's true start survives copies made after long reading sessions.
        """
        saved = 0
        with self.get_session() as session:
            for content_id, occurred_at in first_open_by_content_id.items():
                book = session.query(KoboBook).filter(KoboBook.content_id == content_id).first()
                if not book or not occurred_at:
                    continue
                if book.first_opened_at is None or self._naive(occurred_at) < self._naive(book.first_opened_at):
                    book.first_opened_at = occurred_at
                    saved += 1
        return {"saved": saved}

    # ── Kobo Bookmarks (highlights/notes) ──

    def save_kobo_bookmarks(self, bookmarks):
        """Insert new snapshots; existing bookmark_ids update text/annotation."""
        saved = 0
        new_ids = []
        with self.get_session() as session:
            all_ids = [b["bookmark_id"] for b in bookmarks if b.get("bookmark_id")]
            existing_rows = (
                session.query(KoboBookmark).filter(KoboBookmark.bookmark_id.in_(all_ids)).all() if all_ids else []
            )
            lookup = {row.bookmark_id: row for row in existing_rows}

            seen_in_batch = set()
            for b in bookmarks:
                bookmark_id = b.get("bookmark_id")
                if not bookmark_id or bookmark_id in seen_in_batch:
                    continue
                seen_in_batch.add(bookmark_id)
                existing = lookup.get(bookmark_id)
                if existing:
                    existing.text = b.get("text")
                    existing.annotation = b.get("annotation")
                    existing.kind = b.get("kind", existing.kind)
                    existing.chapter_progress = b.get("chapter_progress", existing.chapter_progress)
                    existing.highlighted_at = b.get("highlighted_at", existing.highlighted_at)
                    continue
                try:
                    nested = session.begin_nested()
                    session.add(
                        KoboBookmark(
                            bookmark_id=bookmark_id,
                            content_id=b.get("content_id", ""),
                            kind=b.get("kind", "highlight"),
                            text=b.get("text"),
                            annotation=b.get("annotation"),
                            chapter_progress=b.get("chapter_progress"),
                            highlighted_at=b.get("highlighted_at"),
                        )
                    )
                    session.flush()
                    saved += 1
                    new_ids.append(bookmark_id)
                except IntegrityError:
                    nested.rollback()
                    logger.warning("Duplicate Kobo bookmark %s, skipping", bookmark_id)
        return {"saved": saved, "new_ids": new_ids}

    def get_kobo_bookmark_counts_by_content_id(self):
        """Return bookmark counts keyed by content_id (avoids per-book queries)."""
        with self.get_session() as session:
            rows = (
                session.query(KoboBookmark.content_id, func.count(KoboBookmark.id))
                .group_by(KoboBookmark.content_id)
                .all()
            )
            return {content_id: count for content_id, count in rows}

    def get_kobo_bookmarks(self):
        with self.get_session() as session:
            query = session.query(KoboBookmark).order_by(KoboBookmark.highlighted_at.desc().nullslast())
            return self._query_and_expunge(session, query)

    def get_kobo_bookmarks_for_content_id(self, content_id):
        with self.get_session() as session:
            query = (
                session.query(KoboBookmark)
                .filter(KoboBookmark.content_id == content_id)
                .order_by(KoboBookmark.highlighted_at)
            )
            return self._query_and_expunge(session, query)

    def get_kobo_bookmarks_for_book_by_book_id(self, book_id):
        with self.get_session() as session:
            query = (
                session.query(KoboBookmark)
                .filter(KoboBookmark.matched_book_id == book_id)
                .order_by(KoboBookmark.highlighted_at)
            )
            return self._query_and_expunge(session, query)

    def link_kobo_bookmarks_by_content_id(self, content_id, book_id):
        """Link a Kobo book's bookmarks to a library book."""
        with self.get_session() as session:
            session.query(KoboBookmark).filter(KoboBookmark.content_id == content_id).update(
                {KoboBookmark.matched_book_id: book_id}, synchronize_session=False
            )
