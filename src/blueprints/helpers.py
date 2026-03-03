"""Shared helper functions for Book Stitch blueprints.

All functions access shared state via flask.current_app.config rather than
module-level globals, which allows them to work from any blueprint.
"""

import glob as glob_module
import html
import logging
import os
import re
import signal
import time
from pathlib import Path

from flask import current_app

logger = logging.getLogger(__name__)


# --------------- Accessors for shared state ---------------

def get_container():
    return current_app.config['container']


def get_manager():
    return current_app.config['sync_manager']


def get_database_service():
    return current_app.config['database_service']


def get_ebook_dir():
    return current_app.config['EBOOK_DIR']


def get_covers_dir():
    return current_app.config['COVERS_DIR']


def get_abs_service():
    return current_app.config['abs_service']


# --------------- Booklore multi-instance helpers ---------------

def get_booklore_clients():
    """Return a list of configured (or all) BookloreClient instances from the container."""
    container = get_container()
    clients = [container.booklore_client()]
    try:
        clients.append(container.booklore_client_2())
    except AttributeError:
        pass
    except Exception as e:
        logger.debug(f"Failed to resolve booklore_client_2: {e}")
    return clients


def find_in_booklore(filename):
    """Search both Booklore instances, return (book_info, client) or (None, None)."""
    for client in get_booklore_clients():
        if client.is_configured():
            book = client.find_book_by_filename(filename)
            if book:
                return book, client
    return None, None


def any_booklore_configured():
    """Return True if any Booklore instance is configured."""
    return any(c.is_configured() for c in get_booklore_clients())


# --------------- Helper functions ---------------

def get_audiobooks_conditionally():
    """Get audiobooks from configured libraries (ABS_LIBRARY_IDS) or all libraries if not set."""
    return get_abs_service().get_audiobooks()


def get_abs_author(ab):
    """Extract author from ABS audiobook metadata."""
    media = ab.get('media', {})
    metadata = media.get('metadata', {})
    return metadata.get('authorName') or (metadata.get('authors') or [{}])[0].get("name", "")


def audiobook_matches_search(ab, search_term):
    """Check if audiobook matches search term (searches title AND author)."""
    manager = get_manager()

    def normalize(s):
        return re.sub(r'[^\w\s]', '', s.lower())

    title = normalize(manager.get_abs_title(ab))
    author = normalize(get_abs_author(ab))
    search_norm = normalize(search_term)

    # 1. Standard Search
    if search_norm in title or search_norm in author:
        return True

    # 2. Reverse Search (enforce minimum length to prevent short matches)
    MIN_LEN = 4
    if len(title) >= MIN_LEN and title in search_norm:
        return True
    if len(author) >= MIN_LEN and author in search_norm:
        return True

    return False


def find_ebook_file(filename, ebook_dir=None):
    base = ebook_dir if ebook_dir is not None else get_ebook_dir()
    escaped_filename = glob_module.escape(filename)
    matches = list(base.rglob(escaped_filename))
    return matches[0] if matches else None


