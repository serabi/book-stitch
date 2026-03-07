"""Repository for reading tracker: journals, goals, and reading fields."""

import re

from sqlalchemy import extract

from .base_repository import BaseRepository
from .models import Book, BookfusionHighlight, ReadingGoal, ReadingJournal

VALID_JOURNAL_EVENTS = {'started', 'progress', 'finished', 'paused', 'dnf', 'resumed', 'note', 'highlight'}
BOOKFUSION_IMPORT_PREFIX = "📖 "
BOOKFUSION_ENTRY_SPLIT = "\n— "
BOOKFUSION_CHAPTER_PREFIX = re.compile(r'^#{1,6}\s*')


class ReadingRepository(BaseRepository):

    def update_book_reading_fields(self, abs_id, **kwargs):
        """Update reading-specific fields on a book (started_at, finished_at, rating, read_count)."""
        allowed = {'started_at', 'finished_at', 'rating', 'read_count'}
        rating = kwargs.get('rating')
        if rating is not None and not (0.0 <= rating <= 5.0):
            raise ValueError("rating must be between 0 and 5")
        read_count = kwargs.get('read_count')
        if read_count is not None and read_count < 1:
            raise ValueError("read_count must be >= 1")
        with self.get_session() as session:
            book = session.query(Book).filter(Book.abs_id == abs_id).first()
            if not book:
                return None
            for key, value in kwargs.items():
                if key in allowed:
                    setattr(book, key, value)
            session.flush()
            session.refresh(book)
            session.expunge(book)
            return book

    def get_reading_journals(self, abs_id):
        return self._get_all(
            ReadingJournal,
            ReadingJournal.abs_id == abs_id,
            order_by=ReadingJournal.created_at.desc(),
        )

    def get_reading_journal(self, journal_id):
        return self._get_one(ReadingJournal, ReadingJournal.id == journal_id)

    def add_reading_journal(self, abs_id, event, entry=None, percentage=None, created_at=None):
        if event not in VALID_JOURNAL_EVENTS:
            raise ValueError(f"event must be one of {VALID_JOURNAL_EVENTS}")
        if percentage is not None and not (0.0 <= percentage <= 1.0):
            raise ValueError("percentage must be between 0.0 and 1.0")
        return self._save_new(
            ReadingJournal(abs_id=abs_id, event=event, entry=entry, percentage=percentage, created_at=created_at)
        )

    def update_reading_journal(self, journal_id, *, entry=None):
        with self.get_session() as session:
            journal = session.query(ReadingJournal).filter(ReadingJournal.id == journal_id).first()
            if not journal:
                return None
            if entry is not None:
                journal.entry = entry
            session.flush()
            session.refresh(journal)
            session.expunge(journal)
            return journal

    def cleanup_bookfusion_import_notes(self, abs_id=None):
        """Strip the legacy emoji prefix and backfill timestamps when a cached highlight matches."""
        def _normalize_entry(entry):
            if not entry:
                return ('', '')
            text = entry
            if text.startswith(BOOKFUSION_IMPORT_PREFIX):
                text = text[len(BOOKFUSION_IMPORT_PREFIX):]
            elif text.startswith('📖'):
                text = text[1:].lstrip()

            quote, chapter = text, ''
            if BOOKFUSION_ENTRY_SPLIT in text:
                quote, chapter = text.split(BOOKFUSION_ENTRY_SPLIT, 1)
            return (quote.strip(), chapter.strip())

        def _normalize_highlight(quote, chapter):
            return (
                (quote or '').strip(),
                BOOKFUSION_CHAPTER_PREFIX.sub('', (chapter or '').strip()),
            )

        with self.get_session() as session:
            journal_query = session.query(ReadingJournal).filter(
                ReadingJournal.event.in_(('note', 'highlight')),
                ReadingJournal.entry.is_not(None),
                (ReadingJournal.entry.startswith(BOOKFUSION_IMPORT_PREFIX) | ReadingJournal.entry.startswith('📖')),
            )
            if abs_id:
                journal_query = journal_query.filter(ReadingJournal.abs_id == abs_id)
            journals = journal_query.order_by(ReadingJournal.id.asc()).all()
            if not journals:
                return {'entries_cleaned': 0, 'timestamps_backfilled': 0}

            highlight_query = session.query(BookfusionHighlight).filter(
                BookfusionHighlight.matched_abs_id.is_not(None)
            )
            if abs_id:
                highlight_query = highlight_query.filter(BookfusionHighlight.matched_abs_id == abs_id)
            highlights = highlight_query.all()

            highlight_map = {}
            for hl in highlights:
                key = (
                    hl.matched_abs_id,
                    *_normalize_highlight(hl.quote_text or hl.content, hl.chapter_heading),
                )
                if not key[1]:
                    continue
                if hl.highlighted_at is not None:
                    highlight_map.setdefault(key, []).append(hl.highlighted_at)

            for dates in highlight_map.values():
                dates.sort(reverse=True)

            entries_cleaned = 0
            timestamps_backfilled = 0
            for journal in journals:
                quote, chapter = _normalize_entry(journal.entry)
                cleaned_entry = quote
                if chapter:
                    cleaned_entry += f"{BOOKFUSION_ENTRY_SPLIT}{chapter}"

                if journal.entry != cleaned_entry:
                    journal.entry = cleaned_entry
                    entries_cleaned += 1

                key = (journal.abs_id, quote, chapter)
                if quote and key in highlight_map and highlight_map[key]:
                    highlighted_at = highlight_map[key].pop(0)
                    if journal.event != 'highlight':
                        journal.event = 'highlight'
                    if highlighted_at and journal.created_at != highlighted_at:
                        journal.created_at = highlighted_at
                        timestamps_backfilled += 1

            return {
                'entries_cleaned': entries_cleaned,
                'timestamps_backfilled': timestamps_backfilled,
            }

    def delete_reading_journal(self, journal_id):
        return self._delete_one(ReadingJournal, ReadingJournal.id == journal_id)

    def get_reading_goal(self, year):
        return self._get_one(ReadingGoal, ReadingGoal.year == year)

    def save_reading_goal(self, year, target_books):
        if target_books is None or isinstance(target_books, bool) or not isinstance(target_books, int):
            raise ValueError("target_books must be a non-negative integer")
        if target_books < 0:
            raise ValueError("target_books must be a non-negative integer")
        with self.get_session() as session:
            existing = session.query(ReadingGoal).filter(ReadingGoal.year == year).first()
            if existing:
                existing.target_books = target_books
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                goal = ReadingGoal(year=year, target_books=target_books)
                session.add(goal)
                session.flush()
                session.refresh(goal)
                session.expunge(goal)
                return goal

    def get_reading_stats(self, year):
        """Return reading statistics.

        ``books_finished`` is scoped to the given year.
        ``currently_reading`` and ``total_tracked`` are intentionally global
        (not year-scoped) because active/tracked books span across years.
        """
        with self.get_session() as session:
            books_finished = session.query(Book).filter(
                extract('year', Book.finished_at) == year
            ).count()
            currently_reading = session.query(Book).filter(Book.status == 'active').count()
            total_tracked = session.query(Book).filter(
                Book.status.in_(['active', 'completed', 'paused', 'dnf', 'not_started'])
            ).count()
            goal = session.query(ReadingGoal).filter(ReadingGoal.year == year).first()
            return {
                'books_finished': books_finished,
                'currently_reading': currently_reading,
                'total_tracked': total_tracked,
                'goal_target': goal.target_books if goal else None,
            }
