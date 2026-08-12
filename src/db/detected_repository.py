"""Repository for detected external books."""

from datetime import UTC, datetime

from .base_repository import BaseRepository
from .models import DetectedBook


class DetectedRepository(BaseRepository):
    ACTIVE_STATUSES = ("detected",)

    def get_detected_book(self, source_id, source="abs"):
        return self._get_one(
            DetectedBook,
            DetectedBook.source_id == source_id,
            DetectedBook.source == source,
        )

    def get_active_detected_books(self, limit=None):
        with self.get_session() as session:
            query = (
                session.query(DetectedBook)
                .filter(DetectedBook.status.in_(self.ACTIVE_STATUSES))
                .order_by(DetectedBook.last_seen_at.desc())
            )
            if limit is not None:
                query = query.limit(limit)
            return self._query_and_expunge(session, query, one=False)

    UPSERT_ATTRS = (
        "title",
        "author",
        "cover_url",
        "matches_json",
        "device",
        "ebook_filename",
        "progress_percentage",
        "status",
        "last_seen_at",
        "first_detected_at",
    )

    def save_detected_book(self, detected_book):
        """Upsert a detected book while preserving dismissed status.

        Normalization runs against the existing row inside the upsert transaction
        so a concurrent insert of the same (source_id, source) cannot bypass the
        conditional update rules.
        """
        return self._upsert(
            DetectedBook,
            [
                DetectedBook.source_id == detected_book.source_id,
                DetectedBook.source == detected_book.source,
            ],
            detected_book,
            self.UPSERT_ATTRS,
            normalize=self._normalize_for_update,
        )

    def _normalize_for_update(self, detected_book, existing):
        """Reconcile incoming values against an existing row so an unconditional
        attribute copy preserves the original conditional update rules."""
        now = datetime.now(UTC)

        if existing.status == "dismissed" and detected_book.status == "detected":
            detected_book.status = "dismissed"

        for attr in ("title", "author", "cover_url", "device", "ebook_filename"):
            if not getattr(detected_book, attr):
                setattr(detected_book, attr, getattr(existing, attr))

        if detected_book.matches_json is None:
            detected_book.matches_json = existing.matches_json

        detected_book.last_seen_at = detected_book.last_seen_at or now
        if existing.first_detected_at is None:
            detected_book.first_detected_at = detected_book.first_detected_at or now
        else:
            detected_book.first_detected_at = existing.first_detected_at

    def _set_detected_status(self, source_id, source, status):
        """Set the status of a matching detected book, refreshing last_seen_at.

        Returns True only when a row scoped by (source_id, source) exists; returns
        False without inserting anything when none does.
        """
        with self.get_session() as session:
            detected = (
                session.query(DetectedBook)
                .filter(
                    DetectedBook.source_id == source_id,
                    DetectedBook.source == source,
                )
                .first()
            )
            if not detected:
                return False
            detected.status = status
            detected.last_seen_at = datetime.now(UTC)
            return True

    def dismiss_detected_book(self, source_id, source="abs"):
        return self._set_detected_status(source_id, source, "dismissed")

    def resolve_detected_book(self, source_id, source="abs"):
        return self._set_detected_status(source_id, source, "resolved")

    def get_all_ebook_filenames(self):
        """Get all ebook filenames from detected books with matches."""
        with self.get_session() as session:
            query = session.query(DetectedBook).filter(
                DetectedBook.status.in_(self.ACTIVE_STATUSES),
                DetectedBook.matches_json.isnot(None),
            )
            results = self._query_and_expunge(session, query, one=False)
            filenames = set()
            for detected in results:
                matches = detected.matches or []
                for match in matches:
                    if match.get("filename"):
                        filenames.add(match["filename"])
            return filenames
