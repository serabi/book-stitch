"""Repository for reading tracker: journals, goals, and reading fields."""

from sqlalchemy import extract

from .base_repository import BaseRepository
from .models import Book, ReadingGoal, ReadingJournal

VALID_JOURNAL_EVENTS = {'started', 'progress', 'finished', 'paused', 'dnf', 'resumed', 'note'}


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

    def add_reading_journal(self, abs_id, event, entry=None, percentage=None):
        if event not in VALID_JOURNAL_EVENTS:
            raise ValueError(f"event must be one of {VALID_JOURNAL_EVENTS}")
        if percentage is not None and not (0.0 <= percentage <= 1.0):
            raise ValueError("percentage must be between 0.0 and 1.0")
        return self._save_new(
            ReadingJournal(abs_id=abs_id, event=event, entry=entry, percentage=percentage)
        )

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
