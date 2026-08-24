import logging
import os
import time

from src.api.libby_client import LibbyClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import ServiceState, SyncClient, SyncResult, UpdateProgressRequest
from src.utils.ebook_utils import EbookParser
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)

# Never hit Libby more often than this, even on targeted syncs.
MIN_FETCH_GAP = 60
# Give up (and cool down) after this many consecutive failed refresh cycles.
MAX_CONSECUTIVE_FAILURES = 5
AUTO_DISABLE_COOLDOWN_SECS = 1800


class LibbySyncClient(SyncClient):
    """Libby reader-position client — read-only leader.

    Positions come from two stores, tried in order:
      1. legacy sentry data store (cross-device, works for audiobooks)
      2. reader passport possession (web-reader sessions)

    Polling is throttled to LIBBY_POLL_MINS (default 60, floor 10);
    fetch_bulk_state serves its cache between polls. Consecutive failed
    cycles trigger exponential backoff honoring Retry-After and eventually
    an auto-disable cooldown so the integration degrades to "offline"
    instead of hammering a struggling API.
    """

    def __init__(self, libby_client: LibbyClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.libby_client = libby_client
        # psn_key -> {"pct": float, "reading_time": int|None}
        self._positions: dict[str, dict] = {}
        # psn_key -> passport response (re-issued near expiry)
        self._passport_cache: dict[str, tuple[dict, float]] = {}
        self._last_fetch = 0.0
        self._consecutive_failures = 0
        self._disabled_until = 0.0
        self._retry_after_until = 0.0

    # ── Capability ─────────────────────────────────────────────────

    def is_configured(self) -> bool:
        if time.time() < self._disabled_until:
            return False
        return self.libby_client.is_configured()

    def check_connection(self):
        return self.libby_client.check_connection()

    def can_be_leader(self) -> bool:
        return True

    def get_supported_sync_types(self) -> set:
        """Libby loans cover both ebooks and audiobooks."""
        return {"audiobook", "ebook"}

    @property
    def auto_disabled(self) -> bool:
        return time.time() < self._disabled_until

    def _poll_secs(self) -> int:
        return self.libby_client.poll_mins * 60

    # ── Bulk fetch ─────────────────────────────────────────────────

    def fetch_bulk_state(self) -> dict | None:
        """Refresh positions for all active loans (throttled).

        Returns {psn_key: {"pct": float, "reading_time": int|None}} or the
        cached copy when the poll interval hasn't elapsed."""
        now = time.time()
        if not self.is_configured():
            return None
        if now < self._retry_after_until:
            logger.debug("Libby: honoring Retry-After; skipping cycle")
            return dict(self._positions) or None

        poll_secs = self._poll_secs()
        if self._positions and now - self._last_fetch < max(poll_secs, MIN_FETCH_GAP):
            return dict(self._positions)

        try:
            loans = self.libby_client.get_active_loans()
        except Exception as e:
            loans = None
            logger.warning("Libby: loan fetch failed: %s", e)

        if loans is None:
            self._register_failure()
            return dict(self._positions) or None

        fresh: dict[str, dict] = {}
        for loan in loans:
            pct, reading_time = self._read_loan_position(loan)
            if pct is not None:
                fresh[loan["psn_key"]] = {"pct": pct, "reading_time": reading_time}

        self._positions = fresh
        self._last_fetch = time.time()
        self._consecutive_failures = 0
        logger.info("Libby: refreshed %d position(s) from %d active loan(s)", len(fresh), len(loans))
        return dict(self._positions)

    def _register_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self._disabled_until = time.time() + AUTO_DISABLE_COOLDOWN_SECS
            self._consecutive_failures = 0
            logger.error(
                "Libby: %d consecutive failed cycles — auto-disabling for %d minutes",
                MAX_CONSECUTIVE_FAILURES,
                AUTO_DISABLE_COOLDOWN_SECS // 60,
            )
        else:
            logger.warning(
                "Libby: cycle failed (%d/%d consecutive)",
                self._consecutive_failures,
                MAX_CONSECUTIVE_FAILURES,
            )

    def _read_loan_position(self, loan: dict) -> tuple[float | None, int | None]:
        """Resolve one loan's position: legacy store first, then possession."""
        card_id, title_id, media_type = loan["card_id"], loan["title_id"], loan["media_type"]

        legacy = self.libby_client.get_legacy_position(card_id, title_id, media_type)
        pct, reading_time = self._extract_position(legacy)
        if pct is not None:
            return pct, reading_time

        passport = self._get_cached_passport(loan)
        if not passport:
            return None, None
        possession = self.libby_client.get_possession(passport["urls"]["possession"])
        return self._extract_position(possession)

    @staticmethod
    def _extract_position(data: dict | None) -> tuple[float | None, int | None]:
        """Pull percentageOfBook + readingTime out of either store's payload."""
        if not data:
            return None, None
        pos = data.get("position")
        stats = data.get("statistics") or {}
        reading_time = stats.get("readingTime")
        pct = pos.get("percentageOfBook") if isinstance(pos, dict) else None
        if pct is None:
            return None, None
        return float(pct), reading_time

    def _get_cached_passport(self, loan: dict) -> dict | None:
        psn_key = loan["psn_key"]
        cached = self._passport_cache.get(psn_key)
        if cached:
            passport, issued_at = cached
            expires = passport.get("expires") or 0
            leeway = passport.get("leeway") or 0
            if time.time() < expires - leeway and time.time() - issued_at < 3600:
                return passport

        if not self.libby_client.can_read_positions:
            return None
        passport = self.libby_client.get_passport(loan["card_id"], loan["title_id"], loan.get("media_type") or "book")
        if passport and passport.get("urls"):
            self._passport_cache[psn_key] = (passport, time.time())
            return passport
        logger.debug("Libby: no passport for %s", sanitize_log_data(psn_key))
        return None

    # ── SyncClient contract ────────────────────────────────────────

    def get_service_state(
        self, book: Book, prev_state: State | None, title_snip: str = "", bulk_context: dict = None
    ) -> ServiceState | None:
        psn_key = getattr(book, "libby_psn_key", None)
        if not psn_key:
            return None

        data = (bulk_context or {}).get(psn_key)
        if data is None:
            if bulk_context is not None:
                return None  # bulk provided but this book has no Libby loan
            data = self._targeted_position(book)

        if not data or data.get("pct") is None:
            return None

        pct = data["pct"]
        previous = prev_state.percentage if prev_state and prev_state.percentage is not None else 0
        threshold = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

        return ServiceState(
            current={"pct": pct},
            previous_pct=previous,
            delta=abs(pct - previous),
            threshold=threshold,
            is_configured=self.is_configured(),
            display=("Libby", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v * 100:.4f}%",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> str | None:
        # Percentage-only locator: text extraction happens via the shared
        # percentage path in the sync manager when needed.
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        """Read-only: Libby offers no patron write path we exercise."""
        logger.debug("Libby: read-only client; ignoring progress push for '%s'", sanitize_log_data(book.title))
        return SyncResult(None, False, {})

    # ── Targeted (non-bulk) path ───────────────────────────────────

    def _targeted_position(self, book: Book) -> dict | None:
        """Fetch a single loan's position for manual per-book syncs.
        Throttled to MIN_FETCH_GAP to stay rate-safe."""
        now = time.time()
        if now < self._last_fetch + MIN_FETCH_GAP:
            return self._positions.get(book.libby_psn_key)

        loans = {loan["psn_key"]: loan for loan in self.libby_client.get_active_loans()}
        loan = loans.get(book.libby_psn_key)
        if not loan:
            return None
        pct, reading_time = self._read_loan_position(loan)
        self._last_fetch = now
        if pct is None:
            return None
        entry = {"pct": pct, "reading_time": reading_time}
        self._positions[book.libby_psn_key] = entry
        return entry
