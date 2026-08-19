"""Kobo sync client: exposes Kobo device progress as a read-only sync source.

The Kobo can lead (we resolve its percentage into epub text like Grimmory) but
can never be written to — stock firmware progress lives on the device, so
update_progress is a no-op follower.
"""

import logging
import os

from src.db.models import Book, State
from src.services.kobo_service import KoboService
from src.sync_clients.sync_client_interface import ServiceState, SyncClient, SyncResult, UpdateProgressRequest
from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)


class KoboSyncClient(SyncClient):
    def __init__(self, kobo_service: KoboService, ebook_parser: EbookParser, client_name: str = "Kobo"):
        super().__init__(ebook_parser)
        self.kobo_service = kobo_service
        self.client_name = client_name
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.kobo_service.is_configured()

    def check_connection(self):
        if not self.kobo_service._db_files():
            raise RuntimeError("No Kobo database copies found (KOBO_DB_DIR and upload dir both empty)")

    def get_supported_sync_types(self) -> set:
        return {"ebook"}

    def fetch_bulk_state(self) -> dict | None:
        if not self.is_configured():
            return None
        by_id = self.kobo_service.kobo_books_by_library_id()
        return by_id or None

    def get_service_state(
        self, book: Book, prev_state: State | None, title_snip: str = "", bulk_context: dict = None
    ) -> ServiceState | None:
        kobo_books = None
        if bulk_context is not None:
            kobo_books = bulk_context.get(book.id)
        else:
            kobo_books = self.kobo_service.kobo_books_for(book.id)
        if not kobo_books:
            return None

        best = max(kobo_books, key=lambda b: (b.percent, b.read_status))
        pct = 1.0 if best.read_status == 2 else best.percent / 100.0
        prev_pct = prev_state.percentage if prev_state and prev_state.percentage is not None else 0
        delta = abs(pct - prev_pct)

        return ServiceState(
            current={"pct": pct},
            previous_pct=prev_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=True,
            display=(self.client_name, "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v * 100:.4f}%",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> str | None:
        pct = state.current.get("pct")
        epub = book.original_ebook_filename or book.ebook_filename
        if pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        # Read-only source: stock Kobo firmware can't receive progress from a server.
        logger.debug("Kobo: skipping push to '%s' (read-only source)", book.title)
        return SyncResult(request.locator_result.percentage, False)
