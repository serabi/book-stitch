"""Repository for pending suggestions."""

from .base_repository import BaseRepository
from .models import PendingSuggestion


class SuggestionRepository(BaseRepository):
    ACTIONABLE_STATUSES = ("pending", "hidden")

    def get_suggestion(self, source_id, source="abs"):
        return self._get_one(
            PendingSuggestion,
            PendingSuggestion.source_id == source_id,
            PendingSuggestion.source == source,
        )

    def get_pending_suggestion(self, source_id, source="abs"):
        return self._get_one(
            PendingSuggestion,
            PendingSuggestion.source_id == source_id,
            PendingSuggestion.source == source,
            PendingSuggestion.status == "pending",
        )

    def suggestion_exists(self, source_id, source="abs"):
        return self._exists(
            PendingSuggestion,
            PendingSuggestion.source_id == source_id,
            PendingSuggestion.source == source,
        )

    def is_suggestion_ignored(self, source_id, source="abs"):
        return self._exists(
            PendingSuggestion,
            PendingSuggestion.source_id == source_id,
            PendingSuggestion.source == source,
            PendingSuggestion.status == "ignored",
        )

    def save_pending_suggestion(self, suggestion):
        """Upsert a suggestion, preserving hidden status if already hidden.

        Hidden preservation runs against the existing row inside the upsert
        transaction so a hide landing after any earlier read cannot be
        clobbered back to pending.
        """
        return self._upsert(
            PendingSuggestion,
            [
                PendingSuggestion.source_id == suggestion.source_id,
                PendingSuggestion.source == suggestion.source,
            ],
            suggestion,
            ["title", "author", "cover_url", "matches_json", "status"],
            normalize=self._preserve_hidden_status,
        )

    @staticmethod
    def _preserve_hidden_status(suggestion, existing):
        if existing.status == "hidden" and suggestion.status == "pending":
            suggestion.status = "hidden"

    def get_pending_suggestion_count(self):
        return self._count(PendingSuggestion, PendingSuggestion.status == "pending")

    def get_all_pending_suggestions(self):
        return self._get_all(
            PendingSuggestion,
            PendingSuggestion.status == "pending",
            order_by=PendingSuggestion.created_at.desc(),
        )

    def get_all_actionable_suggestions(self):
        return self._get_all(
            PendingSuggestion,
            PendingSuggestion.status.in_(self.ACTIONABLE_STATUSES),
            order_by=PendingSuggestion.created_at.desc(),
        )

    def get_hidden_suggestions(self):
        return self._get_all(
            PendingSuggestion,
            PendingSuggestion.status == "hidden",
            order_by=PendingSuggestion.created_at.desc(),
        )

    def delete_pending_suggestion(self, source_id, source="abs"):
        return self._delete_one(
            PendingSuggestion,
            PendingSuggestion.source_id == source_id,
            PendingSuggestion.source == source,
            PendingSuggestion.status == "pending",
        )

    def resolve_suggestion(self, source_id, source="abs"):
        return self._delete_one(
            PendingSuggestion,
            PendingSuggestion.source_id == source_id,
            PendingSuggestion.source == source,
        )

    def _transition_status(self, source_id, source, to_status, allowed_from=None):
        """Transition a suggestion's status. If allowed_from is set, only transitions
        when the current status is in the allowed set. Returns True if applied."""
        with self.get_session() as session:
            suggestion = (
                session.query(PendingSuggestion)
                .filter(
                    PendingSuggestion.source_id == source_id,
                    PendingSuggestion.source == source,
                )
                .first()
            )
            if not suggestion:
                return False
            if allowed_from is not None and suggestion.status not in allowed_from:
                return False
            suggestion.status = to_status
            return True

    def hide_suggestion(self, source_id, source="abs"):
        return self._transition_status(source_id, source, to_status="hidden", allowed_from=("pending", "hidden"))

    def unhide_suggestion(self, source_id, source="abs"):
        return self._transition_status(source_id, source, to_status="pending", allowed_from=("hidden",))

    def ignore_suggestion(self, source_id, source="abs"):
        return self._transition_status(source_id, source, to_status="ignored")

    def clear_stale_suggestions(self):
        """Delete suggestions whose source items no longer exist."""
        from sqlalchemy import not_

        from .models import Book, KosyncDocument

        with self.get_session() as session:
            count = 0
            # ABS: source_id not in any book's abs_id
            count += (
                session.query(PendingSuggestion)
                .filter(
                    PendingSuggestion.source == "abs",
                    PendingSuggestion.status.in_(self.ACTIONABLE_STATUSES),
                    not_(PendingSuggestion.source_id.in_(session.query(Book.abs_id).filter(Book.abs_id.isnot(None)))),
                )
                .delete(synchronize_session=False)
            )
            # Storyteller: source_id not in any book's storyteller_uuid
            count += (
                session.query(PendingSuggestion)
                .filter(
                    PendingSuggestion.source == "storyteller",
                    PendingSuggestion.status.in_(self.ACTIONABLE_STATUSES),
                    not_(
                        PendingSuggestion.source_id.in_(
                            session.query(Book.storyteller_uuid).filter(Book.storyteller_uuid.isnot(None))
                        )
                    ),
                )
                .delete(synchronize_session=False)
            )
            # Grimmory: source_id not in any book's ebook_filename
            count += (
                session.query(PendingSuggestion)
                .filter(
                    PendingSuggestion.source == "grimmory",
                    PendingSuggestion.status.in_(self.ACTIONABLE_STATUSES),
                    not_(
                        PendingSuggestion.source_id.in_(
                            session.query(Book.ebook_filename).filter(Book.ebook_filename.isnot(None))
                        )
                    ),
                )
                .delete(synchronize_session=False)
            )
            # KoSync: source_id not in kosync_documents
            count += (
                session.query(PendingSuggestion)
                .filter(
                    PendingSuggestion.source == "kosync",
                    PendingSuggestion.status.in_(self.ACTIONABLE_STATUSES),
                    not_(PendingSuggestion.source_id.in_(session.query(KosyncDocument.document_hash))),
                )
                .delete(synchronize_session=False)
            )
            return count

    def normalize_dismissed_suggestions(self):
        with self.get_session() as session:
            updated = (
                session.query(PendingSuggestion)
                .filter(PendingSuggestion.status == "dismissed")
                .update({"status": "hidden"}, synchronize_session=False)
            )
            return updated