def get_kosync_id_for_ebook(ebook_filename, booklore_id=None, original_filename=None, bl_client=None):
    """Get KOSync document ID for an ebook.
    Tries Booklore API first (if configured and booklore_id provided),
    falls back to filesystem if needed.
    """
    container = get_container()
    EBOOK_DIR = get_ebook_dir()

    # Try Booklore API first — use the specific client that reported the ID
    if booklore_id and bl_client and bl_client.is_configured():
        try:
            content = bl_client.download_book(booklore_id)
            if content:
                kosync_id = container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                if kosync_id:
                    logger.debug(f"Computed KOSync ID from Booklore download: '{kosync_id}'")
                    return kosync_id
        except Exception as e:
            logger.warning(f"Failed to get KOSync ID from Booklore ({bl_client.source_tag}): {e}")

    # Fall back to filesystem
    ebook_path = find_ebook_file(ebook_filename)
    if not ebook_path and original_filename:
        logger.debug(f"Primary file '{ebook_filename}' not found, checking original '{original_filename}'")
        ebook_path = find_ebook_file(original_filename)

    if ebook_path:
        return container.ebook_parser().get_kosync_id(ebook_path)

    # Check Epub Cache explicitly
    epub_cache = container.epub_cache_dir()
    cached_path = epub_cache / ebook_filename
    if cached_path.exists():
        return container.ebook_parser().get_kosync_id(cached_path)

    # On-Demand Fetching: ABS
    if "_abs." in ebook_filename:
        try:
            abs_id = ebook_filename.split("_abs.")[0]
            abs_client = container.abs_client()
            if abs_client and abs_client.is_configured():
                logger.info(f"Attempting on-demand ABS download for '{abs_id}'")
                ebook_files = abs_client.get_ebook_files(abs_id)
                if ebook_files:
                    target = ebook_files[0]
                    if not epub_cache.exists():
                        epub_cache.mkdir(parents=True, exist_ok=True)

                    if abs_client.download_file(target['stream_url'], cached_path):
                        logger.info(f"   Downloaded ABS ebook to '{cached_path}'")
                        return container.ebook_parser().get_kosync_id(cached_path)
                else:
                    logger.warning(f"   No ebook files found in ABS for item '{abs_id}'")
        except Exception as e:
            logger.error(f"   Failed ABS on-demand download: {e}")

    # On-Demand Fetching: CWA
    if "_cwa." in ebook_filename or ebook_filename.startswith("cwa_"):
        try:
            if ebook_filename.startswith("cwa_"):
                cwa_id = ebook_filename[4:].rsplit(".", 1)[0]
            else:
                cwa_id = ebook_filename.split("_cwa.")[0]
                if "_" in cwa_id and not ebook_filename.startswith("cwa_"):
                    pass

            if cwa_id:
                cwa_client = container.cwa_client()
                if cwa_client and cwa_client.is_configured():
                    logger.info(f"Attempting on-demand CWA download for ID '{cwa_id}'")

                    target = None
                    results = cwa_client.search_ebooks(cwa_id)

                    for res in results:
                        if str(res.get('id')) == cwa_id:
                            target = res
                            break

                    if not target and len(results) == 1:
                        target = results[0]

                    if target and target.get('download_url'):
                        logger.info(f"Using direct download link from search for '{target.get('title', 'Unknown')}'")
                    else:
                        logger.debug("Search did not return a usable result, trying direct ID lookup")
                        target = cwa_client.get_book_by_id(cwa_id)

                    if target and target.get('download_url'):
                        if not epub_cache.exists():
                            epub_cache.mkdir(parents=True, exist_ok=True)
                        if cwa_client.download_ebook(target['download_url'], cached_path):
                            logger.info(f"   Downloaded CWA ebook to '{cached_path}'")
                            return container.ebook_parser().get_kosync_id(cached_path)
                    else:
                        logger.warning(f"   Could not find CWA book for ID '{cwa_id}'")
        except Exception as e:
            logger.error(f"   Failed CWA on-demand download: {e}")

    # Neither source available
    if not any_booklore_configured() and not EBOOK_DIR.exists():
        logger.warning(
            f"Cannot compute KOSync ID for '{ebook_filename}': "
            "Neither Booklore integration nor /books volume is configured. "
            "Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books"
        )
    elif not booklore_id and not find_ebook_file(ebook_filename):
        logger.warning(f"Cannot compute KOSync ID for '{ebook_filename}': File not found in Booklore, filesystem, or remote sources")

    return None


class EbookResult:
    """Wrapper to provide consistent interface for ebooks from Booklore, CWA, ABS, or filesystem."""

    def __init__(self, name, title=None, subtitle=None, authors=None, booklore_id=None, path=None, source=None, source_id=None, cover_url=None):
        self.name = name
        self.title = title or Path(name).stem
        self.subtitle = subtitle or ''
        self.authors = authors or ''
        self.booklore_id = booklore_id
        self.path = path
        self.source = source
        self.source_id = source_id or booklore_id
        self.cover_url = cover_url
        self.has_metadata = booklore_id is not None or (title is not None and title != name)

    @property
    def display_name(self):
        """Format: 'Title: Subtitle - Author' for sources with metadata, title for filesystem."""
        if self.has_metadata and self.title:
            full_title = self.title
            if self.subtitle:
                full_title = f"{self.title}: {self.subtitle}"
            if self.authors:
                return f"{full_title} - {self.authors}"
            return full_title
        return self.title

    @property
    def stem(self):
        return Path(self.name).stem

    def __str__(self):
        return self.name


