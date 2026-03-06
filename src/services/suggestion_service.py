import json
import logging
import os
import re
import threading
import traceback

from src.db.models import PendingSuggestion

logger = logging.getLogger(__name__)


class SuggestionService:
    """Handles suggestion discovery and creation for unmapped books."""

    def __init__(self,
                 database_service,
                 abs_client,
                 booklore_clients: list,
                 storyteller_client,
                 library_service,
                 books_dir,
                 ebook_parser):
        self.database_service = database_service
        self.abs_client = abs_client
        self._booklore_clients = booklore_clients
        self.storyteller_client = storyteller_client
        self.library_service = library_service
        self.books_dir = books_dir
        self.ebook_parser = ebook_parser

        self._suggestion_lock = threading.Lock()
        self._suggestion_in_flight: set[str] = set()

    def queue_suggestion(self, abs_id: str) -> None:
        """Queue suggestion discovery for an unmapped book (called from socket listener)."""
        if os.environ.get("SUGGESTIONS_ENABLED", "true").lower() != "true":
            return

        # Already mapped?
        all_books = self.database_service.get_all_books()
        mapped_ids = {b.abs_id for b in all_books}
        if abs_id in mapped_ids:
            return

        # Already has a suggestion (pending, dismissed, or ignored)?
        if self.database_service.suggestion_exists(abs_id):
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
                        # Check if a suggestion already exists (pending, dismissed, or ignored)
                        if self.database_service.suggestion_exists(abs_id):
                            logger.debug(f"Skipping {abs_id}: suggestion already exists/dismissed")
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
        for bl_client in self._booklore_clients:
            if not (bl_client and bl_client.is_configured()):
                continue
            try:
                bl_books = bl_client.get_all_books()
                for bl_book in bl_books:
                    title = bl_book.get('title', '')
                    filename = bl_book.get('fileName', '')
                    if not title:
                        continue

                    # Check if this book has progress
                    pct_raw, _ = bl_client.get_progress(filename)
                    if not pct_raw or pct_raw < 0.01 or pct_raw > 0.70:
                        continue

                    clean_title = re.sub(r'\s*[\(\[].*?[\)\]]', '', title).strip().lower()
                    source_key = f"booklore:{bl_client.source_tag}:{filename}"
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

        if self.database_service.suggestion_exists(abs_id):
            return

        cover = f"/api/cover-proxy/{abs_id}"
        # Include source_key as provenance so we know where the suggestion originated
        matches_with_provenance = [dict(m, source_key=source_key) for m in matches]
        suggestion = PendingSuggestion(
            source_id=abs_id,
            title=best.get('title', title),
            author=best.get('author'),
            cover_url=cover,
            matches_json=json.dumps(matches_with_provenance),
        )
        self.database_service.save_pending_suggestion(suggestion)
        logger.info(f"Reverse suggestion: '{title}' has matching audiobook '{best.get('title')}' in ABS")

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
            title = metadata.get('title')
            author = metadata.get('authorName')
            # Use local proxy for cover image to ensure accessibility
            cover = f"/api/cover-proxy/{abs_id}"

            # Clean title for better matching (remove text in parens/brackets)
            search_title = title
            if title:
                # Remove (Unabridged), [Dramatized Adaptation], etc.
                search_title = re.sub(r'\s*[\(\[].*?[\)\]]', '', title).strip()
                if search_title != title:
                    logger.debug(f"cleaned title for search: '{title}' -> '{search_title}'")

            logger.debug(f"Checking suggestions for '{title}' (Search: '{search_title}', Author: {author})")

            matches = []

            found_filenames = set()

            # 2a. Search Booklore (all instances)
            for bl_client in self._booklore_clients:
                if not (bl_client and bl_client.is_configured()):
                    continue
                try:
                    bl_results = bl_client.search_books(search_title)
                    logger.debug(f"Booklore ({bl_client.source_tag}) returned {len(bl_results)} results for '{search_title}'")
                    for b in bl_results:
                         # Filter for EPUBs
                         fname = b.get('fileName', '')
                         if fname.lower().endswith('.epub'):
                             found_filenames.add(fname)
                             matches.append({
                                 "source": "booklore",
                                 "title": b.get('title'),
                                 "author": b.get('authors'),
                                 "filename": fname,
                                 "id": str(b.get('id')),
                                 "confidence": "high" if search_title.lower() in b.get('title', '').lower() else "medium"
                             })
                except Exception as e:
                    logger.warning(f"Booklore search failed during suggestion: {e}")

            # 2b. Search Local Filesystem
            if self.books_dir and self.books_dir.exists():
                try:
                    clean_title = search_title.lower()
                    fs_matches = 0
                    for epub in self.books_dir.rglob("*.epub"):
                         if epub.name in found_filenames:
                             continue
                         if clean_title in epub.name.lower():
                             fs_matches += 1
                             matches.append({
                                 "source": "filesystem",
                                 "filename": epub.name,
                                 "path": str(epub),
                                 "confidence": "high"
                             })
                    logger.debug(f"Filesystem found {fs_matches} matches")
                except Exception as e:
                    logger.warning(f"Filesystem search failed during suggestion: {e}")

            # 2c. Search Storyteller
            if self.storyteller_client and self.storyteller_client.is_configured():
                try:
                    st_results = self.storyteller_client.search_books(search_title)
                    if st_results:
                        logger.debug(f"Storyteller: Found {len(st_results)} result(s) for '{search_title}'")
                        for sr in st_results:
                            matches.append({
                                "source": "storyteller",
                                "title": sr.get('title'),
                                "author": ', '.join(sr.get('authors', [])),
                                "uuid": sr.get('uuid'),
                                "confidence": "high" if search_title.lower() in sr.get('title', '').lower() else "medium"
                            })
                except Exception as e:
                    logger.warning(f"Storyteller search failed during suggestion: {e}")

            # 2d. ABS Direct Match (check if audiobook item has ebook files)
            if self.abs_client:
                try:
                    ebook_files = self.abs_client.get_ebook_files(abs_id)
                    if ebook_files:
                        logger.debug(f"ABS Direct: Found {len(ebook_files)} ebook file(s) in audiobook item")
                        for ef in ebook_files:
                            matches.append({
                                "source": "abs_direct",
                                "title": title,
                                "author": author,
                                "filename": f"{abs_id}_direct.{ef['ext']}",
                                "stream_url": ef['stream_url'],
                                "ext": ef['ext'],
                                "confidence": "high"
                            })
                except Exception as e:
                    logger.warning(f"ABS Direct search failed during suggestion: {e}")

            # 2e. CWA Search (Calibre-Web Automated via OPDS)
            if self.library_service and self.library_service.cwa_client and self.library_service.cwa_client.is_configured():
                try:
                    query = f"{search_title}"
                    if author:
                        query += f" {author}"
                    cwa_results = self.library_service.cwa_client.search_ebooks(query)
                    if cwa_results:
                        logger.debug(f"CWA: Found {len(cwa_results)} result(s) for '{search_title}'")
                        for cr in cwa_results:
                            matches.append({
                                "source": "cwa",
                                "title": cr.get('title'),
                                "author": cr.get('author'),
                                "filename": f"{abs_id}_cwa.{cr.get('ext', 'epub')}",
                                "download_url": cr.get('download_url'),
                                "ext": cr.get('ext', 'epub'),
                                "confidence": "high" if search_title.lower() in cr.get('title', '').lower() else "medium"
                            })
                except Exception as e:
                    logger.warning(f"CWA search failed during suggestion: {e}")

            # 2f. ABS Search (search other libraries for matching ebook)
            if self.abs_client:
                try:
                    abs_results = self.abs_client.search_ebooks(search_title)
                    if abs_results:
                        logger.debug(f"ABS Search: Found {len(abs_results)} result(s) for '{search_title}'")
                        for ar in abs_results:
                            # Check if this result has ebook files
                            result_ebooks = self.abs_client.get_ebook_files(ar['id'])
                            if result_ebooks:
                                ef = result_ebooks[0]
                                matches.append({
                                    "source": "abs_search",
                                    "title": ar.get('title'),
                                    "author": ar.get('author'),
                                    "filename": f"{abs_id}_abs_search.{ef['ext']}",
                                    "stream_url": ef['stream_url'],
                                    "ext": ef['ext'],
                                    "confidence": "medium"
                                })
                except Exception as e:
                    logger.warning(f"ABS Search failed during suggestion: {e}")

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
