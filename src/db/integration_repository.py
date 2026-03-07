"""Repository for integration entities: Hardcover, Booklore, BookFusion."""

import logging
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .base_repository import BaseRepository
from .models import (
    BookfusionBook,
    BookfusionHighlight,
    BookloreBook,
    HardcoverDetails,
)

logger = logging.getLogger(__name__)


class IntegrationRepository(BaseRepository):

    # ── Hardcover ──

    def get_hardcover_details(self, abs_id):
        return self._get_one(HardcoverDetails, HardcoverDetails.abs_id == abs_id)

    def save_hardcover_details(self, details):
        return self._upsert(
            HardcoverDetails,
            [HardcoverDetails.abs_id == details.abs_id],
            details,
            ['hardcover_book_id', 'hardcover_slug', 'hardcover_edition_id',
             'hardcover_pages', 'hardcover_audio_seconds', 'isbn', 'asin', 'matched_by',
             'hardcover_cover_url'],
        )

    def delete_hardcover_details(self, abs_id):
        return self._delete_one(HardcoverDetails, HardcoverDetails.abs_id == abs_id)

    def get_all_hardcover_details(self):
        return self._get_all(HardcoverDetails)

    # ── Booklore ──

    def get_booklore_book(self, filename):
        return self._get_one(BookloreBook, BookloreBook.filename == filename)

    def get_all_booklore_books(self):
        return self._get_all(BookloreBook)

    def save_booklore_book(self, booklore_book):
        with self.get_session() as session:
            existing = session.query(BookloreBook).filter(BookloreBook.filename == booklore_book.filename).first()

            if existing:
                for attr in ['title', 'authors', 'raw_metadata']:
                    if hasattr(booklore_book, attr):
                        setattr(existing, attr, getattr(booklore_book, attr))
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                try:
                    session.add(booklore_book)
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    existing = session.query(BookloreBook).filter(BookloreBook.filename == booklore_book.filename).first()
                    if existing:
                        for attr in ['title', 'authors', 'raw_metadata']:
                            if hasattr(booklore_book, attr):
                                setattr(existing, attr, getattr(booklore_book, attr))
                        session.flush()
                        session.refresh(existing)
                        session.expunge(existing)
                        return existing
                    raise
                session.refresh(booklore_book)
                session.expunge(booklore_book)
                return booklore_book

    def delete_booklore_book(self, filename):
        try:
            with self.get_session() as session:
                deleted = session.query(BookloreBook).filter(BookloreBook.filename == filename).delete(synchronize_session=False)
                return deleted > 0
        except SQLAlchemyError as e:
            logger.error(f"Failed to delete Booklore book '{filename}': {e}")
            return False

    # ── BookFusion Highlights ──

    def save_bookfusion_highlights(self, highlights):
        saved = 0
        with self.get_session() as session:
            all_ids = [h['highlight_id'] for h in highlights if h.get('highlight_id')]
            existing_rows = session.query(BookfusionHighlight).filter(
                BookfusionHighlight.highlight_id.in_(all_ids)
            ).all() if all_ids else []
            lookup = {row.highlight_id: row for row in existing_rows}

            seen_in_batch = set()
            for h in highlights:
                highlight_id = h.get('highlight_id')
                if not highlight_id or highlight_id in seen_in_batch:
                    continue
                seen_in_batch.add(highlight_id)
                existing = lookup.get(highlight_id)
                if existing:
                    existing.content = h.get('content', '')
                    existing.chapter_heading = h.get('chapter_heading')
                    existing.book_title = h.get('book_title')
                    existing.highlighted_at = h.get('highlighted_at')
                    existing.quote_text = h.get('quote_text')
                else:
                    session.add(BookfusionHighlight(
                        bookfusion_book_id=h.get('bookfusion_book_id'),
                        highlight_id=highlight_id,
                        content=h.get('content', ''),
                        book_title=h.get('book_title'),
                        chapter_heading=h.get('chapter_heading'),
                        highlighted_at=h.get('highlighted_at'),
                        quote_text=h.get('quote_text'),
                    ))
                    saved += 1
        return saved

    def get_bookfusion_highlights(self):
        with self.get_session() as session:
            highlights = session.query(BookfusionHighlight).order_by(
                BookfusionHighlight.book_title, BookfusionHighlight.id
            ).all()
            session.expunge_all()
            return highlights

    def get_unmatched_bookfusion_highlights(self):
        with self.get_session() as session:
            highlights = session.query(BookfusionHighlight).filter(
                BookfusionHighlight.matched_abs_id.is_(None)
            ).order_by(BookfusionHighlight.book_title, BookfusionHighlight.id).all()
            session.expunge_all()
            return highlights

    def link_bookfusion_highlight(self, highlight_id, abs_id):
        with self.get_session() as session:
            hl = session.query(BookfusionHighlight).filter(
                BookfusionHighlight.id == highlight_id
            ).first()
            if hl:
                hl.matched_abs_id = abs_id
                return True
            return False

    def link_bookfusion_book(self, bookfusion_book_id, abs_id):
        with self.get_session() as session:
            session.query(BookfusionHighlight).filter(
                BookfusionHighlight.bookfusion_book_id == bookfusion_book_id
            ).update({BookfusionHighlight.matched_abs_id: abs_id}, synchronize_session=False)

    def get_bookfusion_highlights_for_book(self, abs_id):
        with self.get_session() as session:
            highlights = session.query(BookfusionHighlight).filter(
                BookfusionHighlight.matched_abs_id == abs_id
            ).order_by(
                BookfusionHighlight.highlighted_at.desc().nullslast(),
                BookfusionHighlight.id
            ).all()
            session.expunge_all()
            return highlights

    # ── BookFusion Books (Library Catalog) ──

    def save_bookfusion_books(self, books):
        saved = 0
        with self.get_session() as session:
            for b in books:
                bookfusion_id = b.get('bookfusion_id')
                if not bookfusion_id:
                    continue
                existing = session.query(BookfusionBook).filter(
                    BookfusionBook.bookfusion_id == bookfusion_id
                ).first()
                title = b.get('title') or ''
                if title.endswith('.md'):
                    title = title[:-3].strip()

                if existing:
                    existing.title = title or existing.title
                    existing.authors = b.get('authors') or existing.authors
                    existing.filename = b.get('filename') or existing.filename
                    existing.frontmatter = b.get('frontmatter') or existing.frontmatter
                    existing.tags = b.get('tags') or existing.tags
                    existing.series = b.get('series') or existing.series
                    existing.highlight_count = b.get('highlight_count', existing.highlight_count)
                    existing.last_updated = datetime.now(UTC)
                else:
                    session.add(BookfusionBook(
                        bookfusion_id=bookfusion_id,
                        title=title,
                        authors=b.get('authors'),
                        filename=b.get('filename'),
                        frontmatter=b.get('frontmatter'),
                        tags=b.get('tags'),
                        series=b.get('series'),
                        highlight_count=b.get('highlight_count', 0),
                        last_updated=datetime.now(UTC),
                    ))
                    saved += 1
        return saved

    def get_bookfusion_books(self):
        return self._get_all(BookfusionBook, order_by=BookfusionBook.title)

    def is_bookfusion_linked(self, abs_id):
        with self.get_session() as session:
            return session.query(BookfusionBook).filter(
                BookfusionBook.matched_abs_id == abs_id
            ).first() is not None

    def set_bookfusion_book_match(self, bookfusion_id, abs_id):
        with self.get_session() as session:
            book = session.query(BookfusionBook).filter(
                BookfusionBook.bookfusion_id == bookfusion_id
            ).first()
            if book:
                book.matched_abs_id = abs_id

    def set_bookfusion_books_hidden(self, bookfusion_ids, hidden):
        with self.get_session() as session:
            session.query(BookfusionBook).filter(
                BookfusionBook.bookfusion_id.in_(bookfusion_ids)
            ).update({BookfusionBook.hidden: hidden}, synchronize_session=False)

    def get_bookfusion_book(self, bookfusion_id):
        return self._get_one(BookfusionBook, BookfusionBook.bookfusion_id == bookfusion_id)

    def get_bookfusion_book_by_abs_id(self, abs_id):
        return self._get_one(BookfusionBook, BookfusionBook.matched_abs_id == abs_id)

    def unlink_bookfusion_by_abs_id(self, abs_id):
        with self.get_session() as session:
            session.query(BookfusionBook).filter(
                BookfusionBook.matched_abs_id == abs_id
            ).update({BookfusionBook.matched_abs_id: None}, synchronize_session=False)
            session.query(BookfusionHighlight).filter(
                BookfusionHighlight.matched_abs_id == abs_id
            ).update({BookfusionHighlight.matched_abs_id: None}, synchronize_session=False)

    def get_bookfusion_highlight_date_range(self, bookfusion_book_ids):
        with self.get_session() as session:
            result = session.query(
                func.min(BookfusionHighlight.highlighted_at),
                func.max(BookfusionHighlight.highlighted_at),
                func.count(BookfusionHighlight.id),
            ).filter(
                BookfusionHighlight.bookfusion_book_id.in_(bookfusion_book_ids),
                BookfusionHighlight.highlighted_at.isnot(None),
            ).first()
            if result and result[2] > 0:
                return result
            return None

    def get_bookfusion_linked_abs_ids(self):
        with self.get_session() as session:
            book_ids = {
                r[0] for r in session.query(BookfusionBook.matched_abs_id).filter(
                    BookfusionBook.matched_abs_id.isnot(None)
                ).all()
            }
            highlight_ids = {
                r[0] for r in session.query(BookfusionHighlight.matched_abs_id).filter(
                    BookfusionHighlight.matched_abs_id.isnot(None)
                ).distinct().all()
            }
            return book_ids | highlight_ids

    def get_bookfusion_highlight_counts(self):
        with self.get_session() as session:
            rows = session.query(
                BookfusionHighlight.matched_abs_id,
                func.count(BookfusionHighlight.id)
            ).filter(
                BookfusionHighlight.matched_abs_id.isnot(None)
            ).group_by(BookfusionHighlight.matched_abs_id).all()
            return {abs_id: count for abs_id, count in rows}
