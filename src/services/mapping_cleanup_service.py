import logging
import os
from pathlib import Path

from src.sync_clients.abs_sync_client import TRANSCRIPT_DB_MANAGED
from src.utils.path_utils import is_safe_path_within

logger = logging.getLogger(__name__)


def cleanup_mapping_resources(
    book,
    *,
    container,
    manager,
    database_service,
    abs_service,
    grimmory_client,
    collection_name: str | None = None,
):
    """Delete external artifacts and membership data for a mapped book."""
    if not book:
        return

    grimmory_instance_id = None
    if book.kosync_doc_id:
        try:
            doc = database_service.get_kosync_document(book.kosync_doc_id)
            grimmory_id = getattr(doc, "grimmory_id", None) if doc else None
            if getattr(doc, "source", None) == "grimmory" and isinstance(grimmory_id, str) and ":" in grimmory_id:
                grimmory_instance_id = grimmory_id.split(":", 1)[0]
        except Exception as e:
            logger.debug(f"Failed to resolve Grimmory instance for cleanup: {e}")

    if book.transcript_file and book.transcript_file != TRANSCRIPT_DB_MANAGED:
        data_dir = container.data_dir()
        transcript_dir = data_dir / "transcripts"
        transcript_path = Path(book.transcript_file)
        if is_safe_path_within(transcript_path, transcript_dir):
            try:
                transcript_path.unlink()
            except Exception as e:
                logger.debug(f"Failed to delete transcript file '{book.transcript_file}': {e}")
        else:
            logger.warning(f"Blocked transcript deletion — path escapes transcripts dir: '{book.transcript_file}'")

    if book.ebook_filename:
        cache_dirs = []
        try:
            cache_dirs.append(container.epub_cache_dir())
        except Exception as e:
            logger.debug(f"Failed to get epub cache dir: {e}")

        manager_cache_dir = getattr(manager, "epub_cache_dir", None)
        if manager_cache_dir:
            cache_dirs.append(manager_cache_dir)

        seen_dirs = set()
        for cache_dir in cache_dirs:
            cache_dir_path = Path(cache_dir)
            cache_dir_key = str(cache_dir_path)
            if cache_dir_key in seen_dirs:
                continue
            seen_dirs.add(cache_dir_key)

            cached_path = cache_dir_path / book.ebook_filename
            if not is_safe_path_within(cached_path, cache_dir_path):
                logger.warning(f"Blocked ebook cache deletion — path escapes cache dir: '{book.ebook_filename}'")
                continue
            if cached_path.exists():
                try:
                    cached_path.unlink()
                    logger.info(f"Deleted cached ebook file: {book.ebook_filename}")
                except Exception as e:
                    logger.warning(f"Failed to delete cached ebook {book.ebook_filename}: {e}")

    if book.sync_mode == "ebook_only" and book.kosync_doc_id:
        logger.info(f"Deleting KOSync document record for ebook-only mapping: '{book.kosync_doc_id[:8]}'")
        database_service.delete_kosync_document(book.kosync_doc_id)

    collection_name = collection_name or os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")
    try:
        abs_service.remove_from_collection(book.abs_id, collection_name)
    except Exception as e:
        logger.warning(f"Failed to remove from ABS collection: {e}")

    if book.ebook_filename and grimmory_client.is_configured():
        shelf_filename = book.original_ebook_filename or book.ebook_filename
        try:
            if grimmory_instance_id:
                grimmory_client.remove_from_shelf(shelf_filename, instance_id=grimmory_instance_id)
            else:
                grimmory_client.remove_from_shelf(shelf_filename)
        except Exception as e:
            logger.warning(f"Failed to remove from Grimmory shelf: {e}")
