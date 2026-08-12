"""Repository for Book, State, and Job entities."""

import logging

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from .base_repository import BaseRepository
from .models import (
    Book,
    BookAlignment,
    BookfusionBook,
    BookfusionHighlight,
    HardcoverDetails,
    HardcoverSyncLog,
    Job,
    KosyncDocument,
    ReadingJournal,
    State,
    StorytellerSubmission,
    TbrItem,
)

logger = logging.getLogger(__name__)
_UNSET = object()

_BOOK_MERGE_METADATA_ATTRS = (
    "title",
    "author",
    "subtitle",
    "ebook_filename",
    "original_ebook_filename",
    "kosync_doc_id",
    "transcript_file",
    "status",
    "duration",
    "sync_mode",
    "storyteller_uuid",
    "abs_ebook_item_id",
    "ebook_item_id",
    "activity_flag",
    "custom_cover_url",
    "started_at",
    "finished_at",
    "rating",
    "read_count",
)

# A freshly created target book never carries these alignment/UX hints, so a
# falsy value must not clobber the canonical book that already holds them.
_BOOK_MERGE_PRESERVE_IF_EMPTY = {"transcript_file", "activity_flag"}


class BookRepository(BaseRepository):
    # ── Book CRUD ──

    def get_book_by_abs_id(self, abs_id):
        if not abs_id:
            return None
        return self._get_one(Book, Book.abs_id == abs_id)

    def get_book_by_id(self, book_id):
        return self._get_one(Book, Book.id == book_id)

    def get_book_by_ref(self, ref):
        """Resolve a book reference as abs_id first, then integer book_id.

        This keeps legacy ABS-ID URLs working while allowing new routes and
        templates to use the canonical integer primary key.
        """
        if ref is None:
            return None

        if isinstance(ref, int):
            return self.get_book_by_id(ref)

        ref_str = str(ref).strip()
        if not ref_str:
            return None

        book = self.get_book_by_abs_id(ref_str)
        if book is not None:
            return book

        if ref_str.isdigit():
            return self.get_book_by_id(int(ref_str))

        return None

    def get_book_by_kosync_id(self, kosync_id):
        return self._get_one(Book, Book.kosync_doc_id == kosync_id)

    def get_book_by_storyteller_uuid(self, uuid):
        return self._get_one(Book, Book.storyteller_uuid == uuid)

    def get_all_books(self):
        return self._get_all(Book)

    def get_books_by_status(self, status):
        return self._get_all(Book, Book.status == status)

    def search_books(self, query, limit=10):
        """Search books by title (case-insensitive substring match)."""
        if not query or not query.strip():
            return []
        with self.get_session() as session:
            db_query = session.query(Book).filter(Book.title.ilike(f"%{query}%")).limit(limit)
            return self._query_and_expunge(session, db_query, one=False)

    def get_book_by_ebook_filename(self, filename):
        """Find a book by its ebook filename (current or original)."""
        from sqlalchemy import or_

        return self._get_one(Book, or_(Book.ebook_filename == filename, Book.original_ebook_filename == filename))

    def create_book(self, book):
        return self._save_new(book)

    def save_book(self, book):
        update_attrs = [
            "title",
            "author",
            "subtitle",
            "ebook_filename",
            "original_ebook_filename",
            "kosync_doc_id",
            "transcript_file",
            "status",
            "duration",
            "sync_mode",
            "storyteller_uuid",
            "abs_ebook_item_id",
            "ebook_item_id",
            "activity_flag",
            "custom_cover_url",
            "started_at",
            "finished_at",
            "rating",
            "read_count",
        ]
        if book.id:
            return self._upsert(Book, [Book.id == book.id], book, update_attrs)
        elif book.abs_id:
            return self._upsert(Book, [Book.abs_id == book.abs_id], book, update_attrs)
        else:
            return self._save_new(book)

    def _mutate_first_and_detach(self, query_factory, mutate):
        """Load the first row from ``query_factory``, mutate it, and return it detached.

        Returns ``None`` when no row matches. Otherwise applies ``mutate(obj)``
        for the method's domain-specific semantics, then flushes, refreshes, and
        expunges the row so callers receive a detached, usable object after the
        session closes.
        """
        with self.get_session() as session:
            obj = query_factory(session).first()
            if not obj:
                return None
            mutate(obj)
            session.flush()
            session.refresh(obj)
            session.expunge(obj)
            return obj

    def update_book_metadata_overrides(self, book_id, *, title_override=_UNSET, author_override=_UNSET):
        """Update PageKeeper-local metadata override fields for a book."""

        def mutate(book):
            if title_override is not _UNSET:
                book.title_override = title_override or None
            if author_override is not _UNSET:
                book.author_override = author_override or None

        return self._mutate_first_and_detach(
            lambda session: session.query(Book).filter(Book.id == book_id),
            mutate,
        )

    def delete_book(self, book_id):
        with self.get_session() as session:
            session.query(KosyncDocument).filter(KosyncDocument.linked_book_id == book_id).update(
                {KosyncDocument.linked_abs_id: None, KosyncDocument.linked_book_id: None}
            )
            book = session.query(Book).filter(Book.id == book_id).first()
            if book:
                session.delete(book)
                return True
            return False

    def migrate_book_data(self, old_abs_id, new_abs_id):
        """Migrate a book identity while preserving the source Book row.

        The existing book is the canonical row because child state, journals,
        jobs, and external links already point at its primary key.  If a target
        ABS row was pre-created to hold fresh metadata, fold that metadata into
        the source row, move any target-only children, then delete only the
        temporary target row.
        """
        with self.get_session() as session:
            try:
                book = session.query(Book).filter(Book.abs_id == old_abs_id).first()
                if not book and old_abs_id is not None:
                    old_ref = str(old_abs_id).strip()
                    if old_ref.isdigit():
                        book = session.query(Book).filter(Book.id == int(old_ref)).first()
                if not book:
                    logger.warning(f"migrate_book_data: book '{old_abs_id}' not found")
                    return

                canonical_book_id = book.id
                previous_abs_id = book.abs_id
                incoming_clients = {
                    r[0] for r in session.query(State.client_name).filter(State.book_id == canonical_book_id).all()
                }
                target_book = session.query(Book).filter(Book.abs_id == new_abs_id).first()
                target_book_id = target_book.id if target_book else None

                if target_book and target_book_id != canonical_book_id:
                    for attr in _BOOK_MERGE_METADATA_ATTRS:
                        value = getattr(target_book, attr)
                        if attr in _BOOK_MERGE_PRESERVE_IF_EMPTY and not value:
                            continue
                        setattr(book, attr, value)

                    if incoming_clients:
                        session.query(State).filter(
                            State.book_id == target_book_id,
                            State.client_name.in_(incoming_clients),
                        ).delete(synchronize_session=False)

                    session.query(State).filter(State.book_id == target_book_id).update(
                        {State.book_id: canonical_book_id, State.abs_id: new_abs_id},
                        synchronize_session=False,
                    )
                    session.query(Job).filter(Job.book_id == target_book_id).update(
                        {Job.book_id: canonical_book_id, Job.abs_id: new_abs_id},
                        synchronize_session=False,
                    )
                    session.query(ReadingJournal).filter(ReadingJournal.book_id == target_book_id).update(
                        {ReadingJournal.book_id: canonical_book_id, ReadingJournal.abs_id: new_abs_id},
                        synchronize_session=False,
                    )
                    session.query(StorytellerSubmission).filter(StorytellerSubmission.book_id == target_book_id).update(
                        {StorytellerSubmission.book_id: canonical_book_id, StorytellerSubmission.abs_id: new_abs_id},
                        synchronize_session=False,
                    )

                    self._move_unique_child(session, HardcoverDetails, canonical_book_id, target_book_id, new_abs_id)
                    self._move_unique_child(session, BookAlignment, canonical_book_id, target_book_id, new_abs_id)

                    session.query(HardcoverSyncLog).filter(HardcoverSyncLog.book_id == target_book_id).update(
                        {HardcoverSyncLog.book_id: canonical_book_id, HardcoverSyncLog.abs_id: new_abs_id},
                        synchronize_session=False,
                    )
                    session.query(BookfusionHighlight).filter(
                        BookfusionHighlight.matched_book_id == target_book_id
                    ).update({BookfusionHighlight.matched_book_id: canonical_book_id}, synchronize_session=False)
                    session.query(BookfusionBook).filter(BookfusionBook.matched_book_id == target_book_id).update(
                        {BookfusionBook.matched_book_id: canonical_book_id},
                        synchronize_session=False,
                    )
                    session.query(TbrItem).filter(TbrItem.book_id == target_book_id).update(
                        {TbrItem.book_id: canonical_book_id, TbrItem.book_abs_id: new_abs_id},
                        synchronize_session=False,
                    )

                    session.delete(target_book)
                    session.flush()

                # Update the book's abs_id — child rows follow via book_id FK
                book.abs_id = new_abs_id

                # Update denormalized abs_id on child rows
                session.query(State).filter(State.book_id == canonical_book_id).update(
                    {State.abs_id: new_abs_id}, synchronize_session=False
                )
                session.query(Job).filter(Job.book_id == canonical_book_id).update(
                    {Job.abs_id: new_abs_id}, synchronize_session=False
                )
                session.query(ReadingJournal).filter(ReadingJournal.book_id == canonical_book_id).update(
                    {ReadingJournal.abs_id: new_abs_id}, synchronize_session=False
                )
                session.query(StorytellerSubmission).filter(StorytellerSubmission.book_id == canonical_book_id).update(
                    {StorytellerSubmission.abs_id: new_abs_id}, synchronize_session=False
                )
                session.query(HardcoverDetails).filter(HardcoverDetails.book_id == canonical_book_id).update(
                    {HardcoverDetails.abs_id: new_abs_id}, synchronize_session=False
                )
                session.query(BookAlignment).filter(BookAlignment.book_id == canonical_book_id).update(
                    {BookAlignment.abs_id: new_abs_id}, synchronize_session=False
                )
                session.query(HardcoverSyncLog).filter(HardcoverSyncLog.book_id == canonical_book_id).update(
                    {HardcoverSyncLog.abs_id: new_abs_id}, synchronize_session=False
                )
                session.query(TbrItem).filter(TbrItem.book_id == canonical_book_id).update(
                    {TbrItem.book_abs_id: new_abs_id}, synchronize_session=False
                )

                session.query(KosyncDocument).filter(
                    or_(
                        KosyncDocument.linked_book_id.in_([canonical_book_id, target_book_id]),
                        KosyncDocument.linked_abs_id.in_([old_abs_id, previous_abs_id, new_abs_id]),
                    )
                ).update(
                    {KosyncDocument.linked_book_id: canonical_book_id, KosyncDocument.linked_abs_id: new_abs_id},
                    synchronize_session=False,
                )

                logger.info(f"Migrated book identity from '{old_abs_id}' to '{new_abs_id}'")
            except Exception as e:
                logger.error(f"Failed to migrate book data: {e}")
                raise

    def _move_unique_child(self, session, model, canonical_book_id, target_book_id, new_abs_id):
        target_child = session.query(model).filter(model.book_id == target_book_id).first()
        if not target_child:
            return

        canonical_child = session.query(model).filter(model.book_id == canonical_book_id).first()
        if canonical_child:
            session.delete(target_child)
            return

        target_child.book_id = canonical_book_id
        if hasattr(target_child, "abs_id"):
            target_child.abs_id = new_abs_id

    # ── State CRUD ──

    def get_state(self, book_id, client_name):
        return self._get_one(State, State.book_id == book_id, State.client_name == client_name)

    def get_states_for_book(self, book_id):
        return self._get_all(State, State.book_id == book_id)

    def get_all_states(self):
        return self._get_all(State)

    def save_state(self, state):
        if not state.book_id and not state.abs_id:
            logger.error("save_state called without book_id or abs_id — skipping")
            return None

        update_attrs = ["last_updated", "percentage", "timestamp", "xpath", "cfi", "abs_id", "book_id"]
        with self.get_session() as session:
            self._hydrate_state_book_reference(session, state)
            if not state.book_id:
                logger.warning("save_state could not resolve abs_id '%s' to a book — skipping", state.abs_id)
                return None

            lookup = self._state_lookup_filters(state)
            existing = self._dedupe_existing_states(session, lookup)

            if existing:
                self._apply_state_attrs(existing, state, update_attrs)
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing

            snapshot = self._snapshot_state_scalars(state, update_attrs)
            try:
                session.add(state)
                session.flush()
            except IntegrityError:
                session.rollback()
                if not snapshot["book_id"] and not snapshot["abs_id"]:
                    logger.warning("save_state could not resolve abs_id '%s' to a book after conflict — skipping", snapshot["abs_id"])
                    return None

                lookup = self._snapshot_lookup_filters(snapshot)
                existing = self._dedupe_existing_states(session, lookup)
                if not existing:
                    raise
                self._apply_snapshot_attrs(existing, snapshot, update_attrs)
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing

            session.refresh(state)
            session.expunge(state)
            return state

    def _hydrate_state_book_reference(self, session, state):
        """Resolve legacy abs_id-only saves to the canonical book_id."""
        if state.book_id:
            if not state.abs_id:
                book = session.query(Book).filter(Book.id == state.book_id).first()
                if book:
                    state.abs_id = book.abs_id or ""
            return

        if state.abs_id:
            book = session.query(Book).filter(Book.abs_id == state.abs_id).first()
            if book:
                state.book_id = book.id

    @staticmethod
    def _state_lookup_filters(state):
        if state.book_id:
            return [State.book_id == state.book_id, State.client_name == state.client_name]
        return [State.abs_id == state.abs_id, State.client_name == state.client_name]

    @staticmethod
    def _snapshot_state_scalars(state, update_attrs):
        """Capture scalar values off the ORM object so conflict recovery never
        re-reads it after rollback (post-rollback lifecycle is brittle)."""
        snapshot = {
            "book_id": state.book_id,
            "client_name": state.client_name,
            "abs_id": state.abs_id,
        }
        for attr in update_attrs:
            if hasattr(state, attr):
                snapshot[attr] = getattr(state, attr)
        return snapshot

    @staticmethod
    def _snapshot_lookup_filters(snapshot):
        if snapshot["book_id"]:
            return [State.book_id == snapshot["book_id"], State.client_name == snapshot["client_name"]]
        return [State.abs_id == snapshot["abs_id"], State.client_name == snapshot["client_name"]]

    @staticmethod
    def _apply_snapshot_attrs(existing, snapshot, update_attrs):
        for attr in update_attrs:
            if attr in snapshot:
                value = snapshot[attr]
                if attr == "book_id" and value is None:
                    continue
                if attr == "abs_id" and not value:
                    continue
                setattr(existing, attr, value)

    @staticmethod
    def _dedupe_existing_states(session, lookup_filters):
        matches = (
            session.query(State)
            .filter(*lookup_filters)
            .order_by(func.coalesce(State.last_updated, -1).desc(), State.id.desc())
            .all()
        )
        if not matches:
            return None

        keeper = matches[0]
        for duplicate in matches[1:]:
            session.delete(duplicate)
        return keeper

    @staticmethod
    def _apply_state_attrs(existing, incoming, update_attrs):
        for attr in update_attrs:
            if hasattr(incoming, attr):
                value = getattr(incoming, attr)
                if attr == "book_id" and value is None:
                    continue
                if attr == "abs_id" and not value:
                    continue
                setattr(existing, attr, value)

    def delete_states_for_book(self, book_id):
        with self.get_session() as session:
            count = session.query(State).filter(State.book_id == book_id).count()
            session.query(State).filter(State.book_id == book_id).delete()
            return count

    # ── Job CRUD ──

    def get_latest_job(self, book_id):
        with self.get_session() as session:
            db_query = session.query(Job).filter(Job.book_id == book_id).order_by(Job.last_attempt.desc())
            return self._query_and_expunge(session, db_query, one=True)

    def get_latest_jobs_bulk(self, book_ids):
        """Fetch the latest job for each book_id in one query.

        Returns a dict of {book_id: Job}.
        """
        if not book_ids:
            return {}
        with self.get_session() as session:
            latest = (
                session.query(
                    Job.book_id,
                    func.max(Job.last_attempt).label("max_ts"),
                )
                .filter(Job.book_id.in_(book_ids))
                .group_by(Job.book_id)
                .subquery()
            )
            db_query = session.query(Job).join(
                latest,
                (Job.book_id == latest.c.book_id)
                & (func.coalesce(Job.last_attempt, "1970-01-01") == func.coalesce(latest.c.max_ts, "1970-01-01")),
            )
            rows = self._query_and_expunge(session, db_query, one=False)
            result = {}
            for job in rows:
                result[job.book_id] = job
            return result

    def get_jobs_for_book(self, book_id):
        return self._get_all(Job, Job.book_id == book_id, order_by=Job.last_attempt.desc())

    def get_all_jobs(self):
        return self._get_all(Job)

    def save_job(self, job):
        return self._save_new(job)

    def update_latest_job(self, book_id, **kwargs):
        def mutate(job):
            for key, value in kwargs.items():
                if hasattr(job, key):
                    setattr(job, key, value)
                else:
                    logger.warning(f"update_latest_job: unknown attribute '{key}' for job {job.id}")

        return self._mutate_first_and_detach(
            lambda session: session.query(Job).filter(Job.book_id == book_id).order_by(Job.last_attempt.desc()),
            mutate,
        )

    def delete_jobs_for_book(self, book_id):
        with self.get_session() as session:
            count = session.query(Job).filter(Job.book_id == book_id).count()
            session.query(Job).filter(Job.book_id == book_id).delete()
            return count

    # ── Advanced Queries ──

    def get_books_with_recent_activity(self, limit=10):
        with self.get_session() as session:
            latest = (
                session.query(State.book_id, func.max(State.last_updated).label("max_updated"))
                .group_by(State.book_id)
                .subquery()
            )
            db_query = (
                session.query(Book)
                .join(latest, Book.id == latest.c.book_id)
                .order_by(latest.c.max_updated.desc())
                .limit(limit)
            )
            return self._query_and_expunge(session, db_query, one=False)

    def get_failed_jobs(self, limit=20):
        with self.get_session() as session:
            db_query = (
                session.query(Job)
                .filter(Job.last_error.isnot(None))
                .order_by(Job.last_attempt.desc())
                .limit(limit)
            )
            return self._query_and_expunge(session, db_query, one=False)

    def get_statistics(self):
        with self.get_session() as session:
            stats = {
                "total_books": session.query(Book).count(),
                "active_books": session.query(Book).filter(Book.status == "active").count(),
                "paused_books": session.query(Book).filter(Book.status == "paused").count(),
                "dnf_books": session.query(Book).filter(Book.status == "dnf").count(),
                "total_states": session.query(State).count(),
                "total_jobs": session.query(Job).count(),
                "failed_jobs": session.query(Job).filter(Job.last_error.isnot(None)).count(),
            }
            client_counts = session.query(State.client_name, func.count(State.id)).group_by(State.client_name).all()
            stats["states_by_client"] = {client: count for client, count in client_counts}
            return stats
