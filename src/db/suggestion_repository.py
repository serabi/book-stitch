"""Repository for pending suggestions."""

from .base_repository import BaseRepository
from .models import Book, KosyncDocument, PendingSuggestion


class SuggestionRepository(BaseRepository):

    def get_pending_suggestion(self, source_id):
        return self._get_one(
            PendingSuggestion,
            PendingSuggestion.source_id == source_id,
            PendingSuggestion.status == 'pending',
        )

    def suggestion_exists(self, source_id):
        with self.get_session() as session:
            return session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id
            ).first() is not None

    def save_pending_suggestion(self, suggestion):
        return self._upsert(
            PendingSuggestion,
            [PendingSuggestion.source_id == suggestion.source_id],
            suggestion,
            ['title', 'author', 'cover_url', 'matches_json', 'status'],
        )

    def get_all_pending_suggestions(self):
        return self._get_all(
            PendingSuggestion,
            PendingSuggestion.status == 'pending',
            order_by=PendingSuggestion.created_at.desc(),
        )

    def dismiss_suggestion(self, source_id):
        with self.get_session() as session:
            suggestion = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id
            ).first()
            if suggestion:
                suggestion.status = 'dismissed'
                return True
            return False

    def ignore_suggestion(self, source_id):
        with self.get_session() as session:
            suggestion = session.query(PendingSuggestion).filter(
                PendingSuggestion.source_id == source_id
            ).first()
            if suggestion:
                suggestion.status = 'ignored'
                return True
            return False

    def clear_stale_suggestions(self):
        """Delete suggestions whose source_id is not in the books table."""
        from sqlalchemy import not_
        with self.get_session() as session:
            count = session.query(PendingSuggestion).filter(
                not_(PendingSuggestion.source_id.in_(session.query(Book.abs_id)))
            ).delete(synchronize_session=False)
            return count

    def is_hash_linked_to_device(self, doc_hash):
        if not doc_hash:
            return False
        with self.get_session() as session:
            return session.query(KosyncDocument).filter(
                KosyncDocument.document_hash == doc_hash,
                KosyncDocument.linked_abs_id.isnot(None),
            ).count() > 0
