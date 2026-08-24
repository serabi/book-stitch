"""Repository for Hardcover integration: details and sync logs."""

import logging

from .base_repository import BaseRepository
from .models import HardcoverDetails, HardcoverSyncLog

logger = logging.getLogger(__name__)


class HardcoverRepository(BaseRepository):
    # ── Hardcover Details ──

    def get_hardcover_details(self, book_id):
        return self._get_one(HardcoverDetails, HardcoverDetails.book_id == book_id)

    def save_hardcover_details(self, details):
        # Prefer book_id for lookup; fall back to abs_id
        if details.book_id:
            lookup = [HardcoverDetails.book_id == details.book_id]
        else:
            lookup = [HardcoverDetails.abs_id == details.abs_id]
        return self._upsert(
            HardcoverDetails,
            lookup,
            details,
            [
                "abs_id",
                "hardcover_book_id",
                "hardcover_slug",
                "hardcover_edition_id",
                "hardcover_pages",
                "hardcover_audio_seconds",
                "isbn",
                "asin",
                "matched_by",
                "hardcover_cover_url",
                "hardcover_user_book_id",
                "hardcover_user_book_read_id",
                "hardcover_status_id",
                "hardcover_audio_edition_id",
            ],
        )

    def get_all_hardcover_details(self):
        return self._get_all(HardcoverDetails)

    # ── Hardcover Sync Logs ──

    def add_hardcover_sync_log(self, entry):
        return self._save_new(entry)

    def get_hardcover_sync_logs(self, page=1, per_page=50, direction=None, action=None, search=None):
        with self.get_session() as session:
            query = session.query(HardcoverSyncLog)
            if direction:
                query = query.filter(HardcoverSyncLog.direction == direction)
            if action:
                query = query.filter(HardcoverSyncLog.action == action)
            if search:
                like = f"%{search}%"
                query = query.filter(
                    (HardcoverSyncLog.book_title.ilike(like))
                    | (HardcoverSyncLog.detail.ilike(like))
                    | (HardcoverSyncLog.error_message.ilike(like))
                )
            items, total = self._paginate(
                query.order_by(HardcoverSyncLog.created_at.desc()), page=page, per_page=per_page
            )
            return self._expunge_items(session, items), total

    def prune_hardcover_sync_logs(self, before_date):
        with self.get_session() as session:
            deleted = (
                session.query(HardcoverSyncLog)
                .filter(HardcoverSyncLog.created_at < before_date)
                .delete(synchronize_session=False)
            )
            return deleted
