"""Repository for BookFusion integration: highlights and library catalog."""

import logging
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .base_repository import BaseRepository
from .models import BookfusionBook, BookfusionHighlight

logger = logging.getLogger(__name__)


class BookFusionRepository(BaseRepository):
    # ── BookFusion Highlights ──

    def save_bookfusion_highlights(self, highlights):
        saved = 0
        new_ids = []
        with self.get_session() as session:
            all_ids = [h["highlight_id"] for h in highlights if h.get("highlight_id")]
            existing_rows = (
                session.query(BookfusionHighlight).filter(BookfusionHighlight.highlight_id.in_(all_ids)).all()
                if all_ids
                else []
            )
            lookup = {row.highlight_id: row for row in existing_rows}

            seen_in_batch = set()
            for h in highlights:
                highlight_id = h.get("highlight_id")
                if not highlight_id or highlight_id in seen_in_batch:
                    continue
                seen_in_batch.add(highlight_id)
                existing = lookup.get(highlight_id)
                if existing:
                    existing.content = h.get("content", "")
                    existing.chapter_heading = h.get("chapter_heading")
                    existing.book_title = h.get("book_title")
                    existing.highlighted_at = h.get("highlighted_at")
                    existing.quote_text = h.get("quote_text")
                else:
                    try:
                        nested = session.begin_nested()
                        session.add(
                            BookfusionHighlight(
                                bookfusion_book_id=h.get("bookfusion_book_id"),
                                highlight_id=highlight_id,
                                content=h.get("content", ""),
                                book_title=h.get("book_title"),
                                chapter_heading=h.get("chapter_heading"),
                                highlighted_at=h.get("highlighted_at"),
                                quote_text=h.get("quote_text"),
                            )
                        )
                        session.flush()
                        saved += 1
                        new_ids.append(highlight_id)
                    except IntegrityError:
                        nested.rollback()
                        logger.warning("Duplicate BookFusion highlight %s, updating instead", highlight_id)
                        existing = (
                            session.query(BookfusionHighlight)
                            .filter(BookfusionHighlight.highlight_id == highlight_id)
                            .first()
                        )
                        if existing:
                            existing.content = h.get("content", "")
                            existing.chapter_heading = h.get("chapter_heading")
                            existing.book_title = h.get("book_title")
                            existing.highlighted_at = h.get("highlighted_at")
                            existing.quote_text = h.get("quote_text")
                            session.flush()
        return {"saved": saved, "new_ids": new_ids}

    def get_bookfusion_highlights(self):
        with self.get_session() as session:
            query = session.query(BookfusionHighlight).order_by(
                BookfusionHighlight.book_title, BookfusionHighlight.id
            )
            return self._query_and_expunge(session, query, one=False)

    def get_unmatched_bookfusion_highlights(self):
        with self.get_session() as session:
            query = (
                session.query(BookfusionHighlight)
                .filter(BookfusionHighlight.matched_book_id.is_(None))
                .order_by(BookfusionHighlight.book_title, BookfusionHighlight.id)
            )
            return self._query_and_expunge(session, query, one=False)

    def link_bookfusion_highlights_by_book_id(self, bookfusion_book_id, book_id):
        """Link all highlights for a BookFusion book to a library book by book_id."""
        normalized_id = bookfusion_book_id
        if normalized_id.startswith("book-"):
            normalized_id = normalized_id[5:]
        with self.get_session() as session:
            session.query(BookfusionHighlight).filter(
                (BookfusionHighlight.bookfusion_book_id == bookfusion_book_id)
                | (BookfusionHighlight.bookfusion_book_id == normalized_id)
                | (BookfusionHighlight.bookfusion_book_id == f"book-{normalized_id}")
            ).update({BookfusionHighlight.matched_book_id: book_id}, synchronize_session=False)

    def get_bookfusion_highlights_for_book_by_book_id(self, book_id):
        """Get highlights matched to a book by book_id."""
        with self.get_session() as session:
            query = (
                session.query(BookfusionHighlight)
                .filter(BookfusionHighlight.matched_book_id == book_id)
                .order_by(BookfusionHighlight.highlighted_at.desc().nullslast(), BookfusionHighlight.id)
            )
            return self._query_and_expunge(session, query, one=False)

    # ── BookFusion Books (Library Catalog) ──

    def save_bookfusion_books(self, books):
        saved = 0
        with self.get_session() as session:
            for b in books:
                bookfusion_id = b.get("bookfusion_id")
                if not bookfusion_id:
                    continue
                existing = session.query(BookfusionBook).filter(BookfusionBook.bookfusion_id == bookfusion_id).first()
                title = b.get("title") or ""
                if title.endswith(".md"):
                    title = title[:-3].strip()

                if existing:
                    existing.title = title or existing.title
                    existing.authors = b.get("authors") or existing.authors
                    existing.filename = b.get("filename") or existing.filename
                    existing.frontmatter = b.get("frontmatter") or existing.frontmatter
                    existing.tags = b.get("tags") or existing.tags
                    existing.series = b.get("series") or existing.series
                    existing.highlight_count = b.get("highlight_count", existing.highlight_count)
                    existing.last_updated = datetime.now(UTC)
                else:
                    session.add(
                        BookfusionBook(
                            bookfusion_id=bookfusion_id,
                            title=title,
                            authors=b.get("authors"),
                            filename=b.get("filename"),
                            frontmatter=b.get("frontmatter"),
                            tags=b.get("tags"),
                            series=b.get("series"),
                            highlight_count=b.get("highlight_count", 0),
                        )
                    )
                    saved += 1
        return saved

    def get_bookfusion_books(self):
        return self._get_all(BookfusionBook, order_by=BookfusionBook.title)

    def is_bookfusion_linked_by_book_id(self, book_id):
        """Check if a book has a linked BookFusion catalog entry by book_id."""
        with self.get_session() as session:
            return session.query(BookfusionBook).filter(BookfusionBook.matched_book_id == book_id).first() is not None

    def set_bookfusion_book_match_by_book_id(self, bookfusion_id, book_id):
        """Match a BookFusion catalog book to a library book by book_id."""
        with self.get_session() as session:
            bf_book = session.query(BookfusionBook).filter(BookfusionBook.bookfusion_id == bookfusion_id).first()
            if bf_book:
                bf_book.matched_book_id = book_id

    def set_bookfusion_books_hidden(self, bookfusion_ids, hidden):
        with self.get_session() as session:
            session.query(BookfusionBook).filter(BookfusionBook.bookfusion_id.in_(bookfusion_ids)).update(
                {BookfusionBook.hidden: hidden}, synchronize_session=False
            )

    def get_bookfusion_book(self, bookfusion_id):
        return self._get_one(BookfusionBook, BookfusionBook.bookfusion_id == bookfusion_id)

    def get_bookfusion_book_by_book_id(self, book_id):
        return self._get_one(BookfusionBook, BookfusionBook.matched_book_id == book_id)

    def get_bookfusion_books_by_book_id(self, book_id):
        """Get all BookFusion catalog entries linked to a specific PageKeeper book."""
        with self.get_session() as session:
            query = session.query(BookfusionBook).filter(BookfusionBook.matched_book_id == book_id)
            return self._query_and_expunge(session, query, one=False)

    def save_bookfusion_book(self, bookfusion_book):
        """Save a single BookFusion catalog entry."""
        return self._save_new(bookfusion_book)

    def unlink_bookfusion_by_book_id(self, book_id):
        with self.get_session() as session:
            session.query(BookfusionBook).filter(BookfusionBook.matched_book_id == book_id).update(
                {BookfusionBook.matched_book_id: None}, synchronize_session=False
            )
            session.query(BookfusionHighlight).filter(BookfusionHighlight.matched_book_id == book_id).update(
                {BookfusionHighlight.matched_book_id: None}, synchronize_session=False
            )

    def get_bookfusion_highlight_date_range(self, bookfusion_book_ids):
        with self.get_session() as session:
            result = (
                session.query(
                    func.min(BookfusionHighlight.highlighted_at),
                    func.max(BookfusionHighlight.highlighted_at),
                    func.count(BookfusionHighlight.id),
                )
                .filter(
                    BookfusionHighlight.bookfusion_book_id.in_(bookfusion_book_ids),
                    BookfusionHighlight.highlighted_at.isnot(None),
                )
                .first()
            )
            if result and result[2] > 0:
                return result
            return None

    def get_bookfusion_linked_book_ids(self):
        with self.get_session() as session:
            book_ids = {
                r[0]
                for r in session.query(BookfusionBook.matched_book_id)
                .filter(BookfusionBook.matched_book_id.isnot(None))
                .all()
            }
            highlight_ids = {
                r[0]
                for r in session.query(BookfusionHighlight.matched_book_id)
                .filter(BookfusionHighlight.matched_book_id.isnot(None))
                .distinct()
                .all()
            }
            return book_ids | highlight_ids

    def get_bookfusion_highlight_counts_by_book_id(self):
        """Return highlight counts keyed by book_id."""
        with self.get_session() as session:
            rows = (
                session.query(BookfusionHighlight.matched_book_id, func.count(BookfusionHighlight.id))
                .filter(BookfusionHighlight.matched_book_id.isnot(None))
                .group_by(BookfusionHighlight.matched_book_id)
                .all()
            )
            return {book_id: count for book_id, count in rows}

    def auto_link_by_title(self, book):
        """Auto-link unmatched BookFusion highlights to a book by title similarity."""
        if not book.title:
            return
        try:
            import difflib

            from src.utils.title_utils import clean_book_title, normalize_title

            unmatched = self.get_unmatched_bookfusion_highlights()
            if not unmatched:
                return
            norm_book = normalize_title(book.title)
            for hl in unmatched:
                bf_title = clean_book_title(hl.book_title or "")
                norm_bf = normalize_title(bf_title)
                if norm_bf == norm_book or difflib.SequenceMatcher(None, norm_bf, norm_book).ratio() > 0.85:
                    if hl.bookfusion_book_id:
                        self.link_bookfusion_highlights_by_book_id(hl.bookfusion_book_id, book.id)
                        logger.info(f"Auto-linked BookFusion highlights for '{bf_title}' to book {book.id}")
        except (AttributeError, TypeError) as e:
            logger.warning(f"BookFusion auto-link failed: {e}")
