"""Libby integration service — matching, lifecycle, TBR curation, journal backfill.

Bridges Libby loans to PageKeeper books:
- enriches active loans with public Thunder metadata (ISBNs)
- matches by ISBN (HardcoverDetails/TbrItem) then title/author; confident
  matches link book.libby_psn_key automatically, the rest land in the
  pending_suggestions review queue
- unlinks books whose loans disappeared (returned/expired) with a journal
  note instead of a status change — returning a library book is not
  "finished reading"
- surfaces holds as TBR review-queue suggestions (merge is always explicit)
- backfills possession/legacy statistics.readingTime deltas into the
  reading journal, attributed to the day of observation
"""

import json
import logging

from src.api.thunder_client import ThunderClient

logger = logging.getLogger(__name__)

# Minimum listening/reading seconds before a delta becomes a journal entry.
READING_TIME_DELTA_THRESHOLD = 300


class LibbyService:
    def __init__(self, database_service, libby_client, thunder_client=None):
        self.db = database_service
        self.libby = libby_client
        self.thunder = thunder_client or ThunderClient()
        # psn_key -> last observed readingTime seconds (session-scoped)
        self._last_reading_time: dict[str, int] = {}

    def refresh(self, positions: dict | None = None):
        """Run one matching/lifecycle/backfill pass over active loans.

        ``positions`` maps psn_key -> {"pct", "reading_time"} as produced by
        the sync client's bulk fetch. Called from the sync client after
        positions refresh (same cadence)."""
        if not self.libby.is_configured():
            return
        loans = self.libby.get_active_loans()
        if loans is None:
            return

        enriched = []
        for loan in loans:
            media = self.thunder.get_media(loan["title_id"])
            loan["isbns"] = self.thunder.extract_isbns(media)
            loan["cover_url"] = self.thunder.extract_cover(media)
            pos = (positions or {}).get(loan["psn_key"]) or {}
            loan["reading_time"] = pos.get("reading_time")
            enriched.append(loan)

        self.match_loans(enriched)
        self.handle_lifecycle(enriched)
        for loan in enriched:
            if positions is not None:
                self._maybe_journal_reading_time(loan)

    # ── Enrichment & matching ──────────────────────────────────────

    def match_loans(self, loans: list[dict]):
        """Link each loan to a PageKeeper book when confident."""
        linked_psns = {getattr(b, "libby_psn_key", None) for b in self.db.get_all_books() or []}
        for loan in loans:
            psn = loan["psn_key"]
            if psn in linked_psns:
                continue

            book = self._find_book_for_loan(loan)
            if book is None:
                self._queue_match_suggestion(loan)
                continue

            book.libby_psn_key = psn
            self.db.save_book(book)
            logger.info(
                "Libby: matched loan %s to '%s' (%s)",
                psn,
                book.title,
                getattr(book, "_match_method", "unknown"),
            )

    def _find_book_for_loan(self, loan: dict):
        """ISBN exact first (HardcoverDetails / TBR items), then
        title+author fuzzy over the library."""
        books = self.db.get_all_books() or []
        isbns = {i for i in loan.get("isbns") or []}
        if isbns:
            for book in books:
                details = getattr(book, "hardcover_details", None)
                details_isbn = getattr(details, "isbn", None) if details else None
                if details_isbn and details_isbn in isbns:
                    book._match_method = "isbn"
                    return book

        best, best_score = None, 0.0
        try:
            from rapidfuzz import fuzz

            target_title = (loan.get("title") or "").lower()
            target_author = (loan.get("authors") or "").split(",")[0].strip().lower()
            if len(target_title) < 4:
                return None
            for candidate in books:
                score = fuzz.token_sort_ratio(target_title, (candidate.title or "").lower())
                cand_author = ((candidate.author or "").split(",")[0]).strip().lower()
                author_boost = 15 if target_author and cand_author and target_author == cand_author else 0
                total = score + author_boost
                if total > best_score:
                    best, best_score = candidate, total
        except ImportError:
            return None

        if best is not None and best_score >= 90:
            best._match_method = f"title_author({best_score:.0f})"
            return best

        logger.debug("Libby: no confident match for '%s' (best score %.0f)", loan.get("title"), best_score)
        return None

    def _queue_match_suggestion(self, loan: dict):
        """Unmatched loans join the pending_suggestions review queue."""
        existing = [
            s
            for s in (self.db.get_all_pending_suggestions() or [])
            if s.source == "libby_loan" and s.source_id == loan["psn_key"]
        ]
        if any(s.status == "pending" for s in existing):
            return
        from src.db.models import PendingSuggestion

        self.db.save_pending_suggestion(
            PendingSuggestion(
                source="libby_loan",
                source_id=loan["psn_key"],
                title=loan.get("title"),
                author=loan.get("authors"),
                cover_url=loan.get("cover_url"),
                matches_json=None,
            )
        )

    # ── Lifecycle ──────────────────────────────────────────────────

    def handle_lifecycle(self, active_loans: list[dict]):
        """Unlink books whose Libby loan vanished (returned or expired).

        Returning a library book is not 'finished reading', so this never
        touches status transitions — it just stops position sync and notes
        the return in the reading journal."""
        live_psns = {loan["psn_key"] for loan in active_loans}
        for book in self.db.get_all_books() or []:
            psn = getattr(book, "libby_psn_key", None)
            if not psn or psn in live_psns:
                continue
            book.libby_psn_key = None
            self.db.save_book(book)
            try:
                self.db.add_reading_journal(
                    book_id=book.id,
                    event="note",
                    entry="Libby loan ended (returned or expired) — position sync stopped.",
                )
            except Exception as e:
                logger.warning("Libby: could not journal loan end for book %s: %s", book.id, e)
            logger.info("Libby: loan %s gone — unlinked '%s'", psn, book.title)

    # ── Reading-time backfill ──────────────────────────────────────

    def _maybe_journal_reading_time(self, loan: dict):
        """Journal meaningful readingTime deltas, attributed to today."""
        psn = loan["psn_key"]
        current_rt = loan.get("reading_time")
        if current_rt is None:
            return
        previous = self._last_reading_time.get(psn)
        self._last_reading_time[psn] = current_rt
        if previous is None or current_rt <= previous:
            return
        delta = current_rt - previous
        if delta < READING_TIME_DELTA_THRESHOLD:
            return

        book = next((b for b in self.db.get_all_books() or [] if b.libby_psn_key == psn), None)
        if not book:
            return
        minutes = round(delta / 60)
        try:
            self.db.add_reading_journal(
                book_id=book.id,
                event="progress",
                entry=f"Read ~{minutes} min in Libby since last check-in.",
            )
            logger.info("Libby: journalled %d min for '%s'", minutes, book.title)
        except Exception as e:
            logger.warning("Libby: journal write failed for book %s: %s", book.id, e)

    # ── TBR curation (holds → review queue → tbr_items) ────────────

    def sync_hold_suggestions(self):
        """Surface holds in the pending_suggestions review queue. Never
        auto-adds anything to TBR."""
        state = self.libby.get_sync_state()
        if not state:
            return
        cards = {c["id"]: c for c in self.libby._extract_cards(state)}
        for hold in state.get("holds") or []:
            if not isinstance(hold, dict):
                continue
            source_id = f"hold:{hold.get('cardId')}:{hold.get('id')}"
            if any(
                s.source == "libby_hold" and s.source_id == source_id
                for s in (self.db.get_all_pending_suggestions() or [])
            ):
                continue
            card = cards.get(str(hold.get("cardId"))) or {}
            from src.db.models import PendingSuggestion

            self.db.save_pending_suggestion(
                PendingSuggestion(
                    source="libby_hold",
                    source_id=source_id,
                    title=hold.get("title"),
                    author=hold.get("firstCreatorName"),
                    cover_url=None,
                    matches_json=json.dumps({"library": card.get("library"), "title_id": hold.get("id")}),
                )
            )

    def merge_tbr_items(self, suggestion_ids: list[int]) -> int:
        """Explicitly merge reviewed holds into tbr_items. Returns count."""
        merged = 0
        for suggestion in self.db.get_all_pending_suggestions() or []:
            if suggestion.id not in suggestion_ids or suggestion.source != "libby_hold":
                continue
            self.db.add_tbr_item(
                suggestion.title,
                author=suggestion.author,
                cover_url=suggestion.cover_url,
                source="libby",
            )
            suggestion.status = "merged"
            self.db.save_pending_suggestion(suggestion)
            merged += 1
        logger.info("Libby: merged %d hold(s) into TBR", merged)
        return merged

    def dismiss_tbr_items(self, suggestion_ids: list[int]) -> int:
        dismissed = 0
        for suggestion in self.db.get_all_pending_suggestions() or []:
            if suggestion.id not in suggestion_ids or suggestion.source != "libby_hold":
                continue
            suggestion.status = "dismissed"
            self.db.save_pending_suggestion(suggestion)
            dismissed += 1
        return dismissed
