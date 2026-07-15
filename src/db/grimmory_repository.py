"""Repository for Grimmory integration: book metadata cache."""

import logging

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .base_repository import BaseRepository
from .models import GrimmoryBook

logger = logging.getLogger(__name__)


class GrimmoryRepository(BaseRepository):
    def get_grimmory_book(self, filename, server_id="default"):
        return self._get_one(
            GrimmoryBook,
            GrimmoryBook.filename == filename,
            GrimmoryBook.server_id == server_id,
        )

    def get_all_grimmory_books(self, server_id=None):
        if server_id is None:
            return self._get_all(GrimmoryBook)
        return self._get_all(GrimmoryBook, GrimmoryBook.server_id == server_id)

    def save_grimmory_book(self, grimmory_book):
        return self._upsert(
            GrimmoryBook,
            [
                GrimmoryBook.server_id == grimmory_book.server_id,
                GrimmoryBook.filename == grimmory_book.filename,
            ],
            grimmory_book,
            ["title", "authors", "raw_metadata"],
        )

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

    def delete_grimmory_book(self, filename, server_id="default"):
        try:
            with self.get_session() as session:
                deleted = (
                    session.query(GrimmoryBook)
                    .filter(
                        GrimmoryBook.server_id == server_id,
                        GrimmoryBook.filename == filename,
                    )
                    .delete(synchronize_session=False)
                )
                return deleted > 0
        except SQLAlchemyError as e:
            logger.error(f"Failed to delete Grimmory book '{filename}': {e}")
            return False
