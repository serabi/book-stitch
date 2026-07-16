"""Shared helper functions for PageKeeper blueprints.

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

from flask import abort, current_app

from src.utils.path_utils import is_safe_path_within, sanitize_filename
from src.utils.service_url_helper import get_hardcover_book_url, get_service_web_url  # noqa: F401

logger = logging.getLogger(__name__)


# --------------- Accessors for shared state ---------------


def get_container():
    return current_app.config["container"]


def get_manager():
    return current_app.config["sync_manager"]


def get_database_service():
    return current_app.config["database_service"]


def get_ebook_dir():
    return current_app.config["EBOOK_DIR"]


def get_covers_dir():
    return current_app.config["COVERS_DIR"]


def get_abs_service():
    return current_app.config["abs_service"]


def get_book_or_404(ref):
    """Resolve a book by canonical book_id or legacy abs_id and abort if missing."""
    book = get_database_service().get_book_by_ref(ref)
    if not book:
        abort(404)
    return book


# --------------- Grimmory helpers ---------------


def get_grimmory_client():
    """Return the Grimmory client group (facade over all instances)."""
    return get_container().grimmory_client_group()


def find_in_grimmory(filename, source_id=None):
    """Search Grimmory for a book by filename, return (book_info, client) or (None, None).

    When found via the group facade, resolves the owning single-instance client
    so callers can use per-instance operations (e.g. download_book with a bare ID).
    """
    if not filename:
        return None, None
    group = get_grimmory_client()
    if group.is_configured():
        if source_id and ":" in str(source_id):
            parts = str(source_id).split(":")
            instance_id = parts[0]
            client = _resolve_grimmory_instance(instance_id)
            if not client or not client.is_configured():
                return None, None
            if len(parts) == 3:
                book = client.find_book_file_by_source_id(source_id)
                return ({**book, "_instance_id": instance_id}, client) if book else (None, None)
            book_id = parts[1] if len(parts) == 2 else ""
            book = client.find_book_by_filename(filename)
            if book and str(book.get("id")) == book_id:
                return {**book, "_instance_id": instance_id}, client
            return None, None
        book = group.find_book_by_filename(filename)
        if book:
            # Resolve the specific client that owns this book
            instance_id = book.get("_instance_id", "default")
            client = _resolve_grimmory_instance(instance_id)
            return book, client
    return None, None


def _resolve_grimmory_instance(instance_id):
    """Return the single GrimmoryClient for the given instance_id."""
    container = get_container()
    if instance_id == "2":
        return container.grimmory_client_2()
    if instance_id == "default":
        return container.grimmory_client()
    return None


def get_enabled_grimmory_server_ids():
    """Return set of server_ids for enabled Grimmory instances."""
    group = get_grimmory_client()
    active = getattr(group, "_active", None)
    if not isinstance(active, (list, tuple)):
        return set()
    return {c.instance_id for c in active}


def grimmory_cover_proxy_prefix(server_id):
    """Return the cover-proxy URL path prefix for a Grimmory instance."""
    if server_id == "2":
        return "/api/cover-proxy/grimmory2"
    return "/api/cover-proxy/grimmory"


def any_grimmory_configured():
    """Return True if any Grimmory server is configured."""
    return get_grimmory_client().is_configured()


def _grimmory_label(instance_id):
    """Return the user-facing label for a Grimmory instance."""
    if instance_id == "2":
        return os.environ.get("GRIMMORY_2_LABEL", "Grimmory 2")
    return os.environ.get("GRIMMORY_LABEL", "Grimmory")


# --------------- Helper functions ---------------


def get_audiobooks_conditionally():
    """Get audiobooks from configured libraries (ABS_LIBRARY_IDS) or all libraries if not set."""
    return get_abs_service().get_audiobooks()


def get_audiobook_author(ab):
    """Extract author from audiobook metadata."""
    media = ab.get("media", {})
    metadata = media.get("metadata", {})
    return metadata.get("authorName") or (metadata.get("authors") or [{}])[0].get("name", "")


def audiobook_matches_search(ab, search_term):
    """Check if audiobook matches search term (searches title AND author)."""
    manager = get_manager()

    def normalize(s):
        return re.sub(r"[^\w\s]", "", s.lower())

    title = normalize(manager.get_audiobook_title(ab))
    author = normalize(get_audiobook_author(ab))
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
    if sanitize_filename(filename) != filename:
        logger.warning("Rejected unsafe ebook filename")
        return None
    base = ebook_dir if ebook_dir is not None else get_ebook_dir()
    escaped_filename = glob_module.escape(filename)
    for match in base.rglob(escaped_filename):
        if match.is_file() and is_safe_path_within(match, base):
            return match
    return None


def get_kosync_id_for_ebook(
    ebook_filename,
    grimmory_id=None,
    original_filename=None,
    bl_client=None,
    grimmory_file_id=None,
):
    """Get KOSync document ID for an ebook.
    Tries Grimmory API first (if configured and grimmory_id provided),
    falls back to filesystem if needed.
    """
    if sanitize_filename(ebook_filename) != ebook_filename:
        logger.warning("Rejected unsafe ebook filename while computing KoSync ID")
        return None
    if original_filename and sanitize_filename(original_filename) != original_filename:
        original_filename = None

    container = get_container()
    EBOOK_DIR = get_ebook_dir()

    # Try Grimmory API first — use the specific client that reported the ID
    if grimmory_id and bl_client and bl_client.is_configured():
        try:
            if grimmory_file_id is not None:
                content = bl_client.download_book(grimmory_id, file_id=grimmory_file_id)
            else:
                content = bl_client.download_book(grimmory_id)
            if content:
                kosync_id = container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                if kosync_id:
                    logger.debug(f"Computed KOSync ID from Grimmory download: '{kosync_id}'")
                    return kosync_id
        except Exception as e:
            logger.warning(f"Failed to get KOSync ID from Grimmory: {e}")

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
    if is_safe_path_within(cached_path, epub_cache) and cached_path.is_file():
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

                    if abs_client.download_file(target["stream_url"], cached_path):
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
                        if str(res.get("id")) == cwa_id:
                            target = res
                            break

                    if not target and len(results) == 1:
                        target = results[0]

                    if target and target.get("download_url"):
                        logger.info(f"Using direct download link from search for '{target.get('title', 'Unknown')}'")
                    else:
                        logger.debug("Search did not return a usable result, trying direct ID lookup")
                        target = cwa_client.get_book_by_id(cwa_id)

                    if target and target.get("download_url"):
                        if not epub_cache.exists():
                            epub_cache.mkdir(parents=True, exist_ok=True)
                        if cwa_client.download_ebook(target["download_url"], cached_path):
                            logger.info(f"   Downloaded CWA ebook to '{cached_path}'")
                            return container.ebook_parser().get_kosync_id(cached_path)
                    else:
                        logger.warning(f"   Could not find CWA book for ID '{cwa_id}'")
        except Exception as e:
            logger.error(f"   Failed CWA on-demand download: {e}")

    # Neither source available
    if not any_grimmory_configured() and not EBOOK_DIR.exists():
        logger.warning(
            f"Cannot compute KOSync ID for '{ebook_filename}': "
            "Neither Grimmory integration nor /books volume is configured. "
            "Enable Grimmory (GRIMMORY_SERVER, GRIMMORY_USER, GRIMMORY_PASSWORD) "
            "or mount the ebooks directory to /books"
        )
    elif not grimmory_id and not find_ebook_file(ebook_filename):
        logger.warning(
            f"Cannot compute KOSync ID for '{ebook_filename}': File not found in Grimmory, filesystem, or remote sources"
        )

    return None


class EbookResult:
    """Wrapper to provide consistent interface for ebooks from Grimmory, CWA, ABS, or filesystem."""

    def __init__(
        self,
        name,
        title=None,
        subtitle=None,
        authors=None,
        grimmory_id=None,
        path=None,
        source=None,
        source_id=None,
        cover_url=None,
    ):
        self.name = name
        self.title = title or Path(name).stem
        self.subtitle = subtitle or ""
        self.authors = authors or ""
        self.grimmory_id = grimmory_id
        self.path = path
        self.source = source
        self.source_id = source_id or grimmory_id
        self.cover_url = cover_url
        self.has_metadata = grimmory_id is not None or (title is not None and title != name)

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
    """Get ebooks from Grimmory API, filesystem, ABS, and CWA.
    Returns list of EbookResult objects for consistent interface."""
    container = get_container()
    EBOOK_DIR = get_ebook_dir()

    results = []
    found_filenames = set()
    found_grimmory = set()
    found_stems = set()

    # 1. Grimmory (all configured servers)
    bl_group = get_grimmory_client()
    if bl_group.is_configured():
        try:
            books = bl_group.search_books(search_term)
            if books:
                for b in books:
                    fname = b.get("fileName", "")
                    if fname.lower().endswith(".epub"):
                        instance_id = str(b.get("_instance_id") or "default")
                        book_id = b.get("id")
                        file_id = b.get("bookFileId")
                        if book_id is None or file_id is None:
                            continue
                        qualified_id = f"{instance_id}:{book_id}:{file_id}"
                        dedupe_key = qualified_id
                        if dedupe_key in found_grimmory:
                            continue
                        found_grimmory.add(dedupe_key)
                        found_filenames.add(fname.lower())
                        found_stems.add(Path(fname).stem.lower())
                        bl_id = book_id
                        label = _grimmory_label(instance_id)
                        cover_prefix = "grimmory2" if instance_id == "2" else "grimmory"
                        cover = f"/api/cover-proxy/{cover_prefix}/{bl_id}" if bl_id else None
                        results.append(
                            EbookResult(
                                name=fname,
                                title=b.get("title"),
                                subtitle=b.get("subtitle"),
                                authors=b.get("authors"),
                                grimmory_id=qualified_id,
                                source=label,
                                source_id=qualified_id,
                                cover_url=cover,
                            )
                        )
        except Exception as e:
            logger.warning(f"Grimmory search failed: {e}")

    # 2. ABS ebook libraries
    if search_term:
        try:
            abs_service = get_abs_service()
            abs_ebooks = abs_service.search_ebooks(search_term)
            if abs_ebooks:
                for ab in abs_ebooks:
                    ebook_files = abs_service.get_ebook_files(ab["id"])
                    if ebook_files:
                        ef = ebook_files[0]
                        fname = f"{ab['id']}_abs.{ef['ext']}"
                        if fname.lower() not in found_filenames:
                            results.append(
                                EbookResult(
                                    name=fname,
                                    title=ab.get("title"),
                                    authors=ab.get("author"),
                                    source="ABS",
                                    source_id=ab.get("id"),
                                    cover_url=f"/api/cover-proxy/{ab['id']}",
                                )
                            )
                            found_filenames.add(fname.lower())
                            if ab.get("title"):
                                found_stems.add(ab["title"].lower().strip())
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
                            results.append(
                                EbookResult(
                                    name=fname,
                                    title=cr.get("title"),
                                    authors=cr.get("author"),
                                    source="CWA",
                                    source_id=cr.get("id"),
                                )
                            )
                            found_filenames.add(fname.lower())
                            if cr.get("title"):
                                found_stems.add(cr["title"].lower().strip())
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
                    results.append(EbookResult(name=eb.name, path=eb, source="Local File"))
                    found_filenames.add(fname_lower)
                    found_stems.add(stem_lower)

        except Exception as e:
            logger.warning(f"Filesystem search failed: {e}")

    if not results and not EBOOK_DIR.exists() and not any_grimmory_configured():
        logger.warning(
            "No ebooks available: Neither Grimmory integration nor /books volume is configured. "
            "Enable Grimmory (GRIMMORY_SERVER, GRIMMORY_USER, GRIMMORY_PASSWORD) "
            "or mount the ebooks directory to /books"
        )

    return results


def cleanup_mapping_resources(book):
    """Delete external artifacts and membership data for a mapped book."""
    if not book:
        return

    from src.services.mapping_cleanup_service import cleanup_mapping_resources as cleanup_resources

    cleanup_resources(
        book,
        container=get_container(),
        manager=get_manager(),
        database_service=get_database_service(),
        abs_service=get_abs_service(),
        grimmory_client=get_grimmory_client(),
        collection_name=os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader"),
    )


def restart_server():
    """Triggers a graceful restart by sending SIGTERM to the current process.
    The start.sh supervisor loop will catch the exit and restart the application.
    """
    logger.info("Stopping application (Supervisor will restart it)...")
    time.sleep(1.0)
    logger.info("Sending SIGTERM to trigger restart...")
    os.kill(os.getpid(), signal.SIGTERM)


def _has_bookfusion_evidence(match_dict):
    """Check if a match dict has BookFusion-related evidence."""
    if match_dict.get("source_family") == "bookfusion":
        return True
    return any(ev.startswith("bookfusion") for ev in (match_dict.get("evidence") or []))


def serialize_suggestion(s):
    """Shared serializer for PendingSuggestion → JSON-ready dict."""
    matches = []
    for m in s.matches:
        # Skip provenance-only entries (e.g. abs_audiobook markers from reverse suggestions)
        if m.get("source") == "abs_audiobook" and not m.get("action_kind"):
            continue
        matches.append(
            {
                **m,
                "evidence": m.get("evidence") or [],
                "has_bookfusion": _has_bookfusion_evidence(m),
            }
        )

    has_bookfusion_evidence = any(m.get("has_bookfusion") for m in matches)
    return {
        "id": s.id,
        "source_id": s.source_id,
        "source": s.source or "unknown",
        "title": s.title,
        "author": s.author,
        "cover_url": s.cover_url,
        "matches": matches,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "has_bookfusion_evidence": has_bookfusion_evidence,
        "top_match": matches[0] if matches else None,
        "status": "hidden" if s.status == "dismissed" else s.status,
        "hidden": s.status in ("hidden", "dismissed"),
    }


def find_grimmory_metadata(book, grimmory_by_filename):
    """Find best Grimmory metadata entry for a book by filename."""
    for fn in (book.ebook_filename, book.original_ebook_filename):
        if fn:
            candidates = grimmory_by_filename.get(fn.lower(), [])
            match = next((b for b in candidates if b.title), candidates[0] if candidates else None)
            if match:
                return match
    return None


def attempt_hardcover_automatch(container, book):
    """Best-effort Hardcover automatch after book creation."""
    try:
        hc_service = container.hardcover_service()
        if hc_service.is_configured():
            hc_service.automatch_hardcover(book, hardcover_sync_client=container.hardcover_sync_client())
    except Exception as e:
        logger.warning(f"Hardcover automatch failed (book saved): {e}")


def safe_folder_name(name: str) -> str:
    """Sanitize folder name for file system safe usage."""
    invalid = '<>:"/\\|?*'
    name = html.escape(str(name).strip())[:150]
    for c in invalid:
        name = name.replace(c, "_")
    return name.strip() or "Unknown"


def serialize_detected_book(d):
    """Serialize DetectedBook for template context."""
    return {
        "id": d.id,
        "source": d.source,
        "source_id": d.source_id,
        "title": d.title,
        "author": d.author,
        "cover_url": d.cover_url,
        "progress_percentage": d.progress_percentage,
        "first_detected_at": d.first_detected_at.isoformat() if d.first_detected_at else None,
        "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
        "source_updated_at": d.source_updated_at.isoformat() if getattr(d, "source_updated_at", None) else None,
        "matches": d.matches,
        "device": d.device,
        "ebook_filename": d.ebook_filename,
    }
