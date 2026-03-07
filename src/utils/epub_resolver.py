import glob
import logging
import os
import tempfile
from pathlib import Path

from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


def get_local_epub(ebook_filename, books_dir, epub_cache_dir, booklore_client=None):
    """
    Get local path to EPUB file, downloading from Booklore if necessary.

    Args:
        ebook_filename: The filename to look for
        books_dir: Base directory to search for books on filesystem
        epub_cache_dir: Directory for cached EPUB downloads
        booklore_client: Booklore API client to try downloading from
    """
    books_search_dir = Path(books_dir) if books_dir else Path("/books")
    epub_cache_dir = Path(epub_cache_dir) if epub_cache_dir else Path("/tmp/epub_cache")

    # Reject filenames with path traversal components
    if os.sep in ebook_filename or '/' in ebook_filename or '..' in ebook_filename:
        logger.error(f"Invalid ebook filename rejected: {sanitize_log_data(ebook_filename)}")
        return None

    # First, try to find on filesystem
    escaped_filename = glob.escape(ebook_filename)
    resolved_search_dir = books_search_dir.resolve()
    filesystem_matches = list(books_search_dir.glob(f"**/{escaped_filename}"))
    for candidate in filesystem_matches:
        if candidate.resolve().is_relative_to(resolved_search_dir):
            logger.info(f"Found EPUB on filesystem: {candidate}")
            return candidate
    if filesystem_matches:
        logger.warning("EPUB matches found but all outside search directory, skipping")

    # Check persistent EPUB cache
    epub_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_root = epub_cache_dir.resolve()
    cached_path = epub_cache_dir / ebook_filename
    # Prevent path traversal via untrusted filenames (e.g., ../../etc/passwd)
    if not cached_path.resolve().is_relative_to(cache_root):
        logger.error(f"Invalid filename detected: {sanitize_log_data(ebook_filename)}")
        return None
    if cached_path.exists():
        logger.info(f"Found EPUB in cache: '{cached_path}'")
        return cached_path

    # Try to download from Booklore API
    if hasattr(booklore_client, 'is_configured') and booklore_client.is_configured():
        book = booklore_client.find_book_by_filename(ebook_filename)
        if book:
            logger.info(f"Downloading EPUB from Booklore: {sanitize_log_data(ebook_filename)}")
            if not hasattr(booklore_client, 'download_book'):
                logger.error("Booklore client missing download_book method")
            else:
                book_id = book.get('id')
                if not book_id:
                    logger.warning("Booklore returned book without ID")
                else:
                    content = booklore_client.download_book(book_id)
                    if content:
                        cached_path.parent.mkdir(parents=True, exist_ok=True)
                        tmp_fd, tmp_path = tempfile.mkstemp(dir=cached_path.parent, suffix='.tmp')
                        try:
                            with os.fdopen(tmp_fd, 'wb') as f:
                                f.write(content)
                                f.flush()
                                os.fsync(f.fileno())
                            os.replace(tmp_path, cached_path)
                        except BaseException:
                            os.unlink(tmp_path)
                            raise
                        logger.info(f"Downloaded EPUB to cache: '{cached_path}'")
                        return cached_path

                    logger.error("Failed to download EPUB content from Booklore")

    if not filesystem_matches and not (hasattr(booklore_client, 'is_configured') and booklore_client.is_configured()):
        logger.error("EPUB not found on filesystem and Booklore not configured")

    return None
