"""Repository for Book, State, and Job entities."""

import logging

from sqlalchemy import func
from sqlalchemy.exc import ProgrammingError

from .base_repository import BaseRepository
from .models import (
    Book,
    BookfusionBook,
    HardcoverDetails,
    Job,
    KosyncDocument,
    ReadingJournal,
    State,
)

logger = logging.getLogger(__name__)


class BookRepository(BaseRepository):

    # ── Book CRUD ──

    def get_book(self, abs_id):
        return self._get_one(Book, Book.abs_id == abs_id)

    def get_book_by_kosync_id(self, kosync_id):
        return self._get_one(Book, Book.kosync_doc_id == kosync_id)

    def get_all_books(self):
        return self._get_all(Book)

    def get_books_by_status(self, status):
        return self._get_all(Book, Book.status == status)

    def get_book_by_ebook_filename(self, filename):
        """Find a book by its ebook filename (current or original)."""
        from sqlalchemy import or_
        return self._get_one(
            Book,
            or_(Book.ebook_filename == filename, Book.original_ebook_filename == filename)
        )

    def create_book(self, book):
        return self._save_new(book)

    def save_book(self, book):
        return self._upsert(
            Book,
            [Book.abs_id == book.abs_id],
            book,
            ['abs_title', 'ebook_filename', 'original_ebook_filename', 'kosync_doc_id',
             'transcript_file', 'status', 'duration', 'sync_mode', 'storyteller_uuid',
                             'abs_ebook_item_id', 'activity_flag', 'custom_cover_url',
               'started_at', 'finished_at', 'rating', 'read_count'],
        )

    def delete_book(self, abs_id):
        with self.get_session() as session:
            session.query(KosyncDocument).filter(
                KosyncDocument.linked_abs_id == abs_id
            ).update({KosyncDocument.linked_abs_id: None})
            book = session.query(Book).filter(Book.abs_id == abs_id).first()
            if book:
                session.delete(book)
                return True
            return False

    def migrate_book_data(self, old_abs_id, new_abs_id):
        """Migrate all associated data from one book ID to another."""
        with self.get_session() as session:
            try:
                # Delete states for new_abs_id that would conflict with incoming ones
                incoming_clients = {
                    r[0] for r in session.query(State.client_name).filter(
                        State.abs_id == old_abs_id
                    ).all()
                }
                if incoming_clients:
                    session.query(State).filter(
                        State.abs_id == new_abs_id,
                        State.client_name.in_(incoming_clients),
                    ).delete(synchronize_session=False)

                session.query(State).filter(State.abs_id == old_abs_id).update(
                    {State.abs_id: new_abs_id}, synchronize_session=False)
                session.query(Job).filter(Job.abs_id == old_abs_id).update(
                    {Job.abs_id: new_abs_id}, synchronize_session=False)
                session.query(KosyncDocument).filter(
                    KosyncDocument.linked_abs_id == old_abs_id
                ).update({KosyncDocument.linked_abs_id: new_abs_id}, synchronize_session=False)
                session.query(ReadingJournal).filter(
                    ReadingJournal.abs_id == old_abs_id
                ).update({ReadingJournal.abs_id: new_abs_id}, synchronize_session=False)

                from .models import BookAlignment
                try:
                    session.query(BookAlignment).filter(
                        BookAlignment.abs_id == old_abs_id
                    ).update({BookAlignment.abs_id: new_abs_id}, synchronize_session=False)
                except ProgrammingError as e:
                    logger.warning(f"BookAlignment table missing during migration cleanup for '{old_abs_id}': {e}")
                try:
                    session.query(HardcoverDetails).filter(
                        HardcoverDetails.abs_id == old_abs_id
                    ).update({HardcoverDetails.abs_id: new_abs_id}, synchronize_session=False)
                except ProgrammingError as e:
                    logger.warning(f"HardcoverDetails table missing during migration cleanup for '{old_abs_id}': {e}")
                try:
                    session.query(BookfusionBook).filter(
                        BookfusionBook.matched_abs_id == old_abs_id
                    ).update({BookfusionBook.matched_abs_id: new_abs_id}, synchronize_session=False)
                except ProgrammingError as e:
                    logger.warning(f"BookFusion table missing during migration cleanup for '{old_abs_id}': {e}")

                logger.info(f"Migrated data from '{old_abs_id}' to '{new_abs_id}'")
            except Exception as e:
                logger.error(f"Failed to migrate book data: {e}")
                raise

    # ── State CRUD ──

    def get_state(self, abs_id, client_name):
        return self._get_one(State, State.abs_id == abs_id, State.client_name == client_name)

    def get_states_for_book(self, abs_id):
        return self._get_all(State, State.abs_id == abs_id)

    def get_all_states(self):
        return self._get_all(State)

    def save_state(self, state):
        return self._upsert(
            State,
            [State.abs_id == state.abs_id, State.client_name == state.client_name],
            state,
            ['last_updated', 'percentage', 'timestamp', 'xpath', 'cfi'],
        )

    def delete_states_for_book(self, abs_id):
        with self.get_session() as session:
            count = session.query(State).filter(State.abs_id == abs_id).count()
            session.query(State).filter(State.abs_id == abs_id).delete()
            return count

    # ── Job CRUD ──

    def get_latest_job(self, abs_id):
        with self.get_session() as session:
            job = session.query(Job).filter(
                Job.abs_id == abs_id
            ).order_by(Job.last_attempt.desc()).first()
            if job:
                session.expunge(job)
            return job

    def get_jobs_for_book(self, abs_id):
        return self._get_all(Job, Job.abs_id == abs_id, order_by=Job.last_attempt.desc())

    def get_all_jobs(self):
        return self._get_all(Job)

    def save_job(self, job):
        return self._save_new(job)

    def update_latest_job(self, abs_id, **kwargs):
        with self.get_session() as session:
            job = session.query(Job).filter(
                Job.abs_id == abs_id
            ).order_by(Job.last_attempt.desc()).first()
            if job:
                for key, value in kwargs.items():
                    if hasattr(job, key):
                        setattr(job, key, value)
                    else:
                        logger.warning(f"update_latest_job: unknown attribute '{key}' for job {job.id}")
                session.flush()
                session.refresh(job)
                session.expunge(job)
                return job
            return None

    def delete_jobs_for_book(self, abs_id):
        with self.get_session() as session:
            count = session.query(Job).filter(Job.abs_id == abs_id).count()
            session.query(Job).filter(Job.abs_id == abs_id).delete()
            return count

    # ── Advanced Queries ──

    def get_books_with_recent_activity(self, limit=10):
        with self.get_session() as session:
            latest = session.query(
                State.abs_id,
                func.max(State.last_updated).label('max_updated')
            ).group_by(State.abs_id).subquery()
            books = session.query(Book).join(
                latest, Book.abs_id == latest.c.abs_id
            ).order_by(latest.c.max_updated.desc()).limit(limit).all()
            for book in books:
                session.expunge(book)
            return books

    def get_failed_jobs(self, limit=20):
        with self.get_session() as session:
            jobs = session.query(Job).filter(
                Job.last_error.isnot(None)
            ).order_by(Job.last_attempt.desc()).limit(limit).all()
            for job in jobs:
                session.expunge(job)
            return jobs

    def get_statistics(self):
        with self.get_session() as session:
            stats = {
                'total_books': session.query(Book).count(),
                'active_books': session.query(Book).filter(Book.status == 'active').count(),
                'paused_books': session.query(Book).filter(Book.status == 'paused').count(),
                'dnf_books': session.query(Book).filter(Book.status == 'dnf').count(),
                'total_states': session.query(State).count(),
                'total_jobs': session.query(Job).count(),
                'failed_jobs': session.query(Job).filter(Job.last_error.isnot(None)).count(),
            }
            client_counts = session.query(
                State.client_name, func.count(State.id)
            ).group_by(State.client_name).all()
            stats['states_by_client'] = {client: count for client, count in client_counts}
            return stats
