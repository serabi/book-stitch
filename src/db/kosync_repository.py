"""Repository for KoSync document operations."""

from datetime import UTC, datetime

from .base_repository import BaseRepository
from .models import Book, KosyncDocument


class KoSyncRepository(BaseRepository):
    def get_kosync_document(self, document_hash):
        return self._get_one(KosyncDocument, KosyncDocument.document_hash == document_hash)

    def save_kosync_document(self, doc):
        doc.last_updated = datetime.now(UTC)
        return self._merge_save(doc)

    def get_all_kosync_documents(self):
        return self._get_all(KosyncDocument, order_by=KosyncDocument.last_updated.desc())

    def get_unlinked_kosync_documents(self):
        return self._get_all(
            KosyncDocument,
            KosyncDocument.linked_book_id == None,
            order_by=KosyncDocument.last_updated.desc(),
        )

    def _mutate_kosync_link(self, document_hash, *, linked_book_id, linked_abs_id):
        """Set link fields on an existing document. Returns True if it existed."""
        with self.get_session() as session:
            doc = session.query(KosyncDocument).filter(KosyncDocument.document_hash == document_hash).first()
            if doc:
                doc.linked_book_id = linked_book_id
                doc.linked_abs_id = linked_abs_id
                doc.last_updated = datetime.now(UTC)
                return True
            return False

    def link_kosync_document(self, document_hash, book_id, abs_id=None):
        return self._mutate_kosync_link(document_hash, linked_book_id=book_id, linked_abs_id=abs_id)

    def unlink_kosync_document(self, document_hash):
        return self._mutate_kosync_link(document_hash, linked_book_id=None, linked_abs_id=None)

    def delete_kosync_document(self, document_hash):
        return self._delete_one(KosyncDocument, KosyncDocument.document_hash == document_hash)

    def get_kosync_documents_for_book_by_book_id(self, book_id):
        return self._get_all(KosyncDocument, KosyncDocument.linked_book_id == book_id)

    def get_kosync_doc_by_filename(self, filename):
        if filename is None:
            return None
        return self._get_one(KosyncDocument, KosyncDocument.filename == filename)

    def get_kosync_doc_by_grimmory_id(self, grimmory_id):
        if grimmory_id is None:
            return None
        return self._get_one(KosyncDocument, KosyncDocument.grimmory_id == str(grimmory_id))

    def get_orphaned_kosync_books(self):
        """Get books with kosync_doc_id set but no matching KosyncDocument."""
        with self.get_session() as session:
            subq = session.query(KosyncDocument.document_hash)
            query = session.query(Book).filter(Book.kosync_doc_id != None).filter(~Book.kosync_doc_id.in_(subq))
            return self._query_and_expunge(session, query, one=False)
