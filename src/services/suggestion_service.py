import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

from src.api.grimmory_client import GRIMMORY_AUDIO_BOOK_TYPES
from src.db.models import DetectedBook, PendingSuggestion
from src.utils.string_utils import clean_book_title

logger = logging.getLogger(__name__)


# Match scoring thresholds
_TITLE_EXACT_MATCH_FLOOR = 0.99
_TITLE_PARTIAL_MATCH_FLOOR = 0.92
_AUTHOR_EXACT_MATCH_FLOOR = 0.98
_AUTHOR_PARTIAL_MATCH_FLOOR = 0.88
_TITLE_WEIGHT = 0.8
_AUTHOR_WEIGHT = 0.2
_SERIES_NUMBER_PENALTY = 0.18
_CONFIDENCE_HIGH_THRESHOLD = 0.93
_CONFIDENCE_MEDIUM_THRESHOLD = 0.82
_MIN_CANDIDATE_SCORE = 0.72

# Detection progress window (as fractions): a book is only surfaced for detection
# when its external progress falls within these bounds. The lower bound filters
# out untouched books; the upper bound avoids surfacing near-finished books.
_DETECTION_WINDOW_MIN = 0.01
_DETECTION_WINDOW_MAX = 0.95


class SuggestionService:
    """Handles detected-book discovery plus legacy suggestion workflows."""

    def __init__(
        self,
        database_service,
        abs_client,
        grimmory_client,
        storyteller_client,
        library_service,
        books_dir,
        ebook_parser,
    ):
        self.database_service = database_service
        self.abs_client = abs_client
        self.grimmory_client = grimmory_client
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
        "grimmory": 0.06,
        "cwa": 0.03,
        "filesystem": 0.0,
        "bookfusion": -0.03,
    }

    def _suggestions_disabled(self) -> bool:
        return os.environ.get("SUGGESTIONS_ENABLED", "true").lower() != "true"

    def _normalize_title(self, title: str | None) -> str:
        if not title:
            return ""
        title = clean_book_title(title)
        title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title)
        title = re.sub(r"\.(epub|mobi|azw3?|pdf|fb2|cbz|cbr|md)$", "", title, flags=re.IGNORECASE)
        title = re.sub(r"[^\w\s]", " ", title.lower())
        return " ".join(title.split())

    def _normalize_author(self, author: str | None) -> str:
        if not author:
            return ""
        author = re.sub(r"[^\w\s,]", " ", author.lower())
        return " ".join(author.split())

    def _extract_title_numbers(self, normalized_title: str) -> set[str]:
        return {token for token in normalized_title.split() if token.isdigit()}

    def _compute_match_score(
        self, source_title: str, source_author: str, candidate_title: str, candidate_author: str
    ) -> tuple[float, list[str]]:
        norm_source_title = self._normalize_title(source_title)
        norm_source_author = self._normalize_author(source_author)
        norm_candidate_title = self._normalize_title(candidate_title)
        norm_candidate_author = self._normalize_author(candidate_author)

        if not norm_source_title or not norm_candidate_title:
            return 0.0, []

        title_score = SequenceMatcher(None, norm_source_title, norm_candidate_title).ratio()
        evidence = []

        if norm_source_title == norm_candidate_title:
            title_score = max(title_score, _TITLE_EXACT_MATCH_FLOOR)
            evidence.append("title_match")
        elif norm_source_title in norm_candidate_title or norm_candidate_title in norm_source_title:
            title_score = max(title_score, _TITLE_PARTIAL_MATCH_FLOOR)
            evidence.append("title_partial")

        author_score = 0.0
        if norm_source_author and norm_candidate_author:
            author_score = SequenceMatcher(None, norm_source_author, norm_candidate_author).ratio()
            if norm_source_author == norm_candidate_author:
                author_score = max(author_score, _AUTHOR_EXACT_MATCH_FLOOR)
                evidence.append("author_match")
            elif norm_source_author in norm_candidate_author or norm_candidate_author in norm_source_author:
                author_score = max(author_score, _AUTHOR_PARTIAL_MATCH_FLOOR)
                evidence.append("author_partial")

        has_author = norm_source_author and norm_candidate_author
        score = (title_score * _TITLE_WEIGHT) + (author_score * _AUTHOR_WEIGHT) if has_author else title_score

        source_numbers = self._extract_title_numbers(norm_source_title)
        candidate_numbers = self._extract_title_numbers(norm_candidate_title)
        if source_numbers != candidate_numbers and (source_numbers or candidate_numbers):
            score -= _SERIES_NUMBER_PENALTY
            evidence.append("series_penalty")

        return max(score, 0.0), evidence

    def _score_to_confidence(self, score: float) -> str:
        if score >= _CONFIDENCE_HIGH_THRESHOLD:
            return "high"
        if score >= _CONFIDENCE_MEDIUM_THRESHOLD:
            return "medium"
        return "low"

    def _upsert_detected_book(
        self,
        *,
        source: str,
        source_id: str,
        title: str,
        progress_percentage: float,
        author: str = "",
        cover_url: str | None = None,
        matches: list[dict] | None = None,
        device: str | None = None,
        ebook_filename: str | None = None,
        media_format: str | None = None,
        source_updated_at=None,
    ):
        # Pass NULL (not "[]") for empty matches so an upsert from a no-match code
        # path never clobbers cross-source matches recorded by an earlier pass.
        matches_json = json.dumps(matches) if matches else None
        detected = DetectedBook(
            source=source,
            source_id=source_id,
            title=title or source_id,
            author=author or "",
            cover_url=cover_url,
            progress_percentage=max(0.0, min(progress_percentage, 1.0)),
            matches_json=matches_json,
            device=device,
            ebook_filename=ebook_filename,
            source_updated_at=self._source_datetime(source_updated_at),
            media_format=media_format or "ebook",
        )
        return self.database_service.save_detected_book(detected)

    @staticmethod
    def _source_datetime(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp, UTC)
            except (OSError, OverflowError, ValueError):
                return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None

    def _get_bookfusion_context(self) -> dict:
        try:
            bf_books = list(self.database_service.get_bookfusion_books() or [])
        except TypeError:
            bf_books = []

        try:
            linked_book_ids = list(self.database_service.get_bookfusion_linked_book_ids() or [])
        except TypeError:
            linked_book_ids = []

        visible_books = [b for b in bf_books if not getattr(b, "hidden", False)]
        by_title_author = {}
        by_title = {}
        for book in visible_books:
            if book.matched_book_id:
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
            "linked_book_ids": linked_book_ids,
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

    def request_rescan_library_suggestions(self, force: bool = False, catalog: bool = False) -> dict:
        min_interval = int(os.environ.get("SUGGESTIONS_RESCAN_MIN_INTERVAL_SECONDS", "300"))
        with self._rescan_lock:
            if self._rescan_thread and self._rescan_thread.is_alive():
                self._rescan_status["queued"] = False
                self._rescan_status["rate_limited"] = False
                return dict(self._rescan_status)

            last_finished_at = self._rescan_status.get("last_finished_at") or 0
            elapsed = time.time() - last_finished_at if last_finished_at else min_interval
            if not force and elapsed < min_interval:
                self._rescan_status.update(
                    {
                        "running": False,
                        "queued": False,
                        "rate_limited": True,
                        "next_allowed_in": max(0, min_interval - int(elapsed)),
                        "message": "Rescan recently completed. Please wait before running it again.",
                    }
                )
                return dict(self._rescan_status)

            self._rescan_status.update(
                {
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
                }
            )
            self._rescan_thread = threading.Thread(
                target=self._run_rescan_job,
                args=(catalog,),
                daemon=True,
                name="suggestions-rescan",
            )
            self._rescan_thread.start()
            return dict(self._rescan_status)

    def _build_library_candidates(
        self,
        bookfusion_context: dict | None = None,
        include_filesystem: bool = True,
        errors: set[str] | None = None,
    ) -> list[dict]:
        candidates = []
        seen = set()

        self._update_rescan_status(phase="loading_grimmory", message="Loading Grimmory candidates...")
        bl_client = self.grimmory_client
        if bl_client and bl_client.is_configured():
            try:
                for book in bl_client.get_all_books() or []:
                    try:
                        filename = book.get("fileName", "")
                        if not filename or book.get("id") is None:
                            continue
                        if not book.get("bookFileId"):
                            continue
                        instance_id, source_id = self._grimmory_identity(book)
                        dedupe_key = ("grimmory", source_id.lower())
                        if dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)
                        candidates.append(
                            {
                                "source_family": "grimmory",
                                "source": "grimmory",
                                "source_key": f"grimmory:{source_id}",
                                "title": book.get("title") or Path(filename).stem,
                                "author": book.get("authors") or "",
                                "filename": filename,
                                "id": source_id,
                                "action_kind": "create_mapping",
                                "media_format": self._grimmory_media_format(book),
                            }
                        )
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"Grimmory cache scan failed during suggestions rescan: {e}")
                if errors is not None:
                    errors.add("Grimmory")

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
                    candidates.append(
                        {
                            "source_family": "filesystem",
                            "source": "filesystem",
                            "source_key": f"filesystem:{epub.name}",
                            "title": epub.stem,
                            "author": "",
                            "filename": epub.name,
                            "path": str(epub),
                            "action_kind": "create_mapping",
                            "media_format": "ebook",
                        }
                    )
                    if idx % batch_size == 0:
                        self._update_rescan_status(
                            phase="loading_filesystem",
                            message=f"Scanning local EPUB files... {idx} processed",
                        )
                        if pause_ms:
                            time.sleep(pause_ms / 1000.0)
            except Exception as e:
                logger.warning(f"Filesystem scan failed during suggestions rescan: {e}")
                if errors is not None:
                    errors.add("Local EPUB files")

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
                candidates.append(
                    {
                        "source_family": "bookfusion",
                        "source": "bookfusion",
                        "source_key": f"bookfusion:{book.bookfusion_id}",
                        "title": book.title or book.filename or "",
                        "author": book.authors or "",
                        "bookfusion_ids": [book.bookfusion_id],
                        "highlight_count": book.highlight_count or 0,
                        "last_highlighted_at": last_highlighted_at,
                        "action_kind": "link_existing",
                        "media_format": "ebook",
                    }
                )

        return candidates

    def _apply_bookfusion_evidence(
        self, source_title: str, source_author: str, match: dict, bookfusion_context: dict
    ) -> dict:
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

    def _rank_candidates_for_book(
        self,
        source_title: str,
        source_author: str,
        candidates: list[dict],
        bookfusion_context: dict | None = None,
        source_media_format: str | None = None,
    ) -> list[dict]:
        ranked = []
        for candidate in candidates:
            candidate_format = candidate.get("media_format")
            if source_media_format and (
                candidate_format not in {"audiobook", "ebook"} or candidate_format == source_media_format
            ):
                continue
            score, evidence = self._compute_match_score(
                source_title,
                source_author,
                candidate.get("title") or candidate.get("filename") or "",
                candidate.get("author") or "",
            )
            score += self.SOURCE_PRIORITY.get(candidate.get("source_family", ""), 0.0)
            if score < _MIN_CANDIDATE_SCORE:
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

        ranked.sort(
            key=lambda m: (
                m.get("score", 0.0),
                m.get("source_family") == "grimmory",
                m.get("highlight_count", 0),
            ),
            reverse=True,
        )
        return ranked[:6]

    def queue_suggestion(self, abs_id: str, progress_data: dict | None = None) -> None:
        """Queue detected-book discovery for an unmapped ABS item."""
        # Already mapped?
        all_books = self.database_service.get_all_books()
        mapped_ids = {b.abs_id for b in all_books}
        if abs_id in mapped_ids:
            return

        pct = self._progress_fraction(progress_data)
        if pct is None and self.abs_client:
            progress_data = self.abs_client.get_progress(abs_id)
            pct = self._progress_fraction(progress_data)
        if not self._in_detection_window(pct):
            return

        if self._detected_book_is_terminal(abs_id, source="abs"):
            return

        logger.info(f"Socket.IO: Queuing detected-book discovery for '{abs_id[:12]}...'")
        self._create_suggestion(abs_id, progress_data)

    def queue_kosync_suggestion(
        self,
        doc_hash: str,
        filename: str | None = None,
        device: str | None = None,
        progress_percentage: float = 0.0,
        source_updated_at=None,
    ) -> None:
        """Create or refresh a detected entry for a KoSync document."""
        if not self._in_detection_window(progress_percentage):
            return
        title = ""
        if filename:
            title = Path(filename).stem
            title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()
        if not title and device:
            title = device

        if not title:
            logger.debug(f"KoSync suggestion: no title derivable for {doc_hash[:8]}..., skipping")
            return

        matches = []
        if self.abs_client:
            try:
                all_audiobooks = self.abs_client.get_all_audiobooks() or []
            except Exception as e:
                logger.debug(f"KoSync suggestion: failed to fetch ABS audiobooks: {e}")
                all_audiobooks = []

            if all_audiobooks:
                all_books = self.database_service.get_all_books()
                mapped_abs_ids = {b.abs_id for b in all_books}
                abs_by_title: dict[str, list[dict]] = {}
                for ab in all_audiobooks:
                    meta = ab.get("media", {}).get("metadata", {})
                    ab_title = meta.get("title", "")
                    if ab_title:
                        clean = self._normalize_title(ab_title)
                        if clean:
                            abs_by_title.setdefault(clean, []).append(ab)

                clean_title = self._normalize_title(title)
                matches = self._find_abs_audiobook_matches(clean_title, abs_by_title, mapped_abs_ids)

        # Fallback: Grimmory audiobook companions.
        if not matches:
            ebook_candidates = self._build_ebook_source_candidates()
            matches = self._rank_candidates_for_book(
                title,
                "",
                ebook_candidates.get("grimmory", []),
                source_media_format="ebook",
            )

        cover = None
        author = ""
        if matches:
            best = matches[0]
            author = best.get("author") or best.get("authorName") or ""
            if best.get("abs_id"):
                cover = f"/api/cover-proxy/{best['abs_id']}"
            else:
                cover = best.get("cover_url") or self._cover_url_for(
                    best.get("source_family", ""), best.get("abs_id", ""), best
                )

        self._upsert_detected_book(
            source="kosync",
            source_id=doc_hash,
            title=title,
            author=author,
            cover_url=cover,
            progress_percentage=progress_percentage,
            matches=matches,
            device=device,
            ebook_filename=filename,
            media_format="ebook",
            source_updated_at=source_updated_at,
        )
        logger.info(
            f"KoSync detected: '{title}' (hash {doc_hash[:8]}...)"
            + (f" with {len(matches)} match(es)" if matches else "")
        )

    def check_for_suggestions(self, abs_progress_map, active_books, errors: set[str] | None = None):
        """Check for unmapped books with progress and create detected entries."""
        errors = errors if errors is not None else set()
        try:
            # optimization: get all mapped IDs to avoid suggesting existing books (even if inactive)
            all_books = self.database_service.get_all_books()
            mapped_ids = {b.abs_id for b in all_books}

            logger.debug(
                f"Checking for detected ABS books: {len(abs_progress_map)} books with progress, {len(mapped_ids)} already mapped"
            )

            for abs_id, item_data in abs_progress_map.items():
                if item_data.get("mediaType") not in (None, "audiobook"):
                    continue
                if abs_id in mapped_ids:
                    logger.debug(f"Skipping {abs_id}: already mapped")
                    continue

                duration = item_data.get("duration", 0)
                current_time = item_data.get("currentTime", 0)

                if duration > 0:
                    pct = current_time / duration
                    if self._in_detection_window(pct):
                        if self._detected_book_is_terminal(abs_id, source="abs"):
                            logger.debug(f"Skipping {abs_id}: detected entry is terminal")
                            continue
                        logger.debug(f"Creating detected entry for {abs_id} (progress: {pct:.1%})")
                        if self._create_suggestion(abs_id, item_data) is False:
                            errors.add("Audiobookshelf")
                    else:
                        logger.debug(f"Skipping {abs_id}: progress {pct:.1%} outside detection window")
                else:
                    logger.debug(f"Skipping {abs_id}: no duration")
        except Exception as e:
            logger.error(f"Error checking detected ABS books: {e}")
            errors.add("Audiobookshelf")

        # Reverse suggestions: ebook sources → ABS audiobooks
        abs_audiobooks = []
        try:
            abs_audiobooks = self._check_reverse_suggestions(errors)
        except Exception as e:
            logger.warning(f"Reverse suggestions check failed: {e}")
            errors.add("Audiobookshelf")

        # Cross-ebook suggestions: Storyteller <-> Grimmory <-> KoSync
        try:
            self._check_cross_ebook_suggestions(errors, abs_audiobooks)
        except Exception as e:
            logger.warning(f"Cross-ebook suggestions check failed: {e}")
            errors.add("ebook sources")
        return errors

    def _detected_book_is_terminal(self, source_id: str, source: str = "abs") -> bool:
        """Return True when a dismissed or resolved detection should stay hidden."""
        detected = self.database_service.get_detected_book(source_id, source=source)
        return bool(detected and detected.status in {"dismissed", "resolved"})

    @staticmethod
    def _progress_fraction(progress_data: dict | None) -> float | None:
        if not isinstance(progress_data, dict):
            return None
        value = progress_data.get("progress")
        if isinstance(value, (int, float)):
            return float(value)
        duration = progress_data.get("duration")
        current_time = progress_data.get("currentTime")
        if isinstance(duration, (int, float)) and duration > 0 and isinstance(current_time, (int, float)):
            return current_time / duration
        return None

    def _get_storyteller_books_with_progress(
        self, mapped_uuids: set | None = None, errors: set[str] | None = None
    ) -> list[dict]:
        """Fetch Storyteller books with 1-70% progress, excluding already-mapped UUIDs."""
        if not self.storyteller_client or not self.storyteller_client.is_configured():
            return []
        try:
            positions = self.storyteller_client.get_all_positions_bulk()
        except Exception as e:
            logger.debug(f"Storyteller progress fetch failed: {e}")
            if errors is not None:
                errors.add("Storyteller")
            return []

        results = []
        for title_lower, pos_data in positions.items():
            try:
                pct = float(pos_data.get("pct", 0))
                uuid = pos_data.get("uuid")
                if not uuid or not self._in_detection_window(pct):
                    continue
                if mapped_uuids and uuid in mapped_uuids:
                    continue
                results.append(
                    {
                        "uuid": uuid,
                        "title": title_lower,
                        "author": "",
                        "pct": pct,
                        "source_updated_at": pos_data.get("ts"),
                        "cover_url": pos_data.get("cover_url", ""),
                        "media_format": "ebook",
                    }
                )
            except Exception:
                continue
        return results

    @staticmethod
    def _grimmory_media_format(book: dict) -> str:
        book_type = str(book.get("bookType") or "").upper()
        if book_type in GRIMMORY_AUDIO_BOOK_TYPES:
            return "audiobook"
        return "ebook"

    @classmethod
    def _grimmory_identity(cls, book: dict) -> tuple[str, str]:
        instance_id = str(book.get("_instance_id") or "default")
        filename = str(book.get("fileName") or "")
        if book.get("id") is not None and book.get("bookFileId") is not None:
            return instance_id, f"{instance_id}:{book.get('id')}:{book.get('bookFileId')}"
        return instance_id, f"{instance_id}:{filename}"

    @staticmethod
    def _in_detection_window(pct: float | None) -> bool:
        """Return True when progress falls within the detection window (see module constants)."""
        return bool(pct) and _DETECTION_WINDOW_MIN <= pct <= _DETECTION_WINDOW_MAX

    def _get_grimmory_books_with_progress(
        self, mapped_books: set | None = None, errors: set[str] | None = None
    ) -> list[dict]:
        """Fetch Grimmory books within the detection window, excluding mapped instance/filename pairs."""
        if not self.grimmory_client or not self.grimmory_client.is_configured():
            return []
        try:
            bl_books = self.grimmory_client.get_all_books()
        except Exception as e:
            logger.debug(f"Grimmory book fetch failed: {e}")
            if errors is not None:
                errors.add("Grimmory")
            return []

        results = []
        outside_window_count = 0
        for bl_book in bl_books:
            try:
                title = bl_book.get("title", "")
                filename = bl_book.get("fileName", "")
                if not title or not filename:
                    continue
                if not bl_book.get("bookFileId"):
                    continue
                instance_id, source_id = self._grimmory_identity(bl_book)
                media_format = self._grimmory_media_format(bl_book)
                if mapped_books and source_id in mapped_books:
                    continue
                try:
                    pct_raw, _ = self.grimmory_client.get_book_file_progress(source_id)
                except Exception:
                    if errors is not None:
                        errors.add("Grimmory")
                    continue
                pct = float(pct_raw or 0)
                if not self._in_detection_window(pct):
                    outside_window_count += 1
                    continue
                results.append(
                    {
                        "filename": filename,
                        "source_id": source_id,
                        "instance_id": instance_id,
                        "title": title,
                        "author": bl_book.get("authors", ""),
                        "pct": pct,
                        "id": source_id,
                        "source_updated_at": bl_book.get("lastReadTime"),
                        "media_format": media_format,
                    }
                )
            except Exception:
                continue
        if outside_window_count:
            logger.debug(
                "Grimmory detection: excluded %d books outside the %.0f%%-%.0f%% progress window; retained %d",
                outside_window_count,
                _DETECTION_WINDOW_MIN * 100,
                _DETECTION_WINDOW_MAX * 100,
                len(results),
            )
        return results

    def _get_kosync_books_with_progress(
        self, mapped_hashes: set | None = None, errors: set[str] | None = None
    ) -> list[dict]:
        try:
            documents = self.database_service.get_unlinked_kosync_documents()
        except Exception as e:
            logger.debug(f"KoSync progress fetch failed: {e}")
            if errors is not None:
                errors.add("KoSync")
            return []

        results = []
        for doc in documents:
            try:
                if (mapped_hashes and doc.document_hash in mapped_hashes) or getattr(doc, "linked_abs_id", None):
                    continue
                pct = float(doc.percentage or 0)
                if not doc.filename or not self._in_detection_window(pct):
                    continue
                results.append(
                    {
                        "source_id": doc.document_hash,
                        "filename": doc.filename,
                        "title": Path(doc.filename).stem,
                        "author": "",
                        "pct": pct,
                        "device": doc.device,
                        "source_updated_at": doc.timestamp,
                        "media_format": "ebook",
                    }
                )
            except Exception:
                continue
        return results

    def _build_ebook_source_candidates(self, errors: set[str] | None = None) -> dict[str, list[dict]]:
        """Build per-source candidate lists from Storyteller, Grimmory, and KoSync."""
        candidates: dict[str, list[dict]] = {"storyteller": [], "grimmory": [], "kosync": []}

        if self.storyteller_client and self.storyteller_client.is_configured():
            try:
                positions = self.storyteller_client.get_all_positions_bulk()
                for title_lower, pos_data in positions.items():
                    try:
                        uuid = pos_data.get("uuid")
                        if not uuid:
                            continue
                        candidates["storyteller"].append(
                            {
                                "source_family": "storyteller",
                                "source": "storyteller",
                                "source_key": f"storyteller:{uuid}",
                                "title": title_lower,
                                "author": "",
                                "storyteller_uuid": uuid,
                                "cover_url": f"/api/v2/books/{uuid}/cover",
                                "action_kind": "create_ebook_mapping",
                                "media_format": "ebook",
                            }
                        )
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"Ebook candidates: Storyteller fetch failed: {e}")
                if errors is not None:
                    errors.add("Storyteller")

        if self.grimmory_client and self.grimmory_client.is_configured():
            try:
                for book in self.grimmory_client.get_all_books() or []:
                    try:
                        filename = book.get("fileName", "")
                        if not filename or book.get("id") is None:
                            continue
                        if not book.get("bookFileId"):
                            continue
                        instance_id, source_id = self._grimmory_identity(book)
                        candidates["grimmory"].append(
                            {
                                "source_family": "grimmory",
                                "source": "grimmory",
                                "source_key": f"grimmory:{source_id}",
                                "title": book.get("title") or Path(filename).stem,
                                "author": book.get("authors") or "",
                                "filename": filename,
                                "id": source_id,
                                "action_kind": "create_ebook_mapping",
                                "media_format": self._grimmory_media_format(book),
                            }
                        )
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"Ebook candidates: Grimmory fetch failed: {e}")
                if errors is not None:
                    errors.add("Grimmory")

        try:
            unlinked_docs = self.database_service.get_unlinked_kosync_documents()
            for doc in unlinked_docs:
                try:
                    if not doc.filename:
                        continue
                    candidates["kosync"].append(
                        {
                            "source_family": "kosync",
                            "source": "kosync",
                            "source_key": f"kosync:{doc.document_hash}",
                            "title": Path(doc.filename).stem,
                            "author": "",
                            "filename": doc.filename,
                            "action_kind": "create_ebook_mapping",
                            "media_format": "ebook",
                        }
                    )
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Ebook candidates: KoSync fetch failed: {e}")
            if errors is not None:
                errors.add("KoSync")

        return candidates

    def _mapped_grimmory_books(self, books) -> set:
        mapped = set()
        for book in books:
            audio_source_id = getattr(book, "grimmory_audio_source_id", None)
            if audio_source_id:
                mapped.add(audio_source_id)
            doc_hash = getattr(book, "kosync_doc_id", None)
            if doc_hash:
                try:
                    doc = self.database_service.get_kosync_document(doc_hash)
                    grimmory_id = getattr(doc, "grimmory_id", None) if doc else None
                    if getattr(doc, "source", None) == "grimmory" and grimmory_id:
                        mapped.add(str(grimmory_id))
                except Exception:
                    pass
        return mapped

    def _check_cross_ebook_suggestions(
        self, errors: set[str] | None = None, abs_audiobooks: list[dict] | None = None
    ):
        """Check for cross-ebook pairings (Storyteller<->Grimmory, Storyteller<->KoSync, KoSync<->Grimmory)."""
        all_books = self.database_service.get_all_books()
        mapped_st_uuids = {b.storyteller_uuid for b in all_books if b.storyteller_uuid}
        mapped_grimmory_books = self._mapped_grimmory_books(all_books)
        mapped_kosync_hashes = {b.kosync_doc_id for b in all_books if b.kosync_doc_id}

        # Build title-dedup index from existing suggestions to avoid duplicating ABS suggestions
        existing_titles = set()
        for s in self.database_service.get_all_actionable_suggestions():
            if s.title:
                existing_titles.add(self._normalize_title(s.title))

        ebook_candidates = self._build_ebook_source_candidates(errors)
        abs_candidates = self._build_abs_candidates(
            abs_audiobooks or [], mapped_abs_ids={getattr(b, "abs_id", None) for b in all_books}
        )

        # Storyteller books with progress -> audiobook companions.
        for st_book in self._get_storyteller_books_with_progress(mapped_st_uuids, errors):
            uuid = st_book["uuid"]
            norm_title = self._normalize_title(st_book["title"])
            other_candidates = ebook_candidates.get("grimmory", []) + abs_candidates
            matches = self._rank_candidates_for_book(
                st_book["title"],
                st_book["author"],
                other_candidates,
                source_media_format=st_book["media_format"],
            )
            cover = st_book.get("cover_url") or self._cover_url_for("storyteller", uuid, st_book)
            self._upsert_detected_book(
                source="storyteller",
                source_id=uuid,
                title=st_book["title"],
                progress_percentage=st_book.get("pct", 0.0),
                author=st_book.get("author", ""),
                cover_url=cover,
                matches=matches,
                media_format=st_book["media_format"],
                source_updated_at=st_book.get("source_updated_at"),
            )

            if (
                matches
                and not self.database_service.suggestion_exists(uuid, source="storyteller")
                and norm_title not in existing_titles
            ):
                self._save_suggestion_with_merge(
                    "storyteller", uuid, st_book["title"], st_book["author"], cover, matches
                )
                existing_titles.add(norm_title)

        # Grimmory books with progress -> opposite-format companions.
        for bl_book in self._get_grimmory_books_with_progress(mapped_grimmory_books, errors):
            filename = bl_book["filename"]
            source_id = bl_book["source_id"]
            norm_title = self._normalize_title(bl_book["title"])
            own_source_key = f"grimmory:{source_id}"
            other_candidates = [
                candidate
                for candidate in (
                    ebook_candidates.get("storyteller", [])
                    + ebook_candidates.get("kosync", [])
                    + ebook_candidates.get("grimmory", [])
                    + abs_candidates
                )
                if candidate.get("source_key") != own_source_key
            ]
            matches = self._rank_candidates_for_book(
                bl_book["title"],
                bl_book["author"],
                other_candidates,
                source_media_format=bl_book["media_format"],
            )
            matches = self._merge_detected_matches("grimmory", source_id, matches)
            cover = self._cover_url_for("grimmory", filename, bl_book)

            # Legacy suggestion only when there's a cross-source match to act on.
            if (
                matches
                and not self.database_service.suggestion_exists(source_id, source="grimmory")
                and norm_title not in existing_titles
            ):
                self._save_suggestion_with_merge(
                    "grimmory", source_id, bl_book["title"], bl_book["author"], cover, matches
                )

            # Surface every in-progress Grimmory book as a DetectedBook — even with no
            # cross-source match — so Grimmory-only reads still appear for manual promotion.
            self._upsert_detected_book(
                source="grimmory",
                source_id=source_id,
                title=bl_book["title"],
                progress_percentage=bl_book.get("pct", 0.0),
                author=bl_book.get("author", ""),
                cover_url=cover,
                matches=matches,
                ebook_filename=filename if bl_book["media_format"] == "ebook" else None,
                media_format=bl_book["media_format"],
                source_updated_at=bl_book.get("source_updated_at"),
            )
            existing_titles.add(norm_title)

        # KoSync books with progress -> audiobook companions.
        for doc in self._get_kosync_books_with_progress(mapped_kosync_hashes, errors):
            matches = self._rank_candidates_for_book(
                doc["title"],
                doc["author"],
                ebook_candidates.get("storyteller", []) + ebook_candidates.get("grimmory", []) + abs_candidates,
                source_media_format=doc["media_format"],
            )
            matches = self._merge_detected_matches("kosync", doc["source_id"], matches)
            self._upsert_detected_book(
                source="kosync",
                source_id=doc["source_id"],
                title=doc["title"],
                progress_percentage=doc["pct"],
                author=doc["author"],
                matches=matches,
                device=doc.get("device"),
                ebook_filename=doc["filename"],
                media_format=doc["media_format"],
                source_updated_at=doc.get("source_updated_at"),
            )

    def _check_reverse_suggestions(self, errors: set[str] | None = None) -> list[dict]:
        """Check Storyteller and Grimmory for books with progress that could match ABS audiobooks."""
        if not self.abs_client:
            return []

        try:
            all_audiobooks = self.abs_client.get_all_audiobooks()
        except Exception as e:
            logger.debug(f"Reverse suggestions: failed to fetch ABS audiobooks: {e}")
            if errors is not None:
                errors.add("Audiobookshelf")
            return []

        if not all_audiobooks:
            return []

        all_books = self.database_service.get_all_books()
        mapped_abs_ids = {b.abs_id for b in all_books}
        mapped_storyteller_uuids = {b.storyteller_uuid for b in all_books if b.storyteller_uuid}
        mapped_grimmory_books = self._mapped_grimmory_books(all_books)

        abs_by_title: dict[str, list[dict]] = {}
        for ab in all_audiobooks:
            meta = ab.get("media", {}).get("metadata", {})
            title = meta.get("title", "")
            if title:
                clean = self._normalize_title(title)
                if clean:
                    abs_by_title.setdefault(clean, []).append(ab)

        for st_book in self._get_storyteller_books_with_progress(mapped_storyteller_uuids, errors):
            clean_title = self._normalize_title(st_book["title"])
            matches = self._find_abs_audiobook_matches(clean_title, abs_by_title, mapped_abs_ids)
            if matches:
                self._save_reverse_suggestion(matches, clean_title, f"storyteller:{st_book['uuid']}")

        for bl_book in self._get_grimmory_books_with_progress(mapped_grimmory_books, errors):
            if bl_book["media_format"] != "ebook":
                continue
            clean_title = self._normalize_title(bl_book["title"])
            matches = self._find_abs_audiobook_matches(clean_title, abs_by_title, mapped_abs_ids)
            if matches:
                self._save_reverse_suggestion(
                    matches,
                    bl_book["title"],
                    f"grimmory:{bl_book['source_id']}",
                )
        return all_audiobooks

    def _build_abs_candidates(self, audiobooks: list[dict], mapped_abs_ids: set) -> list[dict]:
        candidates = []
        for audiobook in audiobooks:
            abs_id = audiobook.get("id")
            if not abs_id or abs_id in mapped_abs_ids:
                continue
            metadata = audiobook.get("media", {}).get("metadata", {})
            candidates.append(
                {
                    "source_family": "abs_audiobook",
                    "source": "abs_audiobook",
                    "source_key": f"abs:{abs_id}",
                    "abs_id": abs_id,
                    "title": metadata.get("title") or "",
                    "author": metadata.get("authorName") or "",
                    "action_kind": "create_mapping",
                    "media_format": "audiobook",
                }
            )
        return candidates

    def _merge_detected_matches(self, source: str, source_id: str, matches: list[dict]) -> list[dict]:
        existing = self.database_service.get_detected_book(source_id, source=source)
        existing_matches = existing.matches if existing and isinstance(existing.matches, list) else []
        return self._dedupe_matches(existing_matches + matches)

    def _find_abs_audiobook_matches(self, clean_title: str, abs_by_title: dict, mapped_abs_ids: set) -> list[dict]:
        """Find ABS audiobooks matching a title, excluding already-mapped ones."""
        if not clean_title:
            return []
        matches = []
        for indexed_title, audiobooks in abs_by_title.items():
            # Check for substring match in either direction
            if clean_title in indexed_title or indexed_title in clean_title:
                for ab in audiobooks:
                    ab_id = ab.get("id")
                    if ab_id in mapped_abs_ids:
                        continue
                    meta = ab.get("media", {}).get("metadata", {})
                    matches.append(
                        {
                            "source": "abs_audiobook",
                            "source_key": f"abs:{ab_id}",
                            "abs_id": ab_id,
                            "title": meta.get("title"),
                            "author": meta.get("authorName"),
                            "confidence": "high" if clean_title == indexed_title else "medium",
                            "media_format": "audiobook",
                        }
                    )
        return matches

    @staticmethod
    def _cover_url_for(source: str, source_id: str, metadata: dict | None = None) -> str:
        """Construct a cover URL appropriate for the given source type."""
        if source == "abs":
            return f"/api/cover-proxy/{source_id}"
        if source == "storyteller":
            return (metadata or {}).get("cover_url", "")
        if source == "grimmory":
            bl_id = (metadata or {}).get("id")
            if not bl_id:
                return ""
            instance_id = str((metadata or {}).get("instance_id") or "default")
            raw_id = str(bl_id).split(":", 1)[-1]
            prefix = "grimmory2" if instance_id == "2" else "grimmory"
            return f"/api/cover-proxy/{prefix}/{raw_id}"
        return ""

    def _save_suggestion_with_merge(
        self, source: str, source_id: str, title: str, author: str | None, cover_url: str, new_matches: list[dict]
    ):
        """Save or merge a suggestion for any source type. Deduplicates matches by key."""
        if self._suggestions_disabled():
            return
        if self.database_service.is_suggestion_ignored(source_id, source=source):
            return

        existing = self.database_service.get_pending_suggestion(
            source_id, source=source
        ) or self.database_service.get_suggestion(source_id, source=source)
        merged_matches = []
        merged_index = {}

        def _match_key(match):
            return (
                match.get("abs_id"),
                match.get("source_key"),
                match.get("title"),
                match.get("author"),
            )

        for match in (existing.matches if existing else []) + new_matches:
            key = _match_key(match)
            prior = merged_index.get(key)
            if prior is None:
                merged_index[key] = len(merged_matches)
                merged_matches.append(dict(match))
                continue
            current = merged_matches[prior]
            merged_matches[prior] = {
                **current,
                **{k: v for k, v in match.items() if v not in (None, "")},
            }

        suggestion = PendingSuggestion(
            source=source,
            source_id=source_id,
            title=(existing.title if existing and existing.title else title),
            author=(existing.author if existing and existing.author else author),
            cover_url=(existing.cover_url if existing and existing.cover_url else cover_url),
            matches_json=json.dumps(merged_matches),
        )
        self.database_service.save_pending_suggestion(suggestion)
        logger.info(f"Suggestion ({source}): '{title}' saved with {len(merged_matches)} match(es)")

    def _save_reverse_suggestion(self, matches: list[dict], title: str, source_key: str):
        """Save a reverse suggestion (ebook -> audiobook) anchored on ABS."""
        best = next((m for m in matches if m.get("confidence") == "high"), matches[0])
        abs_id = best["abs_id"]
        cover = self._cover_url_for("abs", abs_id)
        matches_with_provenance = [dict(m, source_key=source_key) for m in matches]
        self._save_suggestion_with_merge(
            "abs", abs_id, best.get("title", title), best.get("author"), cover, matches_with_provenance
        )

    def _run_rescan_job(self, catalog: bool = False) -> None:
        try:
            self._update_rescan_status(
                running=True,
                queued=False,
                phase="starting",
                message="Preparing suggestions rescan...",
                rate_limited=False,
            )
            abs_progress = {}
            errors: set[str] = set()
            if self.abs_client and self.abs_client.is_configured():
                try:
                    abs_progress = self.abs_client.get_all_progress_raw() or {}
                except Exception as e:
                    logger.warning(f"Manual discovery failed to load ABS progress: {e}")
                    errors.add("Audiobookshelf")
            self.check_for_suggestions(abs_progress, [], errors=errors)
            if catalog:
                stats = self.rescan_library_suggestions()
                errors.update(stats.pop("failed_sources", []))
                count_label = "catalog suggestion"
            else:
                total = self.database_service.get_active_detected_book_count()
                stats = {
                    "created": 0,
                    "updated": 0,
                    "deleted": 0,
                    "total": total,
                    "detected": total,
                    "bookfusion_catalog": False,
                }
                count_label = "Currently Reading book"
            phase = "partial" if errors else "complete"
            suffix = f" Some sources failed: {', '.join(sorted(errors))}." if errors else ""
            self._update_rescan_status(
                running=False,
                queued=False,
                phase=phase,
                message=f"Rescan {phase}. {stats['total']} {count_label}(s) available.{suffix}",
                last_finished_at=time.time(),
                failed_sources=sorted(errors),
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
        if self._suggestions_disabled():
            return {
                "created": 0,
                "updated": 0,
                "deleted": 0,
                "total": 0,
                "bookfusion_catalog": False,
                "failed_sources": [],
            }

        mapped_ids = {b.abs_id for b in self.database_service.get_all_books()}
        existing_actionable = {
            s.source_id: s
            for s in self.database_service.get_all_actionable_suggestions()
            if getattr(s, "source", "abs") == "abs"
        }
        bookfusion_context = self._get_bookfusion_context()
        errors: set[str] = set()
        candidates = self._build_library_candidates(
            bookfusion_context=bookfusion_context,
            include_filesystem=True,
            errors=errors,
        )

        created = 0
        updated = 0
        kept_ids = set()
        all_abs_books = []

        if self.abs_client:
            try:
                self._update_rescan_status(phase="loading_abs", message="Loading ABS audiobooks...")
                all_abs_books = self.abs_client.get_all_audiobooks() or []
            except Exception as e:
                logger.warning(f"Suggestions rescan failed to load ABS audiobooks: {e}")
                errors.add("Audiobookshelf")
                all_abs_books = []

            total_books = len(all_abs_books)
            self._update_rescan_status(phase="scoring", message=f"Scoring {total_books} ABS books...")
            for idx, abs_book in enumerate(all_abs_books, start=1):
                abs_id = abs_book.get("id")
                if not abs_id or abs_id in mapped_ids or self.database_service.is_suggestion_ignored(abs_id):
                    continue

                meta = abs_book.get("media", {}).get("metadata", {})
                title = meta.get("title") or ""
                author = meta.get("authorName") or ""
                matches = self._rank_candidates_for_book(
                    title, author, candidates, bookfusion_context=bookfusion_context
                )

                if not matches:
                    continue

                kept_ids.add(abs_id)
                suggestion = PendingSuggestion(
                    source_id=abs_id,
                    title=title,
                    author=author,
                    cover_url=self._cover_url_for("abs", abs_id),
                    matches_json=json.dumps(matches),
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
        if all_abs_books and not errors:
            self._update_rescan_status(phase="cleanup", message="Cleaning stale suggestions...")
            for source_id in list(existing_actionable.keys()):
                if source_id not in kept_ids:
                    if self.database_service.resolve_suggestion(source_id):
                        deleted += 1

        total = len(self.database_service.get_all_actionable_suggestions())
        logger.info(
            "Suggestions rescan completed: created=%s updated=%s deleted=%s total=%s", created, updated, deleted, total
        )
        return {
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "total": total,
            "bookfusion_catalog": bookfusion_context["has_catalog"],
            "failed_sources": sorted(errors),
        }

    def _search_live_candidates(
        self,
        title: str,
        author: str,
        bookfusion_context: dict | None = None,
        source_media_format: str | None = None,
    ) -> list[dict]:
        """Search Grimmory and CWA live APIs for opposite-format candidates."""
        matches = []
        query = f"{title} {author}".strip() if author else title

        if self.grimmory_client and self.grimmory_client.is_configured():
            try:
                live_results = self.grimmory_client.search_books(query) or []
                live_candidates = []
                for book in live_results:
                    filename = book.get("fileName", "")
                    if not filename or book.get("id") is None:
                        continue
                    if not book.get("bookFileId"):
                        continue
                    instance_id, source_id = self._grimmory_identity(book)
                    live_candidates.append(
                        {
                            "source_family": "grimmory",
                            "source": "grimmory",
                            "source_key": f"grimmory:{source_id}",
                            "title": book.get("title") or Path(filename).stem,
                            "author": book.get("authors") or "",
                            "filename": filename,
                            "id": source_id,
                            "action_kind": "create_mapping",
                            "media_format": self._grimmory_media_format(book),
                        }
                    )
                matches.extend(
                    self._rank_candidates_for_book(
                        title,
                        author,
                        live_candidates,
                        bookfusion_context=bookfusion_context,
                        source_media_format=source_media_format,
                    )
                )
            except Exception as e:
                logger.warning(f"Grimmory live search failed during suggestion: {e}")

        if self.library_service and self.library_service.cwa_client and self.library_service.cwa_client.is_configured():
            try:
                cwa_results = self.library_service.cwa_client.search_ebooks(query)
                for cr in cwa_results or []:
                    cwa_candidate = {
                        "source_family": "cwa",
                        "source": "cwa",
                        "source_key": f"cwa:{cr.get('id')}",
                        "title": cr.get("title"),
                        "author": cr.get("author"),
                        "filename": f"cwa_{cr.get('id', 'unknown')}.{cr.get('ext', 'epub')}",
                        "action_kind": "create_mapping",
                        "media_format": "ebook",
                    }
                    cwa_ranked = self._rank_candidates_for_book(
                        title,
                        author,
                        [cwa_candidate],
                        bookfusion_context=bookfusion_context,
                        source_media_format=source_media_format,
                    )
                    matches.extend(cwa_ranked)
            except Exception as e:
                logger.warning(f"CWA search failed during suggestion: {e}")

        return matches

    def _dedupe_matches(self, matches: list[dict], limit: int = 6) -> list[dict]:
        """Deduplicate matches by source_key/filename/title, keeping highest score."""
        deduped = {}
        for match in matches:
            key = match.get("source_key") or match.get("filename") or match.get("title")
            if not key:
                continue
            existing = deduped.get(key)
            if not existing or match.get("score", 0) > existing.get("score", 0):
                deduped[key] = match
        return sorted(deduped.values(), key=lambda m: m.get("score", 0.0), reverse=True)[:limit]

    def find_companion_candidates(self, title: str, author: str, source_media_format: str) -> list[dict]:
        """Return verified opposite-format candidates for the manual review flow."""
        candidates = self._build_library_candidates(include_filesystem=True)
        if source_media_format == "ebook" and self.abs_client:
            try:
                candidates.extend(self._build_abs_candidates(self.abs_client.get_all_audiobooks() or [], set()))
            except Exception as exc:
                logger.warning("ABS companion search failed: %s", exc)
        supported_sources = (
            {"abs_audiobook", "abs", "grimmory"}
            if source_media_format == "ebook"
            else {"grimmory", "kosync", "filesystem", "cwa", "abs_ebook"}
        )
        ranked = self._rank_candidates_for_book(
            title,
            author,
            candidates,
            source_media_format=source_media_format,
        )
        return [
            candidate
            for candidate in ranked
            if (candidate.get("source_family") or candidate.get("source")) in supported_sources
        ]

    def _create_suggestion(self, abs_id, progress_data):
        """Create or update a detected ABS book for an unmapped item."""
        with self._suggestion_lock:
            if abs_id in self._suggestion_in_flight:
                return None
            self._suggestion_in_flight.add(abs_id)

        try:
            logger.info(f"Found potential new detected ABS book: '{abs_id}'")
            item = self.abs_client.get_item_details(abs_id)
            if not item:
                logger.debug(f"Detected book lookup failed: Could not get details for {abs_id}")
                return False

            media = item.get("media", {})
            metadata = media.get("metadata", {})
            title = metadata.get("title") or ""
            author = metadata.get("authorName") or ""
            cover = self._cover_url_for("abs", abs_id)
            logger.debug(f"Checking detected matches for '{title}' (Author: {author})")

            progress_percentage = self._progress_fraction(progress_data) or 0.0

            bookfusion_context = self._get_bookfusion_context()
            matches = self._rank_candidates_for_book(
                title,
                author,
                self._build_library_candidates(bookfusion_context=bookfusion_context),
                bookfusion_context=bookfusion_context,
                source_media_format="audiobook",
            )
            matches.extend(
                self._search_live_candidates(
                    title,
                    author,
                    bookfusion_context,
                    source_media_format="audiobook",
                )
            )
            matches = self._dedupe_matches(matches)

            self._upsert_detected_book(
                source="abs",
                source_id=abs_id,
                title=title,
                author=author,
                cover_url=cover,
                progress_percentage=progress_percentage,
                matches=matches,
                media_format="audiobook",
                source_updated_at=(progress_data or {}).get("lastUpdate") or (progress_data or {}).get("updatedAt"),
            )

            logger.info(
                f"Created detected entry for '{title}' with {len(matches)} matches"
                if matches
                else f"Created detected entry for '{title}' with no matches yet"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to create detected entry for '{abs_id}': {e}")
            logger.debug(traceback.format_exc())
            return False
        finally:
            with self._suggestion_lock:
                self._suggestion_in_flight.discard(abs_id)
