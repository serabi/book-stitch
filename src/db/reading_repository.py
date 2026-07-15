"""Repository for reading tracker: journals, goals, and reading fields."""

import re

from .base_repository import BaseRepository
from .models import Book, BookfusionHighlight, ReadingGoal, ReadingJournal

VALID_JOURNAL_EVENTS = {"started", "progress", "finished", "paused", "dnf", "resumed", "note", "highlight"}
BOOKFUSION_IMPORT_PREFIX = "\U0001f4d6 "
BOOKFUSION_ENTRY_SPLIT = "\n\u2014 "
BOOKFUSION_CHAPTER_PREFIX = re.compile(r"^#{1,6}\s*")


class ReadingRepository(BaseRepository):
    def _mutate_and_detach(self, model, *filters, mutate):
        """Look up a single row by ``filters``, apply ``mutate``, and return it detached.

        Returns None when no row matches. On a match, runs ``mutate(obj)`` then the
        shared flush/refresh/expunge finalization so callers get a detached object.
        """
        with self.get_session() as session:
            obj = session.query(model).filter(*filters).first()
            if not obj:
                return None
            mutate(obj)
            session.flush()
            session.refresh(obj)
            session.expunge(obj)
            return obj

    def update_book_reading_fields(self, book_id, **kwargs):
        """Update reading-specific fields on a book (started_at, finished_at, rating, read_count)."""
        allowed = {"started_at", "finished_at", "rating", "read_count"}
        rating = kwargs.get("rating")
        if rating is not None and not (0.0 <= rating <= 5.0):
            raise ValueError("rating must be between 0 and 5")
        read_count = kwargs.get("read_count")
        if read_count is not None and read_count < 1:
            raise ValueError("read_count must be >= 1")

        def _apply(book):
            for key, value in kwargs.items():
                if key in allowed:
                    setattr(book, key, value)

        return self._mutate_and_detach(Book, Book.id == book_id, mutate=_apply)

    def get_reading_journals(self, book_id):
        return self._get_all(
            ReadingJournal,
            ReadingJournal.book_id == book_id,
            order_by=ReadingJournal.created_at.desc(),
        )

    def get_reading_journal_entries_for_book(self, book_id, event=None):
        """Get all journal entries for a book, optionally filtered by event type."""
        with self.get_session() as session:
            query = session.query(ReadingJournal).filter(ReadingJournal.book_id == book_id)
            if event:
                query = query.filter(ReadingJournal.event == event)
            query = query.order_by(ReadingJournal.created_at.desc())
            return self._query_and_expunge(session, query, one=False)

    def get_reading_journal(self, journal_id):
        return self._get_one(ReadingJournal, ReadingJournal.id == journal_id)

    def add_reading_journal(self, book_id, event, entry=None, percentage=None, created_at=None, abs_id=None):
        """Add a journal entry using book_id directly."""
        if event not in VALID_JOURNAL_EVENTS:
            raise ValueError(f"event must be one of {VALID_JOURNAL_EVENTS}")
        if percentage is not None and not (0.0 <= percentage <= 1.0):
            raise ValueError("percentage must be between 0.0 and 1.0")
        return self._save_new(
            ReadingJournal(
                book_id=book_id,
                abs_id=abs_id or "",
                event=event,
                entry=entry,
                percentage=percentage,
                created_at=created_at,
            )
        )

    def update_reading_journal(self, journal_id, *, entry=None, created_at=None):
        def _apply(journal):
            if entry is not None:
                journal.entry = entry
            if created_at is not None:
                journal.created_at = created_at

        return self._mutate_and_detach(ReadingJournal, ReadingJournal.id == journal_id, mutate=_apply)

    def find_journal_by_event(self, book_id, event):
        """Find the most recent journal entry for a book with a given event type."""
        with self.get_session() as session:
            query = (
                session.query(ReadingJournal)
                .filter(
                    ReadingJournal.book_id == book_id,
                    ReadingJournal.event == event,
                )
                .order_by(ReadingJournal.created_at.desc())
            )
            return self._query_and_expunge(session, query, one=True)

    def cleanup_bookfusion_import_notes(self, book_id=None):
        """Strip the legacy emoji prefix and backfill timestamps when a cached highlight matches."""

        def _normalize_entry(entry):
            if not entry:
                return ("", "")
            text = entry
            if text.startswith(BOOKFUSION_IMPORT_PREFIX):
                text = text[len(BOOKFUSION_IMPORT_PREFIX) :]
            elif text.startswith("\U0001f4d6"):
                text = text[1:].lstrip()

            quote, chapter = text, ""
            if BOOKFUSION_ENTRY_SPLIT in text:
                quote, chapter = text.split(BOOKFUSION_ENTRY_SPLIT, 1)
            return (quote.strip(), chapter.strip())

        def _normalize_highlight(quote, chapter):
            return (
                (quote or "").strip(),
                BOOKFUSION_CHAPTER_PREFIX.sub("", (chapter or "").strip()),
            )

        with self.get_session() as session:
            journal_query = session.query(ReadingJournal).filter(
                ReadingJournal.event.in_(("note", "highlight")),
                ReadingJournal.entry.is_not(None),
                (
                    ReadingJournal.entry.startswith(BOOKFUSION_IMPORT_PREFIX)
                    | ReadingJournal.entry.startswith("\U0001f4d6")
                ),
            )
            if book_id:
                journal_query = journal_query.filter(ReadingJournal.book_id == book_id)
            journals = journal_query.order_by(ReadingJournal.id.asc()).all()
            if not journals:
                return {"entries_cleaned": 0, "timestamps_backfilled": 0}

            highlight_query = session.query(BookfusionHighlight).filter(
                BookfusionHighlight.matched_book_id.is_not(None)
            )
            if book_id:
                highlight_query = highlight_query.filter(BookfusionHighlight.matched_book_id == book_id)
            highlights = highlight_query.all()

            highlight_map = {}
            for hl in highlights:
                key = (
                    hl.matched_book_id,
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

                key = (journal.book_id, quote, chapter)
                if quote and key in highlight_map and highlight_map[key]:
                    highlighted_at = highlight_map[key].pop(0)
                    if journal.event != "highlight":
                        journal.event = "highlight"
                    if highlighted_at and journal.created_at != highlighted_at:
                        journal.created_at = highlighted_at
                        timestamps_backfilled += 1

            return {
                "entries_cleaned": entries_cleaned,
                "timestamps_backfilled": timestamps_backfilled,
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
        return self._upsert(
            ReadingGoal,
            [ReadingGoal.year == year],
            ReadingGoal(year=year, target_books=target_books),
            ["target_books"],
        )

    def get_reading_stats(self, year):
        """Backward-compatible lightweight stats summary."""
        with self.get_session() as session:
            books_finished = (
                session.query(Book)
                .filter(
                    Book.status == "completed",
                    Book.finished_at.is_not(None),
                    Book.finished_at.like(f"{year}-%"),
                )
                .count()
            )
            currently_reading = session.query(Book).filter(Book.status == "active").count()
            total_tracked = (
                session.query(Book)
                .filter(Book.status.in_(["active", "completed", "paused", "dnf", "not_started"]))
                .count()
            )
            goal = session.query(ReadingGoal).filter(ReadingGoal.year == year).first()
            return {
                "books_finished": books_finished,
                "currently_reading": currently_reading,
                "total_tracked": total_tracked,
                "goal_target": goal.target_books if goal else None,
            }
