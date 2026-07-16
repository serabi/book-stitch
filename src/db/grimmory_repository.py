"""Repository for Grimmory integration: book metadata cache."""

import json
import logging

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .base_repository import BaseRepository
from .models import GrimmoryBook

logger = logging.getLogger(__name__)


class GrimmoryRepository(BaseRepository):
    def get_grimmory_book(self, filename, server_id="default"):
        with self.get_session() as session:
            rows = (
                session.query(GrimmoryBook)
                .filter(GrimmoryBook.filename == filename, GrimmoryBook.server_id == server_id)
                .limit(2)
                .all()
            )
            if len(rows) != 1:
                return None
            session.expunge(rows[0])
            return rows[0]

    def get_grimmory_book_by_remote_id(self, book_id, file_id, server_id="default"):
        return self._get_one(
            GrimmoryBook,
            GrimmoryBook.server_id == server_id,
            GrimmoryBook.remote_book_id == str(book_id),
            GrimmoryBook.remote_file_id == str(file_id),
        )

    def get_all_grimmory_books(self, server_id=None):
        if server_id is None:
            return self._get_all(GrimmoryBook)
        return self._get_all(GrimmoryBook, GrimmoryBook.server_id == server_id)

    def save_grimmory_book(self, grimmory_book):
        exact_identity = grimmory_book.remote_book_id is not None and grimmory_book.remote_file_id is not None
        with self.get_session() as session:
            query = session.query(GrimmoryBook).filter(GrimmoryBook.server_id == grimmory_book.server_id)
            if exact_identity:
                query = query.filter(
                    GrimmoryBook.remote_book_id == str(grimmory_book.remote_book_id),
                    GrimmoryBook.remote_file_id == str(grimmory_book.remote_file_id),
                )
            else:
                query = query.filter(GrimmoryBook.filename == grimmory_book.filename)

            matches = query.limit(2).all()
            if len(matches) > 1:
                raise ValueError("Ambiguous legacy Grimmory filename cannot be updated without remote IDs")
            target = matches[0] if matches else grimmory_book
            if not matches:
                session.add(target)
            for attr in ("filename", "title", "authors", "raw_metadata", "remote_book_id", "remote_file_id"):
                setattr(target, attr, getattr(grimmory_book, attr))
            session.flush()
            session.refresh(target)
            session.expunge(target)
            return target

    def reconcile_grimmory_books(self, server_id, incoming_books):
        """Upsert a complete remote snapshot and prune stale rows in one transaction."""
        with self.get_session() as session:
            existing = session.query(GrimmoryBook).filter(GrimmoryBook.server_id == server_id).all()
            exact_existing = {
                (row.remote_book_id, row.remote_file_id): row
                for row in existing
                if row.remote_book_id is not None and row.remote_file_id is not None
            }
            desired = {
                (str(book.remote_book_id), str(book.remote_file_id)): book
                for book in incoming_books
                if book.remote_book_id is not None and book.remote_file_id is not None
            }

            for identity, incoming in desired.items():
                target = exact_existing.get(identity)
                if target is None:
                    target = incoming
                    session.add(target)
                for attr in ("filename", "title", "authors", "raw_metadata", "remote_book_id", "remote_file_id"):
                    setattr(target, attr, getattr(incoming, attr))

            for identity, row in exact_existing.items():
                if identity not in desired:
                    session.delete(row)

            live_legacy_keys = {(book.remote_book_id, book.filename) for book in incoming_books}
            legacy_rows = [
                row for row in existing if row.remote_book_id is None or row.remote_file_id is None
            ]
            legacy_filename_counts = {}
            for row in legacy_rows:
                legacy_filename_counts[row.filename] = legacy_filename_counts.get(row.filename, 0) + 1
            for row in legacy_rows:
                try:
                    metadata = json.loads(row.raw_metadata or "{}")
                    legacy_key = (str(metadata.get("id")), row.filename)
                except (AttributeError, TypeError, json.JSONDecodeError):
                    session.delete(row)
                    continue
                if legacy_filename_counts[row.filename] == 1 and legacy_key not in live_legacy_keys:
                    session.delete(row)

            session.flush()

    def replace_grimmory_book_filename(self, old_filename, grimmory_book):
        """Atomically upsert *grimmory_book* and remove the old filename row.

        If the replacement filename already exists as a distinct exact row, keep
        both rows so case-sensitive Grimmory libraries do not lose one book.
        """
        with self.get_session() as session:
            existing = (
                session.query(GrimmoryBook)
                .filter(
                    GrimmoryBook.server_id == grimmory_book.server_id,
                    GrimmoryBook.filename == grimmory_book.filename,
                )
                .first()
            )

            if existing:
                if old_filename != grimmory_book.filename:
                    # Preserve distinct exact-filename rows, including case-only
                    # collisions such as Book.epub and book.epub.
                    logger.warning(
                        "Refusing to replace Grimmory filename '%s' with existing distinct row '%s'",
                        old_filename,
                        grimmory_book.filename,
                    )
                    session.expunge(existing)
                    return existing

                target = existing
                for attr in ["title", "authors", "raw_metadata"]:
                    if hasattr(grimmory_book, attr):
                        setattr(target, attr, getattr(grimmory_book, attr))
            else:
                try:
                    session.add(grimmory_book)
                    session.flush()
                    target = grimmory_book
                except IntegrityError:
                    session.rollback()
                    target = (
                        session.query(GrimmoryBook)
                        .filter(
                            GrimmoryBook.server_id == grimmory_book.server_id,
                            GrimmoryBook.filename == grimmory_book.filename,
                        )
                        .first()
                    )
                    if not target:
                        raise
                    if old_filename != grimmory_book.filename:
                        logger.warning(
                            "Refusing to replace Grimmory filename '%s' with existing distinct row '%s'",
                            old_filename,
                            grimmory_book.filename,
                        )
                        session.expunge(target)
                        return target
                    for attr in ["title", "authors", "raw_metadata"]:
                        if hasattr(grimmory_book, attr):
                            setattr(target, attr, getattr(grimmory_book, attr))

            if old_filename != grimmory_book.filename:
                (
                    session.query(GrimmoryBook)
                    .filter(
                        GrimmoryBook.server_id == grimmory_book.server_id,
                        GrimmoryBook.filename == old_filename,
                    )
                    .delete(synchronize_session=False)
                )

            session.flush()
            session.refresh(target)
            session.expunge(target)
            return target

    def delete_grimmory_book(self, filename, server_id="default", book_id=None, file_id=None):
        try:
            with self.get_session() as session:
                query = session.query(GrimmoryBook).filter(GrimmoryBook.server_id == server_id)
                if book_id is not None and file_id is not None:
                    query = query.filter(
                        GrimmoryBook.remote_book_id == str(book_id),
                        GrimmoryBook.remote_file_id == str(file_id),
                    )
                else:
                    query = query.filter(GrimmoryBook.filename == filename)
                    if query.limit(2).count() != 1:
                        return False
                deleted = query.delete(synchronize_session=False)
                return deleted > 0
        except SQLAlchemyError as e:
            logger.error(f"Failed to delete Grimmory book '{filename}': {e}")
            return False
