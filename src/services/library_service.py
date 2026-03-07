"""
Library Service.
Handles high-level book management, bridging the gap between
AudioBookShelf (ABS), Booklore (Metadata), and our local database.
"""

import logging
import os

from src.api.api_clients import ABSClient
from src.api.cwa_client import CWAClient
from src.db.database_service import DatabaseService
from src.db.models import Book

logger = logging.getLogger(__name__)

class LibraryService:
    def __init__(self, database_service: DatabaseService, booklore_client, cwa_client: CWAClient, abs_client: ABSClient, epub_cache_dir: str):
        self.database_service = database_service
        self.booklore = booklore_client
        self.cwa_client = cwa_client
        self.abs_client = abs_client
        self.epub_cache_dir = epub_cache_dir

        if not os.path.exists(self.epub_cache_dir):
            try:
                os.makedirs(self.epub_cache_dir)
            except Exception as e:
                logger.warning("Failed to create EPUB cache directory '%s': %s", self.epub_cache_dir, e)

    def get_syncable_books(self) -> list[Book]:
        """
        Returns a list of books that are active and candidates for synchronization.
        """
        # This wraps the low-level DB query
        return self.database_service.get_all_books()

    def acquire_ebook(self, abs_item: dict) -> str | None:
        """
        Attempt to acquire an ebook for the given audiobook item.
        Priority Chain:
        1. ABS Direct Match (Audiobook item has ebook file)
        2. Booklore (Curated DB Match)
        3. CWA (Automated Library Search via OPDS)
        4. ABS Search (Search other libraries for title)
        5. Filesystem (Fallback - handled by caller)

        Returns:
            Absolute path to the downloaded/found ebook, or None.
        """
        if not abs_item:
            return None

        item_id = abs_item.get('id')
        title = abs_item.get('media', {}).get('metadata', {}).get('title')
        author = abs_item.get('media', {}).get('metadata', {}).get('authorName')

        # Sanity check
        if not item_id or not title:
            return None

        logger.info(f"Acquiring ebook for: {title} ({item_id})")
        logger.debug(f"   Author: {author}")
        logger.debug(f"   ABS Client available: {self.abs_client is not None}")
        logger.debug(f"   CWA Client available: {self.cwa_client is not None}, configured: {self.cwa_client.is_configured() if self.cwa_client else 'N/A'}")

        # 1. ABS Direct Match
        if self.abs_client:
            ebooks = self.abs_client.get_ebook_files(item_id)
            logger.debug(f"   ABS Direct Check: Found {len(ebooks)} ebook file(s)")
            if ebooks:
                logger.info(f"   Priority 1 (ABS Direct): Found {len(ebooks)} ebook(s) in item.")
                target = ebooks[0]
                filename = f"{item_id}_direct.{target['ext']}"
                output_path = os.path.join(self.epub_cache_dir, filename)

                # Check if already exists?
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                    logger.info(f"   Using cached ebook: {output_path}")
                    return output_path

                if self.abs_client.download_file(target['stream_url'], output_path):
                     logger.info(f"   Downloaded direct match to {output_path}")
                     return output_path

        # 2. Booklore (Curated)
        # Placeholder for curated DB lookup.
        # Future: Check self.db.find_booklore_match(title, author)

        # 3. CWA (OPDS)
        if self.cwa_client and self.cwa_client.is_configured():
            # Use title + author for better precision
            query = f"{title}"
            if author:
                query += f" {author}"

            results = self.cwa_client.search_ebooks(query)
            if results:
                logger.info(f"   Priority 3 (CWA): Found {len(results)} matches for '{query}'")
                target = results[0]
                filename = f"{item_id}_cwa.{target['ext']}"
                output_path = os.path.join(self.epub_cache_dir, filename)

                if self.cwa_client.download_ebook(target['download_url'], output_path):
                    logger.info(f"   Downloaded CWA match to {output_path}")
                    return output_path
            else:
                 logger.debug(f"   CWA: No matches for '{query}'")

        # 4. ABS Search
        if self.abs_client:
             results = self.abs_client.search_ebooks(title)
             if results:
                 logger.info(f"   Priority 4 (ABS Search): Found {len(results)} matches for '{title}'")
                 # Try to find one with ebook files
                 for res in results:
                     # Check if author matches loosely
                     res_author = res.get('author', '')
                     if author and author.lower() not in res_author.lower() and res_author.lower() not in author.lower():
                         continue

                     target_files = self.abs_client.get_ebook_files(res['id'])
                     if target_files:
                         tf = target_files[0]
                         filename = f"{item_id}_abs_search.{tf['ext']}"
                         output_path = os.path.join(self.epub_cache_dir, filename)

                         if self.abs_client.download_file(tf['stream_url'], output_path):
                             logger.info(f"   Downloaded ABS search match to {output_path}")
                             return output_path
                         break

        return None

    def sync_library_books(self):
        """
        Main Routine: Synchronize our local library DB with external metadata sources (Booklore).

        The new BookloreClient handles its own file-based caching internally.
        This method now simply triggers a cache refresh by calling get_all_books().
        """
        books = self.get_syncable_books()
        logger.info(f"LibraryService: Syncing metadata for {len(books)} books...")

        if not self.booklore or not self.booklore.is_configured():
            logger.info("   Booklore not configured, skipping library sync.")
            return

        try:
            all_books = self.booklore.get_all_books()
            logger.info(f"   Booklore cache is active with {len(all_books)} books.")
        except Exception as e:
            logger.error(f"   Library sync failed for Booklore: {e}")

