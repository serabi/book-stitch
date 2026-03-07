import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import UTC
from difflib import SequenceMatcher
from pathlib import Path

from src.db.models import PendingSuggestion
from src.utils.string_utils import clean_book_title

logger = logging.getLogger(__name__)


class SuggestionService:
    """Handles suggestion discovery and creation for unmapped books."""

    def __init__(self,
                 database_service,
                 abs_client,
                 booklore_client,
                 storyteller_client,
                 library_service,
                 books_dir,
                 ebook_parser):
        self.database_service = database_service
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.storyteller_client = storyteller_client
        self.library_service = library_service
        self.books_dir = books_dir
        self.ebook_parser = ebook_parser

        self._suggestion_lock = threading.Lock()
        self._suggestion_in_flight: set[str] = set()
        self._rescan_lock = threading.Lock()
        self._rescan_thread: threading.Thread | None = None
        self._rescan_status = {
            "running": False,
            "queued": False,
            "last_started_at": None,
            "last_finished_at": None,
            "phase": "idle",
            "message": "",
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "total": 0,
            "bookfusion_catalog": False,
            "rate_limited": False,
            "next_allowed_in": 0,
        }

    SOURCE_PRIORITY = {
        "booklore": 0.06,
        "cwa": 0.03,
        "filesystem": 0.0,
        "bookfusion": -0.03,
    }

    def _normalize_title(self, title: str | None) -> str:
        if not title:
            return ""
        title = clean_book_title(title)
        title = re.sub(r'\s*[\(\[].*?[\)\]]', '', title)
        title = re.sub(r'\.(epub|mobi|azw3?|pdf|fb2|cbz|cbr|md)$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'[^\w\s]', ' ', title.lower())
        return ' '.join(title.split())

    def _normalize_author(self, author: str | None) -> str:
        if not author:
            return ""
        author = re.sub(r'[^\w\s,]', ' ', author.lower())
        return ' '.join(author.split())

    def _extract_title_numbers(self, normalized_title: str) -> set[str]:
        return {token for token in normalized_title.split() if token.isdigit()}

    def _compute_match_score(self, source_title: str, source_author: str, candidate_title: str, candidate_author: str) -> tuple[float, list[str]]:
        norm_source_title = self._normalize_title(source_title)
        norm_source_author = self._normalize_author(source_author)
        norm_candidate_title = self._normalize_title(candidate_title)
        norm_candidate_author = self._normalize_author(candidate_author)

        if not norm_source_title or not norm_candidate_title:
            return 0.0, []

        title_score = SequenceMatcher(None, norm_source_title, norm_candidate_title).ratio()
        evidence = []

        if norm_source_title == norm_candidate_title:
            title_score = max(title_score, 0.99)
            evidence.append("title_match")
        elif norm_source_title in norm_candidate_title or norm_candidate_title in norm_source_title:
            title_score = max(title_score, 0.92)
            evidence.append("title_partial")

        author_score = 0.0
        if norm_source_author and norm_candidate_author:
            author_score = SequenceMatcher(None, norm_source_author, norm_candidate_author).ratio()
            if norm_source_author == norm_candidate_author:
                author_score = max(author_score, 0.98)
                evidence.append("author_match")
            elif norm_source_author in norm_candidate_author or norm_candidate_author in norm_source_author:
                author_score = max(author_score, 0.88)
                evidence.append("author_partial")

        score = (title_score * 0.8) + (author_score * 0.2 if norm_source_author and norm_candidate_author else 0.0)

        source_numbers = self._extract_title_numbers(norm_source_title)
        candidate_numbers = self._extract_title_numbers(norm_candidate_title)
        if source_numbers != candidate_numbers and (source_numbers or candidate_numbers):
            score -= 0.18
            evidence.append("series_penalty")

        return max(score, 0.0), evidence

    def _score_to_confidence(self, score: float) -> str:
        if score >= 0.93:
            return "high"
        if score >= 0.82:
            return "medium"
        return "low"

    def _get_bookfusion_context(self) -> dict:
        try:
            bf_books = list(self.database_service.get_bookfusion_books() or [])
        except TypeError:
            bf_books = []

        try:
            linked_abs_ids = list(self.database_service.get_bookfusion_linked_abs_ids() or [])
        except TypeError:
            linked_abs_ids = []

        visible_books = [b for b in bf_books if not getattr(b, 'hidden', False)]
        by_title_author = {}
        by_title = {}
        for book in visible_books:
            if book.matched_abs_id:
                continue
            norm_title = self._normalize_title(book.title or book.filename or "")
            norm_author = self._normalize_author(book.authors or "")
            if not norm_title:
                continue
            if norm_author:
                by_title_author.setdefault((norm_title, norm_author), []).append(book)
            by_title.setdefault(norm_title, []).append(book)
        return {
            "books": visible_books,
            "linked_abs_ids": linked_abs_ids,
            "by_title_author": by_title_author,
            "by_title": by_title,
            "has_catalog": bool(visible_books),
        }

    def _update_rescan_status(self, **kwargs) -> None:
        with self._rescan_lock:
            self._rescan_status.update(kwargs)

    def get_rescan_status(self) -> dict:
        with self._rescan_lock:
            status = dict(self._rescan_status)
        min_interval = int(os.environ.get("SUGGESTIONS_RESCAN_MIN_INTERVAL_SECONDS", "300"))
        last_finished_at = status.get("last_finished_at") or 0
        if not status.get("running") and last_finished_at:
            elapsed = max(0, time.time() - last_finished_at)
            status["next_allowed_in"] = max(0, min_interval - int(elapsed))
        return status

    def request_rescan_library_suggestions(self, force: bool = False) -> dict:
        min_interval = int(os.environ.get("SUGGESTIONS_RESCAN_MIN_INTERVAL_SECONDS", "300"))
        with self._rescan_lock:
            if self._rescan_thread and self._rescan_thread.is_alive():
                self._rescan_status["queued"] = False
                self._rescan_status["rate_limited"] = False
                return dict(self._rescan_status)

            last_finished_at = self._rescan_status.get("last_finished_at") or 0
            elapsed = time.time() - last_finished_at if last_finished_at else min_interval
            if not force and elapsed < min_interval:
                self._rescan_status.update({
                    "running": False,
                    "queued": False,
                    "rate_limited": True,
                    "next_allowed_in": max(0, min_interval - int(elapsed)),
                    "message": "Rescan recently completed. Please wait before running it again.",
                })
                return dict(self._rescan_status)

            self._rescan_status.update({
                "running": True,
                "queued": True,
                "rate_limited": False,
                "next_allowed_in": 0,
                "last_started_at": time.time(),
                "phase": "queued",
                "message": "Queued suggestions rescan.",
                "created": 0,
                "updated": 0,
                "deleted": 0,
                "total": 0,
            })
            self._rescan_thread = threading.Thread(
                target=self._run_rescan_job,
                daemon=True,
                name="suggestions-rescan",
            )
            self._rescan_thread.start()
            return dict(self._rescan_status)

    def _build_library_candidates(self, bookfusion_context: dict | None = None, include_filesystem: bool = True) -> list[dict]:
        candidates = []
        seen = set()

        self._update_rescan_status(phase="loading_booklore", message="Loading Booklore candidates...")
        bl_client = self.booklore_client
        if bl_client and bl_client.is_configured():
            try:
                for book in bl_client.get_all_books() or []:
                    filename = book.get('fileName', '')
                    if not filename or not filename.lower().endswith('.epub'):
                        continue
                    dedupe_key = ("booklore", filename.lower())
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    candidates.append({
                        "source_family": "booklore",
                        "source": "booklore",
                        "source_key": f"booklore:{filename}",
                        "title": book.get('title') or Path(filename).stem,
                        "author": book.get('authors') or '',
                        "filename": filename,
                        "id": str(book.get('id') or ''),
                        "action_kind": "create_mapping",
                    })
            except Exception as e:
                logger.warning(f"Booklore cache scan failed during suggestions rescan: {e}")

        if include_filesystem and self.books_dir and self.books_dir.exists():
            try:
                batch_size = max(1, int(os.environ.get("SUGGESTIONS_RESCAN_FS_BATCH_SIZE", "200")))
                pause_ms = max(0, int(os.environ.get("SUGGESTIONS_RESCAN_PAUSE_MS", "20")))
                self._update_rescan_status(phase="loading_filesystem", message="Scanning local EPUB files...")
                for idx, epub in enumerate(self.books_dir.rglob("*.epub"), start=1):
                    dedupe_key = ("filesystem", epub.name.lower())
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    candidates.append({
                        "source_family": "filesystem",
                        "source": "filesystem",
                        "source_key": f"filesystem:{epub.name}",
                        "title": epub.stem,
                        "author": '',
                        "filename": epub.name,
                        "path": str(epub),
                        "action_kind": "create_mapping",
                    })
                    if idx % batch_size == 0:
                        self._update_rescan_status(
                            phase="loading_filesystem",
                            message=f"Scanning local EPUB files... {idx} processed",
                        )
                        if pause_ms:
                            time.sleep(pause_ms / 1000.0)
            except Exception as e:
                logger.warning(f"Filesystem scan failed during suggestions rescan: {e}")

        if bookfusion_context:
            self._update_rescan_status(phase="loading_bookfusion", message="Loading BookFusion candidates...")
            for book in bookfusion_context["books"]:
                dedupe_key = ("bookfusion", book.bookfusion_id)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                highlight_range = self.database_service.get_bookfusion_highlight_date_range([book.bookfusion_id])
                last_highlighted_at = None
                if highlight_range and highlight_range[1]:
                    try:
                        last_highlighted_at = highlight_range[1].astimezone(UTC).isoformat()
                    except Exception:
                        last_highlighted_at = highlight_range[1].isoformat()
                candidates.append({
                    "source_family": "bookfusion",
                    "source": "bookfusion",
                    "source_key": f"bookfusion:{book.bookfusion_id}",
                    "title": book.title or book.filename or '',
                    "author": book.authors or '',
                    "bookfusion_ids": [book.bookfusion_id],
                    "highlight_count": book.highlight_count or 0,
                    "last_highlighted_at": last_highlighted_at,
                    "action_kind": "link_existing",
                })

        return candidates

    def _apply_bookfusion_evidence(self, source_title: str, source_author: str, match: dict, bookfusion_context: dict) -> dict:
        evidence = list(match.get("evidence") or [])
        score = float(match.get("score") or 0.0)
        norm_title = self._normalize_title(source_title)
        norm_author = self._normalize_author(source_author)

        corroborating_books = []
        if norm_title and norm_author:
            corroborating_books.extend(bookfusion_context["by_title_author"].get((norm_title, norm_author), []))
        if not corroborating_books and norm_title:
            corroborating_books.extend(bookfusion_context["by_title"].get(norm_title, []))

        if match.get("source_family") == "bookfusion":
            if match.get("highlight_count", 0) > 0:
                score += min(0.08, 0.02 + (match.get("highlight_count", 0) * 0.01))
                evidence.append("bookfusion_highlights")
            evidence.append("bookfusion_library")
        elif corroborating_books:
            score += 0.04
            evidence.append("bookfusion_library")
            total_highlights = sum((b.highlight_count or 0) for b in corroborating_books)
            if total_highlights:
                score += min(0.06, 0.02 + (total_highlights * 0.01))
                evidence.append("bookfusion_highlights")

        match["score"] = min(score, 0.995)
        match["evidence"] = sorted(set(evidence))
        match["confidence"] = self._score_to_confidence(match["score"])
        return match

    def _rank_candidates_for_book(self, source_title: str, source_author: str, candidates: list[dict], bookfusion_context: dict | None = None) -> list[dict]:
        ranked = []
        for candidate in candidates:
            score, evidence = self._compute_match_score(
                source_title,
                source_author,
                candidate.get("title") or candidate.get("filename") or "",
                candidate.get("author") or "",
            )
            score += self.SOURCE_PRIORITY.get(candidate.get("source_family", ""), 0.0)
            if score < 0.72:
                continue

            match = {
                **candidate,
                "score": min(score, 0.995),
                "confidence": self._score_to_confidence(score),
                "evidence": sorted(set(evidence)),
            }
            if bookfusion_context:
                match = self._apply_bookfusion_evidence(source_title, source_author, match, bookfusion_context)
            ranked.append(match)

        ranked.sort(key=lambda m: (m.get("score", 0.0), m.get("source_family") == "booklore", m.get("highlight_count", 0)), reverse=True)
        return ranked[:6]

    def queue_suggestion(self, abs_id: str) -> None:
        """Queue suggestion discovery for an unmapped book (called from socket listener)."""
        if os.environ.get("SUGGESTIONS_ENABLED", "true").lower() != "true":
            return

        # Already mapped?
        all_books = self.database_service.get_all_books()
        mapped_ids = {b.abs_id for b in all_books}
        if abs_id in mapped_ids:
            return

        if self._suggestion_already_recorded(abs_id):
            return

        logger.info(f"Socket.IO: Queuing suggestion discovery for '{abs_id[:12]}...'")
        self._create_suggestion(abs_id, None)

    def check_for_suggestions(self, abs_progress_map, active_books):
        """Check for unmapped books with progress and create suggestions."""
        suggestions_enabled_val = os.environ.get("SUGGESTIONS_ENABLED", "true")
        logger.debug(f"SUGGESTIONS_ENABLED env var is: '{suggestions_enabled_val}'")

        if suggestions_enabled_val.lower() != "true":
            return

        try:
            # optimization: get all mapped IDs to avoid suggesting existing books (even if inactive)
            all_books = self.database_service.get_all_books()
            mapped_ids = {b.abs_id for b in all_books}

            logger.debug(f"Checking for suggestions: {len(abs_progress_map)} books with progress, {len(mapped_ids)} already mapped")

            for abs_id, item_data in abs_progress_map.items():
                if abs_id in mapped_ids:
                    logger.debug(f"Skipping {abs_id}: already mapped")
                    continue

                duration = item_data.get('duration', 0)
                current_time = item_data.get('currentTime', 0)

                if duration > 0:
                    pct = current_time / duration
                    if pct > 0.01:
                        if self._suggestion_already_recorded(abs_id):
                            logger.debug(f"Skipping {abs_id}: suggestion already exists/hidden")
                            continue

                        # Check if book is already mostly finished (>70%)
                        # If a user has listened to >70% elsewhere, they probably don't need a suggestion
                        if pct > 0.70:
                            logger.debug(f"Skipping {abs_id}: progress {pct:.1%} > 70% threshold")
                            continue

                        logger.debug(f"Creating suggestion for {abs_id} (progress: {pct:.1%})")
                        self._create_suggestion(abs_id, item_data)
                    else:
                        logger.debug(f"Skipping {abs_id}: progress {pct:.1%} below 1% threshold")
                else:
                    logger.debug(f"Skipping {abs_id}: no duration")
        except Exception as e:
            logger.error(f"Error checking suggestions: {e}")

        # Reverse suggestions: ebook sources → ABS audiobooks
        try:
            self._check_reverse_suggestions()
        except Exception as e:
            logger.warning(f"Reverse suggestions check failed: {e}")

    def _suggestion_already_recorded(self, abs_id: str) -> bool:
        """Return True when a suggestion should not be recreated for this ABS item."""
        return bool(self.database_service.suggestion_exists(abs_id))

    def _check_reverse_suggestions(self):
        """Check Storyteller and Booklore for books with progress that could match ABS audiobooks."""
        if not self.abs_client:
            return

        # Build lookup of ABS audiobooks by cleaned title for matching
        try:
            all_audiobooks = self.abs_client.get_all_audiobooks()
        except Exception as e:
            logger.debug(f"Reverse suggestions: failed to fetch ABS audiobooks: {e}")
            return

        if not all_audiobooks:
            return

        all_books = self.database_service.get_all_books()
        mapped_abs_ids = {b.abs_id for b in all_books}
        mapped_storyteller_uuids = {b.storyteller_uuid for b in all_books if b.storyteller_uuid}

        # Index audiobooks by cleaned title for fuzzy matching
        abs_by_title: dict[str, list[dict]] = {}
        for ab in all_audiobooks:
            meta = ab.get('media', {}).get('metadata', {})
            title = meta.get('title', '')
            if title:
                clean = re.sub(r'\s*[\(\[].*?[\)\]]', '', title).strip().lower()
                if clean:
                    abs_by_title.setdefault(clean, []).append(ab)

        # Check Storyteller books
        if self.storyteller_client and self.storyteller_client.is_configured():
            try:
                positions = self.storyteller_client.get_all_positions_bulk()
                for title_lower, pos_data in positions.items():
                    pct = pos_data.get('pct', 0)
                    uuid = pos_data.get('uuid')
                    if not uuid or pct < 0.01 or pct > 0.70:
                        continue
                    if uuid in mapped_storyteller_uuids:
                        continue

                    # Search ABS for a matching audiobook
                    clean_title = re.sub(r'\s*[\(\[].*?[\)\]]', '', title_lower).strip().lower()
                    matches = self._find_abs_audiobook_matches(clean_title, abs_by_title, mapped_abs_ids)
                    if matches:
                        self._save_reverse_suggestion(matches, clean_title, f"storyteller:{uuid}")
            except Exception as e:
                logger.debug(f"Reverse suggestions: Storyteller check failed: {e}")

        # Check Booklore books
        if self.booklore_client and self.booklore_client.is_configured():
            try:
                bl_books = self.booklore_client.get_all_books()
                for bl_book in bl_books:
                    title = bl_book.get('title', '')
                    filename = bl_book.get('fileName', '')
                    if not title:
                        continue

                    pct_raw, _ = self.booklore_client.get_progress(filename)
                    if not pct_raw or pct_raw < 0.01 or pct_raw > 0.70:
                        continue

                    clean_title = re.sub(r'\s*[\(\[].*?[\)\]]', '', title).strip().lower()
                    source_key = f"booklore:{filename}"
                    matches = self._find_abs_audiobook_matches(clean_title, abs_by_title, mapped_abs_ids)
                    if matches:
                        self._save_reverse_suggestion(matches, title, source_key)
            except Exception as e:
                logger.debug(f"Reverse suggestions: Booklore check failed: {e}")

    def _find_abs_audiobook_matches(self, clean_title: str, abs_by_title: dict, mapped_abs_ids: set) -> list[dict]:
        """Find ABS audiobooks matching a title, excluding already-mapped ones."""
        if not clean_title:
            return []
        matches = []
        for indexed_title, audiobooks in abs_by_title.items():
            # Check for substring match in either direction
            if clean_title in indexed_title or indexed_title in clean_title:
                for ab in audiobooks:
                    ab_id = ab.get('id')
                    if ab_id in mapped_abs_ids:
                        continue
                    meta = ab.get('media', {}).get('metadata', {})
                    matches.append({
                        "source": "abs_audiobook",
                        "abs_id": ab_id,
                        "title": meta.get('title'),
                        "author": meta.get('authorName'),
                        "confidence": "high" if clean_title == indexed_title else "medium",
                    })
        return matches

    def _save_reverse_suggestion(self, matches: list[dict], title: str, source_key: str):
        """Save a reverse suggestion (ebook → audiobook) using the first ABS match as source_id."""
        # Use the best ABS match as the anchor
        best = next((m for m in matches if m.get('confidence') == 'high'), matches[0])
        abs_id = best['abs_id']

        if self.database_service.is_suggestion_ignored(abs_id):
            return

        cover = f"/api/cover-proxy/{abs_id}"
        # Include source_key as provenance so we know where the suggestion originated
        matches_with_provenance = [dict(m, source_key=source_key) for m in matches]
        existing = self.database_service.get_pending_suggestion(abs_id) or self.database_service.get_suggestion(abs_id)
        merged_matches = []
        merged_index = {}

        def _match_key(match):
            return (
                match.get('abs_id'),
                match.get('source_key'),
                match.get('title'),
                match.get('author'),
            )

        for match in (existing.matches if existing else []) + matches_with_provenance:
            key = _match_key(match)
            prior = merged_index.get(key)
            if prior is None:
                merged_index[key] = len(merged_matches)
                merged_matches.append(dict(match))
                continue

            current = merged_matches[prior]
            merged_matches[prior] = {
                **current,
                **{k: v for k, v in match.items() if v not in (None, '')},
            }

        suggestion = PendingSuggestion(
            source_id=abs_id,
            title=(existing.title if existing and existing.title else best.get('title', title)),
            author=(existing.author if existing and existing.author else best.get('author')),
            cover_url=(existing.cover_url if existing and existing.cover_url else cover),
            matches_json=json.dumps(merged_matches),
        )
        self.database_service.save_pending_suggestion(suggestion)
        logger.info(f"Reverse suggestion: '{title}' has matching audiobook '{best.get('title')}' in ABS")

    def _run_rescan_job(self) -> None:
        try:
            self._update_rescan_status(
                running=True,
                queued=False,
                phase="starting",
                message="Preparing suggestions rescan...",
                rate_limited=False,
            )
            stats = self.rescan_library_suggestions()
            self._update_rescan_status(
                running=False,
                queued=False,
                phase="complete",
                message=f"Rescan complete. {stats['total']} suggestion(s) available.",
                last_finished_at=time.time(),
                **stats,
            )
        except Exception as e:
            logger.error(f"Suggestions background rescan failed: {e}")
            logger.debug(traceback.format_exc())
            self._update_rescan_status(
                running=False,
                queued=False,
                phase="error",
                message=str(e),
                last_finished_at=time.time(),
            )

    def rescan_library_suggestions(self) -> dict:
        """Rebuild suggestions from cached library metadata without live BookFusion calls."""
        if os.environ.get("SUGGESTIONS_ENABLED", "true").lower() != "true":
            return {"created": 0, "updated": 0, "deleted": 0, "total": 0, "bookfusion_catalog": False}

        if not self.abs_client:
            return {"created": 0, "updated": 0, "deleted": 0, "total": 0, "bookfusion_catalog": False}

        try:
            self._update_rescan_status(phase="loading_abs", message="Loading ABS audiobooks...")
            all_abs_books = self.abs_client.get_all_audiobooks() or []
        except Exception as e:
            logger.warning(f"Suggestions rescan failed to load ABS audiobooks: {e}")
            return {"created": 0, "updated": 0, "deleted": 0, "total": 0, "bookfusion_catalog": False}

        mapped_ids = {b.abs_id for b in self.database_service.get_all_books()}
        existing_actionable = {
            s.source_id: s for s in self.database_service.get_all_actionable_suggestions()
            if getattr(s, 'source', 'abs') == 'abs'
        }
        bookfusion_context = self._get_bookfusion_context()
        candidates = self._build_library_candidates(bookfusion_context=bookfusion_context, include_filesystem=True)

        created = 0
        updated = 0
        kept_ids = set()
        total_books = len(all_abs_books)

        self._update_rescan_status(phase="scoring", message=f"Scoring {total_books} ABS books...")
        for idx, abs_book in enumerate(all_abs_books, start=1):
            abs_id = abs_book.get('id')
            if not abs_id or abs_id in mapped_ids or self.database_service.is_suggestion_ignored(abs_id):
                continue

            meta = abs_book.get('media', {}).get('metadata', {})
            title = meta.get('title') or ''
            author = meta.get('authorName') or ''
            matches = self._rank_candidates_for_book(title, author, candidates, bookfusion_context=bookfusion_context)

            if not matches:
                continue

            kept_ids.add(abs_id)
            existing = existing_actionable.get(abs_id)
            suggestion = PendingSuggestion(
                source_id=abs_id,
                title=title,
                author=author,
                cover_url=f"/api/cover-proxy/{abs_id}",
                matches_json=json.dumps(matches),
                status='hidden' if existing and getattr(existing, 'status', None) in ('hidden', 'dismissed') else 'pending',
            )
            if abs_id in existing_actionable:
                updated += 1
            else:
                created += 1
            self.database_service.save_pending_suggestion(suggestion)

            if idx % 25 == 0:
                self._update_rescan_status(
                    phase="scoring",
                    message=f"Scoring ABS books... {idx}/{total_books}",
                    created=created,
                    updated=updated,
                )
                time.sleep(0.01)

        deleted = 0
        self._update_rescan_status(phase="cleanup", message="Cleaning stale suggestions...")
        for source_id in list(existing_actionable.keys()):
            if source_id not in kept_ids:
                if self.database_service.resolve_suggestion(source_id):
                    deleted += 1

        total = len(self.database_service.get_all_actionable_suggestions())
        logger.info(
            "Suggestions rescan completed: created=%s updated=%s deleted=%s total=%s",
            created, updated, deleted, total
        )
        return {
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "total": total,
            "bookfusion_catalog": bookfusion_context["has_catalog"],
        }

    def _create_suggestion(self, abs_id, progress_data):
        """Create a new suggestion for an unmapped book."""
        with self._suggestion_lock:
            if abs_id in self._suggestion_in_flight:
                return
            self._suggestion_in_flight.add(abs_id)

        try:
            logger.info(f"Found potential new book for suggestion: '{abs_id}'")
            # 1. Get Details from ABS
            item = self.abs_client.get_item_details(abs_id)
            if not item:
                logger.debug(f"Suggestion failed: Could not get details for {abs_id}")
                return

            media = item.get('media', {})
            metadata = media.get('metadata', {})
            title = metadata.get('title') or ''
            author = metadata.get('authorName') or ''
            cover = f"/api/cover-proxy/{abs_id}"
            logger.debug(f"Checking suggestions for '{title}' (Author: {author})")

            bookfusion_context = self._get_bookfusion_context()
            matches = self._rank_candidates_for_book(
                title,
                author,
                self._build_library_candidates(bookfusion_context=bookfusion_context),
                bookfusion_context=bookfusion_context,
            )

            query = title
            if author:
                query = f"{query} {author}"

            if self.booklore_client and self.booklore_client.is_configured():
                try:
                    live_results = self.booklore_client.search_books(query) or []
                    live_candidates = []
                    for book in live_results:
                        filename = book.get("fileName", "")
                        if not filename or not filename.lower().endswith(".epub"):
                            continue
                        live_candidates.append({
                            "source_family": "booklore",
                            "source": "booklore",
                            "source_key": f"booklore:{filename}",
                            "title": book.get("title") or Path(filename).stem,
                            "author": book.get("authors") or "",
                            "filename": filename,
                            "id": str(book.get("id") or ""),
                            "action_kind": "create_mapping",
                        })
                    matches.extend(
                        self._rank_candidates_for_book(
                            title,
                            author,
                            live_candidates,
                            bookfusion_context=bookfusion_context,
                        )
                    )
                except Exception as e:
                    logger.warning(f"Booklore live search failed during suggestion: {e}")

            if self.library_service and self.library_service.cwa_client and self.library_service.cwa_client.is_configured():
                try:
                    cwa_results = self.library_service.cwa_client.search_ebooks(query)
                    for cr in cwa_results or []:
                        cwa_candidate = {
                            "source_family": "cwa",
                            "source": "cwa",
                            "source_key": f"cwa:{cr.get('id')}",
                            "title": cr.get('title'),
                            "author": cr.get('author'),
                            "filename": f"cwa_{cr.get('id', 'unknown')}.{cr.get('ext', 'epub')}",
                            "action_kind": "create_mapping",
                        }
                        cwa_ranked = self._rank_candidates_for_book(
                            title, author, [cwa_candidate], bookfusion_context=bookfusion_context
                        )
                        matches.extend(cwa_ranked)
                except Exception as e:
                    logger.warning(f"CWA search failed during suggestion: {e}")

            deduped = {}
            for match in matches:
                key = match.get("source_key") or match.get("filename") or match.get("title")
                if not key:
                    continue
                existing = deduped.get(key)
                if not existing or match.get("score", 0) > existing.get("score", 0):
                    deduped[key] = match
            matches = sorted(deduped.values(), key=lambda m: m.get("score", 0.0), reverse=True)[:6]

            # 3. Save to DB
            if not matches:
                logger.debug(f"No matches found for '{title}', skipping suggestion creation")
                return

            suggestion = PendingSuggestion(
                source_id=abs_id,
                title=title,
                author=author,
                cover_url=cover,
                matches_json=json.dumps(matches)
            )
            self.database_service.save_pending_suggestion(suggestion)
            match_count = len(matches)
            logger.info(f"Created suggestion for '{title}' with {match_count} matches")

        except Exception as e:
            logger.error(f"Failed to create suggestion for '{abs_id}': {e}")
            logger.debug(traceback.format_exc())
        finally:
            with self._suggestion_lock:
                self._suggestion_in_flight.discard(abs_id)
