"""Repository for detected external books."""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_

from .base_repository import BaseRepository
from .models import DetectedBook


class DetectedRepository(BaseRepository):
    ACTIVE_STATUSES = ("detected",)
    # ponytail: fixed ceiling keeps abandoned claims recoverable without config surface.
    _PROCESSING_LEASE = timedelta(minutes=10)

    def get_detected_book(self, source_id, source="abs"):
        return self._get_one(
            DetectedBook,
            DetectedBook.source_id == source_id,
            DetectedBook.source == source,
        )

    def get_active_detected_books(self, limit=None):
        with self.get_session() as session:
            self._restore_expired_claims(session)
            query = (
                session.query(DetectedBook)
                .filter(DetectedBook.status.in_(self.ACTIVE_STATUSES))
                .order_by(DetectedBook.source_updated_at.desc(), DetectedBook.last_seen_at.desc())
            )
            if limit is not None:
                query = query.limit(limit)
            return self._query_and_expunge(session, query, one=False)

    def get_active_detected_book_count(self):
        with self.get_session() as session:
            self._restore_expired_claims(session)
            return session.query(DetectedBook).filter(DetectedBook.status.in_(self.ACTIVE_STATUSES)).count()

    def get_detected_book_count(self):
        return self._count(DetectedBook)

    UPSERT_ATTRS = (
        "title",
        "author",
        "cover_url",
        "matches_json",
        "device",
        "ebook_filename",
        "progress_percentage",
        "source_updated_at",
        "status",
        "processing_token",
        "processing_started_at",
        "last_seen_at",
        "first_detected_at",
        "media_format",
    )

    def save_detected_book(self, detected_book):
        """Upsert a detected book while preserving terminal status.

        Normalization runs against the existing row inside the upsert transaction
        so a concurrent insert of the same (source_id, source) cannot bypass the
        conditional update rules.
        """
        if detected_book.source_updated_at is not None and detected_book.source_updated_at.tzinfo is not None:
            detected_book.source_updated_at = detected_book.source_updated_at.astimezone(UTC).replace(tzinfo=None)
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

        if self._has_live_claim(existing, now):
            detected_book.status = "processing"
            detected_book.processing_token = existing.processing_token
            detected_book.processing_started_at = existing.processing_started_at
        else:
            detected_book.processing_token = None
            detected_book.processing_started_at = None

        if existing.status in {"dismissed", "resolved"} and detected_book.status == "detected":
            detected_book.status = existing.status

        for attr in ("title", "author", "cover_url", "device", "ebook_filename", "media_format"):
            if not getattr(detected_book, attr):
                setattr(detected_book, attr, getattr(existing, attr))

        if detected_book.matches_json is None:
            detected_book.matches_json = existing.matches_json

        if detected_book.source_updated_at is None:
            if existing.source_updated_at is not None:
                detected_book.progress_percentage = existing.progress_percentage
            detected_book.source_updated_at = existing.source_updated_at
        else:
            incoming = detected_book.source_updated_at
            if incoming.tzinfo is not None:
                incoming = incoming.astimezone(UTC).replace(tzinfo=None)
            detected_book.source_updated_at = incoming
        if detected_book.source_updated_at is not None and existing.source_updated_at is not None:
            incoming = detected_book.source_updated_at
            current = existing.source_updated_at
            if current.tzinfo is not None:
                current = current.astimezone(UTC).replace(tzinfo=None)
            if incoming < current:
                detected_book.progress_percentage = existing.progress_percentage
                detected_book.source_updated_at = existing.source_updated_at
            elif incoming == current:
                detected_book.progress_percentage = max(
                    detected_book.progress_percentage or 0,
                    existing.progress_percentage or 0,
                )

        detected_book.last_seen_at = detected_book.last_seen_at or now
        if existing.first_detected_at is None:
            detected_book.first_detected_at = detected_book.first_detected_at or now
        else:
            detected_book.first_detected_at = existing.first_detected_at

    def _has_live_claim(self, detected, now):
        if detected.status != "processing" or not detected.processing_token or not detected.processing_started_at:
            return False
        started_at = detected.processing_started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        return started_at > now - self._PROCESSING_LEASE

    def _restore_expired_claims(self, session):
        cutoff = datetime.now(UTC) - self._PROCESSING_LEASE
        return (
            session.query(DetectedBook)
            .filter(
                DetectedBook.status == "processing",
                or_(
                    DetectedBook.processing_started_at.is_(None),
                    DetectedBook.processing_started_at <= cutoff,
                ),
            )
            .update(
                {
                    DetectedBook.status: "detected",
                    DetectedBook.processing_token: None,
                    DetectedBook.processing_started_at: None,
                },
                synchronize_session=False,
            )
        )

    def _set_detected_status(self, source_id, source, status):
        """Set the status of a matching detected book, refreshing last_seen_at.

        Returns True only when a row scoped by (source_id, source) exists; returns
        False without inserting anything when none does.
        """
        with self.get_session() as session:
            updated = (
                session.query(DetectedBook)
                .filter(
                    DetectedBook.source_id == source_id,
                    DetectedBook.source == source,
                    DetectedBook.status != "processing",
                )
                .update(
                    {
                        DetectedBook.status: status,
                        DetectedBook.processing_token: None,
                        DetectedBook.processing_started_at: None,
                        DetectedBook.last_seen_at: datetime.now(UTC),
                    },
                    synchronize_session=False,
                )
            )
            return updated == 1

    def claim_detected_book(self, source_id, source="abs"):
        """Atomically claim active or expired work and return its owner token."""
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        cutoff = now - self._PROCESSING_LEASE
        with self.get_session() as session:
            updated = (
                session.query(DetectedBook)
                .filter(
                    DetectedBook.source_id == source_id,
                    DetectedBook.source == source,
                    or_(
                        DetectedBook.status == "detected",
                        and_(
                            DetectedBook.status == "processing",
                            or_(
                                DetectedBook.processing_started_at.is_(None),
                                DetectedBook.processing_started_at <= cutoff,
                            ),
                        ),
                    ),
                )
                .update(
                    {
                        DetectedBook.status: "processing",
                        DetectedBook.processing_token: token,
                        DetectedBook.processing_started_at: now,
                        DetectedBook.last_seen_at: now,
                    },
                    synchronize_session=False,
                )
            )
            return token if updated == 1 else None

    def renew_detected_book_claim(self, source_id, processing_token, source="abs"):
        """Refresh a claim lease only while the caller still owns it."""
        with self.get_session() as session:
            updated = (
                session.query(DetectedBook)
                .filter(
                    DetectedBook.source_id == source_id,
                    DetectedBook.source == source,
                    DetectedBook.status == "processing",
                    DetectedBook.processing_token == processing_token,
                )
                .update(
                    {DetectedBook.processing_started_at: datetime.now(UTC)},
                    synchronize_session=False,
                )
            )
            return updated == 1

    def _finish_claim(self, source_id, processing_token, source, status):
        with self.get_session() as session:
            updated = (
                session.query(DetectedBook)
                .filter(
                    DetectedBook.source_id == source_id,
                    DetectedBook.source == source,
                    DetectedBook.status == "processing",
                    DetectedBook.processing_token == processing_token,
                )
                .update(
                    {
                        DetectedBook.status: status,
                        DetectedBook.processing_token: None,
                        DetectedBook.processing_started_at: None,
                        DetectedBook.last_seen_at: datetime.now(UTC),
                    },
                    synchronize_session=False,
                )
            )
            return updated == 1

    def restore_detected_book(self, source_id, processing_token, source="abs"):
        return self._finish_claim(source_id, processing_token, source, "detected")

    def complete_detected_book(self, source_id, processing_token, source="abs"):
        return self._finish_claim(source_id, processing_token, source, "resolved")

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
