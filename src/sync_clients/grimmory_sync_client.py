import logging
import os
from pathlib import Path

from src.api.grimmory_client import GrimmoryClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import ServiceState, SyncClient, SyncResult, UpdateProgressRequest
from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)


class GrimmorySyncClient(SyncClient):
    def __init__(self, grimmory_client: GrimmoryClient, ebook_parser: EbookParser, client_name: str = "Grimmory"):
        super().__init__(ebook_parser)
        self.grimmory_client = grimmory_client
        self.client_name = client_name
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.grimmory_client.is_configured()

    def check_connection(self):
        return self.grimmory_client.check_connection()

    def fetch_bulk_state(self) -> dict | None:
        if not self.is_configured():
            return None
        books = self.grimmory_client.get_all_books()
        if not books:
            return None
        return {(b.get("fileName") or "").lower(): b for b in books if b.get("fileName")}

    def get_supported_sync_types(self) -> set:
        """Grimmory participates in both audiobook and ebook sync modes."""
        return {"audiobook", "ebook"}

    def get_service_state(
        self, book: Book, prev_state: State | None, title_snip: str = "", bulk_context: dict = None
    ) -> ServiceState | None:
        # FIX: Use original filename if available (Tri-Link), otherwise standard filename
        epub = book.original_ebook_filename or book.ebook_filename
        if not epub:
            return None

        if bulk_context is not None:
            lookup_key = Path(epub).name.lower() if epub else ""
            book_info = bulk_context.get(lookup_key)
            if book_info:
                gr_pct, _ = self.grimmory_client.extract_progress(book_info)
            else:
                gr_pct = None
        else:
            gr_pct, _ = self.grimmory_client.get_progress(epub)

        if gr_pct is None:
            logger.debug("Grimmory percentage is None - returning None for service state")
            return None

        # Get previous Grimmory state
        prev_grimmory_pct = prev_state.percentage if prev_state else 0

        delta = abs(gr_pct - prev_grimmory_pct)

        return ServiceState(
            current={"pct": gr_pct},
            previous_pct=prev_grimmory_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.grimmory_client.is_configured(),
            display=(self.client_name, "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v * 100:.4f}%",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> str | None:
        gr_pct = state.current.get("pct")
        epub = book.original_ebook_filename or book.ebook_filename
        if gr_pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, gr_pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        # FIX: Use original filename for updates too
        epub = book.original_ebook_filename or book.ebook_filename
        pct = request.locator_result.percentage
        success = self.grimmory_client.update_progress(epub, pct, request.locator_result)
        if success:
            try:
                from src.services.write_tracker import record_write

                record_write(self.client_name, book.id)
            except ImportError:
                pass
        updated_state = {"pct": pct}
        return SyncResult(pct, success, updated_state)


class GrimmoryAudioSyncClient(GrimmorySyncClient):
    """Sync one Grimmory instance's audiobook side by exact source identity."""

    def fetch_bulk_state(self) -> dict | None:
        if not self.is_configured():
            return None
        books = self.grimmory_client.get_all_books()
        states = {
            source_id: book
            for book in books or []
            if (source_id := self.grimmory_client.audio_source_id(book))
        }
        return states or None

    def get_supported_sync_types(self) -> set:
        return {"audiobook"}

    def get_service_state(
        self, book: Book, prev_state: State | None, title_snip: str = "", bulk_context: dict = None
    ) -> ServiceState | None:
        source_id = book.grimmory_audio_source_id
        if not source_id:
            return None

        if bulk_context is not None:
            book_info = bulk_context.get(source_id)
            pct, _ = self.grimmory_client.extract_progress(book_info) if book_info else (None, None)
        else:
            pct, _ = self.grimmory_client.get_audiobook_progress(source_id)
        if pct is None:
            return None

        # ponytail: Grimmory exposes audio percentage but no transcript timing;
        # keep percentage sync until a reliable audio-to-text position exists.
        previous = prev_state.percentage if prev_state and prev_state.percentage is not None else 0
        return ServiceState(
            current={"pct": pct},
            previous_pct=previous,
            delta=abs(pct - previous),
            threshold=self.delta_kosync_thresh,
            is_configured=self.is_configured(),
            display=(self.client_name, "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda value: f"{value * 100:.4f}%",
        )

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        source_id = book.grimmory_audio_source_id
        pct = request.locator_result.percentage
        success = bool(source_id and self.grimmory_client.update_audiobook_progress(source_id, pct))
        if success:
            try:
                from src.services.write_tracker import record_write

                record_write(self.client_name, book.id)
            except ImportError:
                pass
        return SyncResult(pct, success, {"pct": pct})