def get_searchable_ebooks(search_term):
    """Get ebooks from Booklore API, filesystem, ABS, and CWA.
    Returns list of EbookResult objects for consistent interface."""
    container = get_container()
    EBOOK_DIR = get_ebook_dir()

    results = []
    found_filenames = set()
    found_stems = set()

    # 1. Booklore (all instances)
    for bl_client in get_booklore_clients():
        if not bl_client.is_configured():
            continue
        try:
            label = os.environ.get(f"{bl_client.config_prefix}_LABEL", "Booklore")
            books = bl_client.search_books(search_term)
            if books:
                for b in books:
                    fname = b.get('fileName', '')
                    if fname.lower().endswith('.epub'):
                        if fname.lower() in found_filenames:
                            continue
                        found_filenames.add(fname.lower())
                        found_stems.add(Path(fname).stem.lower())
                        bl_id = b.get('id')
                        cover = f"/api/cover-proxy/booklore/{bl_client.source_tag}/{bl_id}" if bl_id else None
                        results.append(EbookResult(
                            name=fname,
                            title=b.get('title'),
                            subtitle=b.get('subtitle'),
                            authors=b.get('authors'),
                            booklore_id=bl_id,
                            source=label,
                            cover_url=cover
                        ))
        except Exception as e:
            logger.warning(f"Booklore ({bl_client.source_tag}) search failed: {e}")

    # 2. ABS ebook libraries
    if search_term:
        try:
            abs_service = get_abs_service()
            abs_ebooks = abs_service.search_ebooks(search_term)
            if abs_ebooks:
                for ab in abs_ebooks:
                    ebook_files = abs_service.get_ebook_files(ab['id'])
                    if ebook_files:
                        ef = ebook_files[0]
                        fname = f"{ab['id']}_abs.{ef['ext']}"
                        if fname.lower() not in found_filenames:
                            results.append(EbookResult(
                                name=fname,
                                title=ab.get('title'),
                                authors=ab.get('author'),
                                source='ABS',
                                source_id=ab.get('id'),
                                cover_url=f"/api/cover-proxy/{ab['id']}"
                            ))
                            found_filenames.add(fname.lower())
                            if ab.get('title'):
                                found_stems.add(ab['title'].lower().strip())
        except Exception as e:
            logger.warning(f"ABS ebook search failed: {e}")

    # 3. CWA (Calibre-Web Automated)
    if search_term:
        try:
            library_service = container.library_service()
            if library_service and library_service.cwa_client and library_service.cwa_client.is_configured():
                cwa_results = library_service.cwa_client.search_ebooks(search_term)
                if cwa_results:
                    for cr in cwa_results:
                        fname = f"cwa_{cr.get('id', 'unknown')}.{cr.get('ext', 'epub')}"
                        if fname.lower() not in found_filenames:
                            results.append(EbookResult(
                                name=fname,
                                title=cr.get('title'),
                                authors=cr.get('author'),
                                source='CWA',
                                source_id=cr.get('id')
                            ))
                            found_filenames.add(fname.lower())
                            if cr.get('title'):
                                found_stems.add(cr['title'].lower().strip())
        except Exception as e:
            logger.warning(f"CWA search failed: {e}")

    # 4. Search filesystem (Local) - LOW PRIORITY
    if EBOOK_DIR.exists():
        try:
            all_epubs = list(EBOOK_DIR.glob("**/*.epub"))
            for eb in all_epubs:
                fname_lower = eb.name.lower()
                stem_lower = eb.stem.lower()

                if fname_lower in found_filenames or stem_lower in found_stems:
                    continue

                if not search_term or search_term.lower() in fname_lower:
                    results.append(EbookResult(name=eb.name, path=eb, source='Local File'))
                    found_filenames.add(fname_lower)
                    found_stems.add(stem_lower)

        except Exception as e:
            logger.warning(f"Filesystem search failed: {e}")

    if not results and not EBOOK_DIR.exists() and not any_booklore_configured():
        logger.warning(
            "No ebooks available: Neither Booklore integration nor /books volume is configured. "
            "Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books"
        )

    return results


def cleanup_mapping_resources(book):
    """Delete external artifacts and membership data for a mapped book."""
    if not book:
        return

    container = get_container()
    manager = get_manager()
    database_service = get_database_service()

    if book.transcript_file:
        try:
            Path(book.transcript_file).unlink()
        except Exception as e:
            logger.debug(f"Failed to delete transcript file '{book.transcript_file}': {e}")

    if book.ebook_filename:
        cache_dirs = []
        try:
            cache_dirs.append(container.epub_cache_dir())
        except Exception as e:
            logger.debug(f"Failed to get epub cache dir: {e}")

        manager_cache_dir = getattr(manager, 'epub_cache_dir', None)
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
            if cached_path.exists():
                try:
                    cached_path.unlink()
                    logger.info(f"Deleted cached ebook file: {book.ebook_filename}")
                except Exception as e:
                    logger.warning(f"Failed to delete cached ebook {book.ebook_filename}: {e}")

    if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only' and book.kosync_doc_id:
        logger.info(f"Deleting KOSync document record for ebook-only mapping: '{book.kosync_doc_id[:8]}'")
        database_service.delete_kosync_document(book.kosync_doc_id)

    collection_name = os.environ.get('ABS_COLLECTION_NAME', 'Synced with KOReader')
    try:
        get_abs_service().remove_from_collection(book.abs_id, collection_name)
    except Exception as e:
        logger.warning(f"Failed to remove from ABS collection: {e}")

    if book.ebook_filename:
        shelf_filename = book.original_ebook_filename or book.ebook_filename
        for bl_client in get_booklore_clients():
            if bl_client.is_configured():
                try:
                    bl_client.remove_from_shelf(shelf_filename)
                except Exception as e:
                    logger.warning(f"Failed to remove from Booklore ({bl_client.source_tag}) shelf: {e}")


def restart_server():
    """Triggers a graceful restart by sending SIGTERM to the current process.
    The start.sh supervisor loop will catch the exit and restart the application.
    """
    logger.info("Stopping application (Supervisor will restart it)...")
    time.sleep(1.0)
    logger.info("Sending SIGTERM to trigger restart...")
    os.kill(os.getpid(), signal.SIGTERM)


def safe_folder_name(name: str) -> str:
    """Sanitize folder name for file system safe usage."""
    invalid = '<>:"/\\|?*'
    name = html.escape(str(name).strip())[:150]
    for c in invalid:
        name = name.replace(c, '_')
    return name.strip() or "Unknown"
