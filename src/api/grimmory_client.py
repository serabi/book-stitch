import json
import logging
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests

from src.db.models import GrimmoryBook
from src.sync_clients.sync_client_interface import LocatorResult
from src.utils.constants import DEFAULT_SHELF_NAME
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)

GRIMMORY_AUDIO_BOOK_TYPES = {"AUDIOBOOK"}
GRIMMORY_MAX_PAGES = 1000


class GrimmoryClient:
    def __init__(self, database_service=None, env_prefix="GRIMMORY", instance_id="default"):
        self.db = database_service
        self.env_prefix = env_prefix
        self.instance_id = instance_id

        # In-memory cache for performance (populated from DB)
        self._book_cache = {}
        self._book_case_insensitive_cache = {}
        self._book_case_insensitive_filenames = {}
        self._book_id_cache = {}
        self._book_file_cache = {}
        self._book_identity_cache = {}
        self._filename_identities = {}
        self._cache_timestamp = 0

        self._token = None
        self._token_timestamp = 0
        self._token_max_age = 300
        self.session = requests.Session()

        # Load cache from DB (and migrate legacy JSON if needed)
        if self.is_configured():
            self._load_cache()

    def reload_from_env(self):
        """Re-read configuration from os.environ so settings changes take effect without restart.

        Config (base_url/username/password/etc.) is read lazily via properties, but the
        book cache is only populated in __init__ when the client starts configured. A client
        constructed while unconfigured therefore never loads its cache, and a later Settings
        save must re-run the cache load so the singleton becomes usable without a restart.
        """
        self._token = None
        self._token_timestamp = 0
        self.session.headers.clear()

        if self.is_configured():
            self._load_cache()
        else:
            self._reset_book_caches()
            self._cache_timestamp = 0

    @property
    def base_url(self) -> str:
        raw_url = os.environ.get(f"{self.env_prefix}_SERVER", "").rstrip("/")
        if raw_url and not raw_url.lower().startswith(("http://", "https://")):
            raw_url = f"http://{raw_url}"
        return raw_url

    @property
    def username(self) -> str | None:
        return os.environ.get(f"{self.env_prefix}_USER")

    @property
    def password(self) -> str | None:
        return os.environ.get(f"{self.env_prefix}_PASSWORD")

    @property
    def target_library_id(self) -> str | None:
        return os.environ.get(f"{self.env_prefix}_LIBRARY_ID")

    @property
    def legacy_cache_files(self) -> list[Path]:
        """Return legacy cache file paths to check during migration (newest naming first)."""
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        return [
            data_dir / "grimmory_cache.json",
            data_dir / "booklore_cache.json",
        ]

    def _load_cache(self):
        """Load cache from DB, migrating legacy JSON if needed."""
        # 1. Migrate Legacy JSON if it exists and DB is empty
        for legacy_cache_file in self.legacy_cache_files:
            if not legacy_cache_file.exists():
                continue
            try:
                if self.db and not self.db.get_all_grimmory_books(server_id=self.instance_id):
                    logger.info("Grimmory: Migrating legacy JSON cache to SQLite...")
                    with open(legacy_cache_file, encoding="utf-8") as f:
                        data = json.load(f)
                        books = data.get("books", {})
                        count = 0
                        for filename, book_info in books.items():
                            try:
                                b_model = GrimmoryBook(
                                    filename=filename,
                                    title=book_info.get("title"),
                                    authors=book_info.get("authors"),
                                    raw_metadata=json.dumps(book_info),
                                    server_id=self.instance_id,
                                )
                                self.db.save_grimmory_book(b_model)
                                count += 1
                            except (KeyError, TypeError, ValueError) as e:
                                logger.warning(f"Failed to migrate book {filename}: {e}")

                        logger.info(f"Grimmory: Migrated {count} books to database.")

                    try:
                        legacy_cache_file.rename(legacy_cache_file.with_suffix(".json.bak"))
                        logger.info("Grimmory: Legacy cache file renamed to .bak")
                    except Exception as e:
                        logger.warning(f"Could not rename legacy cache file: {e}")
                break  # Only migrate from the first file found
            except Exception as e:
                logger.error(f"Grimmory migration failed: {e}")

        # 2. Load from DB into memory
        if self.db:
            try:
                db_books = self.db.get_all_grimmory_books(server_id=self.instance_id)
                self._reset_book_caches()

                for db_book in db_books:
                    book_info = db_book.raw_metadata_dict
                    if not book_info:
                        book_info = {"fileName": db_book.filename, "title": db_book.title, "authors": db_book.authors}

                    remote_book_id = getattr(db_book, "remote_book_id", None)
                    remote_file_id = getattr(db_book, "remote_file_id", None)
                    if isinstance(remote_book_id, (str, int)):
                        book_info.setdefault("id", remote_book_id)
                    if isinstance(remote_file_id, (str, int)):
                        book_info.setdefault("bookFileId", remote_file_id)
                    self._cache_book_info(db_book.filename, book_info, legacy_row_id=db_book.id)

                # Set to 0 to force a refresh/validation against API on next access
                self._cache_timestamp = 0
                logger.info(f"Grimmory: Loaded {len(self._book_identity_cache)} books from database")
            except Exception as e:
                logger.error(f"Failed to load Grimmory cache from DB: {e}")
                self._reset_book_caches()

    def _reset_book_caches(self):
        self._book_cache = {}
        self._book_case_insensitive_cache = {}
        self._book_case_insensitive_filenames = {}
        self._book_id_cache = {}
        self._book_file_cache = {}
        self._book_identity_cache = {}
        self._filename_identities = {}

    def _cache_book_info(self, filename, book_info, legacy_row_id=None):
        cache_key = str(filename)
        bid = book_info.get("id")
        file_id = book_info.get("bookFileId")
        if bid is not None and file_id is not None:
            identity = ("remote", str(bid), str(file_id))
        else:
            identity = ("legacy", str(legacy_row_id if legacy_row_id is not None else id(book_info)))

        if identity in self._book_identity_cache:
            self._remove_cached_identity(identity)
        self._book_identity_cache[identity] = book_info
        if bid is not None and file_id is not None:
            self._book_file_cache[(str(bid), str(file_id))] = book_info
        self._filename_identities.setdefault(cache_key, set()).add(identity)
        self._refresh_filename_cache(cache_key)

        if bid is not None and (book_info.get("isPrimary") or bid not in self._book_id_cache):
            self._book_id_cache[bid] = book_info

    def _refresh_filename_cache(self, filename):
        identities = self._filename_identities.get(filename, set())
        books = [self._book_identity_cache[identity] for identity in identities if identity in self._book_identity_cache]
        if len(books) == 1:
            self._book_cache[filename] = books[0]
        else:
            self._book_cache.pop(filename, None)
        lookup_key = filename.lower()
        self._book_case_insensitive_filenames.setdefault(lookup_key, set()).add(filename)
        self._update_case_insensitive_cache_entry(lookup_key)

    def _remove_cached_identity(self, identity):
        removed = self._book_identity_cache.pop(identity, None)
        if not removed:
            return
        filename = str(removed.get("fileName", ""))
        identities = self._filename_identities.get(filename)
        if identities is not None:
            identities.discard(identity)
            if not identities:
                self._filename_identities.pop(filename, None)
        bid = removed.get("id")
        file_id = removed.get("bookFileId")
        if bid is not None and file_id is not None:
            self._book_file_cache.pop((str(bid), str(file_id)), None)
        if bid is not None and self._book_id_cache.get(bid) is removed:
            replacement = next(
                (book for book in self._book_identity_cache.values() if book.get("id") == bid),
                None,
            )
            if replacement:
                self._book_id_cache[bid] = replacement
            else:
                self._book_id_cache.pop(bid, None)
        self._refresh_filename_cache(filename)

    def _remove_cached_filename(self, filename):
        cache_key = str(filename)
        identities = self._filename_identities.get(cache_key, set())
        if len(identities) == 1:
            self._remove_cached_identity(next(iter(identities)))

    def _update_case_insensitive_cache_entry(self, lookup_key):
        filenames = self._book_case_insensitive_filenames.get(lookup_key, set())
        cached_filenames = [filename for filename in filenames if filename in self._book_cache]

        if not cached_filenames:
            self._book_case_insensitive_cache.pop(lookup_key, None)
            return

        if len(cached_filenames) == 1:
            self._book_case_insensitive_cache[lookup_key] = self._book_cache[cached_filenames[0]]
            return

        self._book_case_insensitive_cache[lookup_key] = None

    def _rebuild_case_insensitive_cache(self):
        self._book_case_insensitive_cache = {}
        self._book_case_insensitive_filenames = {}
        for filename, book_info in self._book_cache.items():
            lookup_key = filename.lower()
            self._book_case_insensitive_filenames.setdefault(lookup_key, set()).add(filename)
            if lookup_key not in self._book_case_insensitive_cache:
                self._book_case_insensitive_cache[lookup_key] = book_info
            elif self._book_case_insensitive_cache[lookup_key] is not book_info:
                self._book_case_insensitive_cache[lookup_key] = None

    def _get_fresh_token(self):
        if self._token and (time.time() - self._token_timestamp) < self._token_max_age:
            return self._token
        if not all([self.base_url, self.username, self.password]):
            return None
        try:
            response = self.session.post(
                f"{self.base_url}/api/v1/auth/login",
                json={"username": self.username, "password": self.password},
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                self._token = data.get("accessToken") or data.get("token")
                self._token_timestamp = time.time()
                return self._token
            else:
                logger.error(f"Grimmory login failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Grimmory login error: {e}")
        return None

    def _make_request(self, method, endpoint, json_data=None):
        token = self._get_fresh_token()
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{self.base_url}{endpoint}"
        try:
            response = self._dispatch_request(method, url, headers, json_data)

            if response.status_code == 401:
                self._token = None
                token = self._get_fresh_token()
                if not token:
                    return None
                headers["Authorization"] = f"Bearer {token}"
                response = self._dispatch_request(method, url, headers, json_data)
            return response
        except Exception as e:
            logger.error(f"Grimmory API request failed: {e}")
            return None

    def _dispatch_request(self, method, url, headers, json_data=None):
        upper = method.upper()
        if upper == "GET":
            return self.session.get(url, headers=headers, timeout=10)
        elif upper == "POST":
            return self.session.post(url, headers=headers, json=json_data, timeout=10)
        elif upper == "PUT":
            return self.session.put(url, headers=headers, json=json_data, timeout=10)
        raise ValueError(f"Unsupported HTTP method: {method}")

    def is_configured(self):
        """Return True if Grimmory is configured, False otherwise."""
        enabled_val = os.environ.get(f"{self.env_prefix}_ENABLED", "").lower()
        if enabled_val == "false":
            return False
        return bool(self.base_url and self.username and self.password)

    def check_connection(self):
        if not all([self.base_url, self.username, self.password]):
            logger.warning("Grimmory not configured (skipping)")
            return False

        token = self._get_fresh_token()
        if token:
            first_run_marker = "/data/.first_run_done"
            try:
                first_run = not os.path.exists(first_run_marker)
            except Exception:
                first_run = False

            if first_run:
                logger.info(f"Connected to Grimmory at {self.base_url}")
                try:
                    open(first_run_marker, "w").close()
                except Exception:
                    pass
            return True

        logger.error("Grimmory connection failed: could not obtain auth token")
        return False

    def get_libraries(self):
        """Fetch all available libraries to help user configure the bridge."""
        self._get_fresh_token()

        # Strategy 1: Try direct libraries endpoint
        try:
            response = self._make_request("GET", "/api/v1/libraries")
            if response and response.status_code == 200:
                libs = response.json()
                return [
                    {"id": l.get("id"), "name": l.get("name"), "path": l.get("root", {}).get("path") or l.get("path")}
                    for l in libs
                ]
        except Exception as e:
            logger.debug(f"Grimmory: Failed to fetch /api/v1/libraries: {e}")

        # Strategy 2: Fallback - Scan a few books to find unique libraries
        try:
            logger.info("Grimmory: Scanning books to discover libraries...")
            response = self._make_request("GET", "/api/v1/books?page=0&size=50")
            if response and response.status_code == 200:
                data = response.json()
                books = data if isinstance(data, list) else data.get("content", [])

                unique_libs = {}
                for b in books:
                    lid = b.get("libraryId")
                    if lid and lid not in unique_libs:
                        unique_libs[lid] = {
                            "id": lid,
                            "name": b.get("libraryName", "Unknown Library"),
                            "path": "Path not available in book scan",
                        }
                return list(unique_libs.values())
        except Exception as e:
            logger.error(f"Grimmory: Failed to discover libraries via book scan: {e}")

        return []

    def _refresh_book_cache(self):
        """Refresh the book cache using robust pagination."""
        if not self.is_configured():
            return

        all_books_list = []
        seen_page_identities = set()
        page = 0
        batch_size = 200

        logger.info("Grimmory: Starting full library scan...")

        while page < GRIMMORY_MAX_PAGES:
            endpoint = f"/api/v1/books?page={page}&size={batch_size}"
            response = self._make_request("GET", endpoint)

            if not response or response.status_code != 200:
                logger.error(f"Grimmory: Failed to fetch page {page}")
                return False

            data = response.json()

            current_batch = []
            if isinstance(data, list):
                current_batch = data
            elif isinstance(data, dict) and "content" in data:
                current_batch = data["content"]

            raw_batch_size = len(current_batch)
            is_last_page = isinstance(data, dict) and (
                data.get("last") is True
                or (isinstance(data.get("totalPages"), int) and page + 1 >= data["totalPages"])
            )

            if not current_batch:
                break

            raw_identities = {
                (str(book.get("id")), str(book_file.get("id") or book_file.get("fileName")))
                for book in current_batch
                for book_file, _is_primary in self._iter_book_files(book)
                if book.get("id") is not None and (book_file.get("id") is not None or book_file.get("fileName"))
            }
            if isinstance(data, list) and page > 0 and not (raw_identities - seen_page_identities):
                logger.warning("Grimmory: Stopping pagination after repeated raw list page %s", page)
                break
            seen_page_identities.update(raw_identities)

            if self.target_library_id and current_batch:
                filtered_batch = []
                for b in current_batch:
                    lid = b.get("libraryId")
                    lname = b.get("libraryName", "Unknown")

                    if lid is not None and str(lid) == str(self.target_library_id):
                        filtered_batch.append(b)
                    elif lid is None:
                        filtered_batch.append(b)
                    else:
                        logger.debug(f"Grimmory: Ignoring book '{b.get('title')}' in Library '{lname}' (ID: {lid})")
                current_batch = filtered_batch

            all_books_list.extend(current_batch)
            logger.debug(f"Grimmory: Fetched page {page} ({len(current_batch)} items)")

            if is_last_page or raw_batch_size != batch_size:
                break

            page += 1

        if page >= GRIMMORY_MAX_PAGES:
            logger.warning("Grimmory: Stopped library scan at the %s-page safety ceiling", GRIMMORY_MAX_PAGES)

        if not all_books_list:
            logger.debug("Grimmory: No books found in library")
            self._reset_book_caches()
            if self.db:
                try:
                    self.db.reconcile_grimmory_books(self.instance_id, [])
                except Exception as e:
                    logger.error(f"Failed to reconcile empty Grimmory book cache: {e}")
            self._cache_timestamp = time.time()
            return True

        logger.info(f"Grimmory: Scan complete. Found {len(all_books_list)} total books.")

        live_files = {
            (str(book.get("id")), str(book_file.get("id"))): str(book_file.get("fileName", "")).strip()
            for book in all_books_list
            for book_file, _is_primary in self._iter_book_files(book)
            if book.get("id") is not None and book_file.get("id") is not None
        }
        live_legacy_files = {
            (str(book.get("id")), str(book_file.get("fileName", "")).strip())
            for book in all_books_list
            for book_file, _is_primary in self._iter_book_files(book)
            if book.get("id") is not None and book_file.get("fileName")
        }

        stale_count = 0
        for identity, book_info in list(self._book_identity_cache.items()):
            filename = str(book_info.get("fileName", ""))
            book_id = book_info.get("id")
            file_id = book_info.get("bookFileId")
            cached_filename = str(book_info.get("fileName", filename)).strip()
            if file_id is not None:
                live_filename = live_files.get((str(book_id), str(file_id)))
                is_stale = live_filename is None or live_filename != cached_filename
            else:
                is_stale = (str(book_id), cached_filename) not in live_legacy_files

            if not is_stale:
                continue
            stale_count += 1
            self._remove_cached_identity(identity)

        if stale_count:
            logger.info(f"Grimmory: Pruned {stale_count} stale books from database.")

        persisted_books = []
        for book in all_books_list:
            persisted_books.extend(self._process_book_detail(book, persist=False))
        if self.db:
            try:
                self.db.reconcile_grimmory_books(self.instance_id, persisted_books)
            except Exception as e:
                logger.error(f"Failed to reconcile Grimmory book cache: {e}")

        self._cache_timestamp = time.time()
        return True

    @staticmethod
    def _iter_book_files(detail):
        """Yield the primary and alternative BookFile DTOs once each."""
        candidates = []
        primary = detail.get("primaryFile")
        if isinstance(primary, dict):
            candidates.append((primary, True))
        alternatives = detail.get("alternativeFormats") or []
        if isinstance(alternatives, dict):
            alternatives = list(alternatives.values())
        candidates.extend((book_file, False) for book_file in alternatives if isinstance(book_file, dict))
        if not candidates and detail.get("fileName"):
            candidates.append((detail, True))

        seen = set()
        for book_file, is_primary in candidates:
            filename = book_file.get("fileName")
            if not filename:
                continue
            identity = (book_file.get("id"), filename)
            if identity in seen:
                continue
            seen.add(identity)
            yield book_file, is_primary

    def _process_book_detail(self, detail, persist=True):
        """Cache every actual file attached to one Grimmory Book DTO."""
        if self.target_library_id:
            lid = detail.get("libraryId")
            if lid is not None and str(lid) != str(self.target_library_id):
                return []

        persisted_books = []

        metadata = detail.get("metadata") or {}
        authors = metadata.get("authors") or []
        author_list = []
        for a in authors:
            if isinstance(a, dict):
                name = a.get("name", "")
                if name:
                    author_list.append(name)
            elif isinstance(a, str) and a.strip():
                author_list.append(a.strip())

        author_str = ", ".join(author_list)
        subtitle = metadata.get("subtitle") or ""
        for book_file, is_primary in self._iter_book_files(detail):
            filename = book_file["fileName"]
            title = metadata.get("title") or detail.get("title") or filename
            book_info = {
                "id": detail.get("id"),
                "fileName": filename,
                "filePath": book_file.get("filePath", ""),
                "title": title,
                "subtitle": subtitle,
                "authors": author_str,
                "bookType": book_file.get("bookType", ""),
                "bookFileId": book_file.get("id"),
                "isPrimary": is_primary,
                "lastReadTime": detail.get("lastReadTime"),
                "epubProgress": detail.get("epubProgress"),
                "pdfProgress": detail.get("pdfProgress"),
                "cbxProgress": detail.get("cbxProgress"),
                "koreaderProgress": detail.get("koreaderProgress"),
                "audiobookProgress": detail.get("audiobookProgress"),
            }

            self._cache_book_info(filename, book_info)
            model = GrimmoryBook(
                filename=filename,
                title=title,
                authors=author_str,
                raw_metadata=json.dumps(book_info),
                server_id=self.instance_id,
                remote_book_id=detail.get("id"),
                remote_file_id=book_file.get("id"),
            )
            persisted_books.append(model)
            if self.db and persist:
                try:
                    self.db.save_grimmory_book(model)
                except Exception as e:
                    logger.error(f"Failed to persist book {filename} to DB: {e}")
        return persisted_books

    def extract_progress(self, book_info: dict) -> tuple[float | None, str | None]:
        """Extract (percentage_as_fraction, cfi) from any book type's progress."""
        if (book_info.get("bookType") or "").upper() in GRIMMORY_AUDIO_BOOK_TYPES:
            progress = book_info.get("audiobookProgress")
            if progress is not None and progress.get("percentage") is not None:
                return progress["percentage"] / 100.0, None
        for key in ("epubProgress", "pdfProgress", "cbxProgress"):
            progress = book_info.get(key)
            if progress is not None and progress.get("percentage") is not None:
                pct = progress["percentage"]
                return (pct / 100.0, progress.get("cfi"))
        return None, None

    def audio_source_id(self, book_info: dict) -> str | None:
        """Return the exact instance/book/file identity for a Grimmory audiobook."""
        if (book_info.get("bookType") or "").upper() not in GRIMMORY_AUDIO_BOOK_TYPES:
            return None
        return self.book_file_source_id(book_info)

    def book_file_source_id(self, book_info: dict) -> str | None:
        """Return the exact instance/book/file identity for any Grimmory BookFile."""
        book_id = book_info.get("id")
        file_id = book_info.get("bookFileId")
        if book_id is None or file_id is None:
            return None
        return f"{getattr(self, 'instance_id', 'default')}:{book_id}:{file_id}"

    def find_book_file_by_source_id(self, source_id, allow_refresh=True):
        """Resolve any exact qualified BookFile identity without filename matching."""
        try:
            instance_id, book_id, file_id = str(source_id).split(":", 2)
        except ValueError:
            return None
        if instance_id != str(getattr(self, "instance_id", "default")):
            return None

        self._ensure_fresh_cache(allow_refresh)
        book = self._book_file_cache.get((book_id, file_id))
        if not book or self.book_file_source_id(book) != str(source_id):
            return None
        return book

    def find_audiobook_by_source_id(self, source_id, allow_refresh=True):
        """Resolve an exact qualified audiobook identity without filename matching."""
        book = self.find_book_file_by_source_id(source_id, allow_refresh=allow_refresh)
        if not book or (book.get("bookType") or "").upper() not in GRIMMORY_AUDIO_BOOK_TYPES:
            return None
        return book

    def get_audio_files(self, source_id):
        """Return an authenticated download descriptor for one exact audiobook file."""
        book = self.find_audiobook_by_source_id(source_id)
        token = self._get_fresh_token()
        if not book or not token:
            return []

        book_id = book.get("id")
        file_id = book.get("bookFileId")
        filename = book.get("fileName") or "audiobook.mp3"
        if book_id is None or file_id is None:
            return []

        return [
            {
                "stream_url": f"{self.base_url}/api/v1/books/{book_id}/files/{file_id}/download",
                "ext": Path(filename).suffix.lstrip(".") or "mp3",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        ]

    def get_book_file_progress(self, source_id):
        book = self.find_book_file_by_source_id(source_id)
        return self.extract_progress(book) if book else (None, None)

    def get_audiobook_progress(self, source_id):
        book = self.find_audiobook_by_source_id(source_id)
        return self.extract_progress(book) if book else (None, None)

    def _normalize_string(self, s):
        """Remove non-alphanumeric characters and lowercase."""
        if not s:
            return ""
        return re.sub(r"[\W_]+", "", s.lower())

    def _ensure_fresh_cache(self, allow_refresh=True):
        """Refresh the in-memory book cache if it is empty or older than an hour."""
        if not allow_refresh:
            return
        if not self._book_identity_cache or time.time() - self._cache_timestamp > 3600:
            self._refresh_book_cache()

    def find_book_by_filename(self, ebook_filename, allow_refresh=True):
        """Find a book by its filename using exact, stem, or normalized matching."""
        self._ensure_fresh_cache(allow_refresh)

        target_name = Path(ebook_filename).name

        # 1. Exact Filename Match
        if target_name in self._book_cache:
            return self._book_cache[target_name]

        lookup_key = target_name.lower()
        if lookup_key in self._book_case_insensitive_cache:
            book_info = self._book_case_insensitive_cache[lookup_key]
            if book_info is not None:
                return book_info
            logger.debug(f"Grimmory: Ambiguous case-insensitive filename match for {sanitize_log_data(ebook_filename)}")
            return None

        target_stem = Path(ebook_filename).stem.lower()

        # 2. Strict Stem Match
        for cached_name, book_info in list(self._book_cache.items()):
            if Path(cached_name).stem.lower() == target_stem:
                return book_info

        # 3. Partial Stem Match
        for cached_name, book_info in list(self._book_cache.items()):
            cached_name_lower = cached_name.lower()
            if target_stem in cached_name_lower or cached_name_lower.replace(".epub", "") in target_stem:
                return book_info

        # 4. Fuzzy / Normalized Match
        target_norm = self._normalize_string(target_stem)
        if len(target_norm) > 5:
            best_match = None
            best_ratio = 0.0

            for cached_name, book_info in list(self._book_cache.items()):
                cached_norm = self._normalize_string(Path(cached_name).stem)
                ratio = SequenceMatcher(None, target_norm, cached_norm).ratio()

                if ratio > 0.90 and ratio > best_ratio:
                    best_ratio = ratio
                    best_match = (cached_name, book_info)

            if best_match:
                logger.debug(f"Fuzzy match: '{target_stem}' ~= '{best_match[0]}' (similarity: {best_ratio:.1%})")
                return best_match[1]

        # If not found, try refreshing cache once
        if allow_refresh and time.time() - self._cache_timestamp > 60:
            if self._refresh_book_cache():
                return self.find_book_by_filename(ebook_filename, allow_refresh=False)

        return None

    def get_all_books(self):
        """Get all books from cache, refreshing if necessary."""
        self._ensure_fresh_cache()
        return list(self._book_identity_cache.values())

    def search_books(self, search_term):
        """Search books by title, author, or filename. Returns list of matching books."""
        self._ensure_fresh_cache()

        if not search_term:
            return list(self._book_identity_cache.values())

        search_lower = search_term.lower()
        search_norm = self._normalize_string(search_term)

        results = []
        for book_info in list(self._book_identity_cache.values()):
            title = (book_info.get("title") or "").lower()
            authors = (book_info.get("authors") or "").lower()
            filename = (book_info.get("fileName") or "").lower()

            if search_lower in title or search_lower in authors or search_lower in filename:
                results.append(book_info)
                continue

            title_norm = self._normalize_string(title)
            authors_norm = self._normalize_string(authors)
            filename_norm = self._normalize_string(filename)

            if len(search_norm) > 3:
                if search_norm in title_norm or search_norm in authors_norm or search_norm in filename_norm:
                    results.append(book_info)

        return results

    def download_book(self, book_id, file_id=None):
        """Download a primary book file or an exact selected alternative."""
        token = self._get_fresh_token()
        if not token:
            return None

        headers = {"Authorization": f"Bearer {token}"}
        if file_id is not None:
            selected = self._book_file_cache.get((str(book_id), str(file_id)))
            if not selected:
                self._refresh_book_cache()
                selected = self._book_file_cache.get((str(book_id), str(file_id)))
            if not selected or str(selected.get("id")) != str(book_id) or str(selected.get("bookFileId")) != str(file_id):
                logger.warning("Grimmory: Refusing download for stale book/file identity %s/%s", book_id, file_id)
                return None
            url = f"{self.base_url}/api/v1/books/{book_id}/files/{file_id}/download"
        else:
            url = f"{self.base_url}/api/v1/books/{book_id}/download"
        logger.debug(f"Downloading book from {url}")

        try:
            response = self.session.get(url, headers=headers, timeout=60)

            if file_id is None and response.status_code == 404:
                file_url = f"{self.base_url}/api/v1/books/{book_id}/file"
                logger.debug(f"404 on /download, trying fallback: {file_url}")
                response = self.session.get(file_url, headers=headers, timeout=60)

            if response.status_code != 200:
                logger.error(f"Failed to download book: {response.status_code}")
                return None

            return response.content
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None

    def get_progress(self, ebook_filename, instance_id=None):
        if instance_id is not None and str(instance_id) != str(getattr(self, "instance_id", "default")):
            return None, None
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            return None, None
        return self.extract_progress(book)

    def update_progress(self, ebook_filename, percentage, rich_locator: LocatorResult | None = None):
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            logger.debug(f"Grimmory: Book not found: {ebook_filename}")
            return False

        return self._update_book_progress(book, ebook_filename, percentage, rich_locator)

    def update_audiobook_progress(self, source_id, percentage):
        book = self.find_audiobook_by_source_id(source_id)
        if not book:
            logger.debug("Grimmory: Audiobook identity no longer matches: %s", sanitize_log_data(source_id))
            return False
        return self._update_book_progress(book, source_id, percentage)

    def _update_book_progress(self, book, display_name, percentage, rich_locator: LocatorResult | None = None):
        book_id = book["id"]
        book_type = (book.get("bookType") or "").upper()
        book_file_id = book.get("bookFileId")
        pct_display = percentage * 100
        cfi = rich_locator.cfi if rich_locator and rich_locator.cfi else None
        href = rich_locator.href if rich_locator and rich_locator.href else None

        if book_file_id:
            file_progress = {
                "bookFileId": book_file_id,
                "progressPercent": pct_display,
            }
            if cfi:
                file_progress["positionData"] = cfi
            if href:
                file_progress["positionHref"] = href
            payload = {"bookId": book_id, "fileProgress": file_progress}
        elif book_type == "EPUB":
            payload = {"bookId": book_id, "epubProgress": {"percentage": pct_display}}
            if cfi:
                payload["epubProgress"]["cfi"] = cfi
        elif book_type in ("PDF", "CBX"):
            progress_key = f"{book_type.lower()}Progress"
            payload = {"bookId": book_id, progress_key: {"percentage": pct_display}}
        else:
            logger.warning(f"Grimmory: Unknown book type {book_type} for {sanitize_log_data(display_name)}")
            return False

        response = self._make_request("POST", "/api/v1/books/progress", payload)
        if response and response.status_code in [200, 201, 204]:
            logger.info(f"Grimmory: {sanitize_log_data(display_name)} -> {pct_display:.1f}%")
            try:
                cached = book
                if cached:
                    progress_keys = {
                        "EPUB": "epubProgress",
                        "PDF": "pdfProgress",
                        "CBX": "cbxProgress",
                        **{kind: "audiobookProgress" for kind in GRIMMORY_AUDIO_BOOK_TYPES},
                    }
                    cached_type = (cached.get("bookType") or "").upper()
                    pk = progress_keys.get(cached_type)
                    if pk:
                        if not cached.get(pk):
                            cached[pk] = {}
                        cached[pk]["percentage"] = pct_display
                        if cfi and pk == "epubProgress":
                            cached[pk]["cfi"] = cfi
                    logger.debug(f"Grimmory: Cache updated in-place for book {book_id}")
            except Exception:
                logger.debug("Grimmory: In-place cache update failed, will refresh on next read")
            return True
        else:
            status = response.status_code if response else "No response"
            logger.error(f"Grimmory update failed: {status}")
            return False

    def update_read_status(self, ebook_filename, status):
        """Update the read status for a book in Grimmory.

        Args:
            ebook_filename: The ebook filename to look up.
            status: One of 'UNREAD', 'READING', 'RE_READING', 'READ',
                    'PARTIALLY_READ', 'PAUSED', 'WONT_READ', 'ABANDONED'.

        Grimmory auto-sets dateFinished when status is set to READ.
        """
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            logger.debug(f"Grimmory: Cannot update read status -- book not found: {ebook_filename}")
            return False

        book_id = book["id"]
        payload = {"bookIds": [book_id], "status": status}
        response = self._make_request("POST", "/api/v1/books/status", payload)
        if response and response.status_code in [200, 201, 204]:
            logger.info(f"Grimmory: Set read status '{status}' for {sanitize_log_data(ebook_filename)}")
            return True
        else:
            resp_status = response.status_code if response else "No response"
            logger.warning(
                f"Grimmory: Failed to set read status for {sanitize_log_data(ebook_filename)}: {resp_status}"
            )
            return False

    def get_recent_activity(self, min_progress=0.01):
        if not self._book_identity_cache:
            self._refresh_book_cache()
        results = []
        for book in list(self._book_identity_cache.values()):
            progress, _ = self.extract_progress(book)
            if progress is not None and progress >= min_progress:
                results.append(
                    {"id": book["id"], "filename": book["fileName"], "progress": progress, "source": "grimmory"}
                )
        return results

    def add_to_shelf(self, ebook_filename, shelf_name=None):
        """Add a book to a shelf, creating the shelf if it doesn't exist."""
        if not shelf_name:
            shelf_name = os.environ.get(f"{self.env_prefix}_SHELF_NAME") or DEFAULT_SHELF_NAME

        try:
            book = self.find_book_by_filename(ebook_filename)
            if not book:
                logger.warning(f"Grimmory: Book not found for shelf assignment: {sanitize_log_data(ebook_filename)}")
                return False

            shelves_response = self._make_request("GET", "/api/v1/shelves")
            if not shelves_response or shelves_response.status_code != 200:
                logger.error("Failed to get Grimmory shelves")
                return False

            shelves = shelves_response.json()
            target_shelf = next((s for s in shelves if s.get("name") == shelf_name), None)

            if not target_shelf:
                create_response = self._make_request(
                    "POST", "/api/v1/shelves", {"name": shelf_name, "icon": "pi pi-book", "iconType": "PRIME_NG"}
                )
                if not create_response or create_response.status_code != 201:
                    logger.error(f"Failed to create Grimmory shelf: {shelf_name}")
                    return False
                target_shelf = create_response.json()

            assign_response = self._make_request(
                "POST",
                "/api/v1/books/shelves",
                {"bookIds": [book["id"]], "shelvesToAssign": [target_shelf["id"]], "shelvesToUnassign": []},
            )

            if assign_response and assign_response.status_code in [200, 201, 204]:
                logger.info(f"Added '{sanitize_log_data(ebook_filename)}' to Grimmory Shelf: {shelf_name}")
                return True
            else:
                logger.error(
                    f"Failed to assign book to shelf. Status: {assign_response.status_code if assign_response else 'No response'}"
                )
                return False

        except Exception as e:
            logger.error(f"Error adding book to Grimmory shelf: {e}")
            return False

    def remove_from_shelf(self, ebook_filename, shelf_name=None):
        """Remove a book from a shelf."""
        if not shelf_name:
            shelf_name = os.environ.get(f"{self.env_prefix}_SHELF_NAME") or DEFAULT_SHELF_NAME

        try:
            book = self.find_book_by_filename(ebook_filename)
            if not book:
                logger.warning(f"Grimmory: Book not found for shelf removal: {sanitize_log_data(ebook_filename)}")
                return False

            shelves_response = self._make_request("GET", "/api/v1/shelves")
            if not shelves_response or shelves_response.status_code != 200:
                logger.error("Failed to get Grimmory shelves")
                return False

            shelves = shelves_response.json()
            target_shelf = next((s for s in shelves if s.get("name") == shelf_name), None)

            if not target_shelf:
                logger.warning(f"Shelf '{shelf_name}' not found")
                return False

            assign_response = self._make_request(
                "POST",
                "/api/v1/books/shelves",
                {"bookIds": [book["id"]], "shelvesToAssign": [], "shelvesToUnassign": [target_shelf["id"]]},
            )

            if assign_response and assign_response.status_code in [200, 201, 204]:
                logger.info(f"Removed '{sanitize_log_data(ebook_filename)}' from Grimmory Shelf: {shelf_name}")
                return True
            else:
                logger.error(
                    f"Failed to remove book from shelf. Status: {assign_response.status_code if assign_response else 'No response'}"
                )
                return False

        except Exception as e:
            logger.error(f"Error removing book from Grimmory shelf: {e}")
            return False


class GrimmoryClientGroup:
    """Facade that wraps multiple GrimmoryClient instances for cross-server queries.

    Presents the same duck-typed interface that services expect from a single
    GrimmoryClient, but aggregates results across all configured instances.
    """

    def __init__(self, clients: list):
        self.clients = [c for c in (clients or []) if c]

    @property
    def _active(self):
        return [c for c in self.clients if c.is_configured()]

    def is_configured(self) -> bool:
        return any(c.is_configured() for c in self.clients)

    def get_all_books(self) -> list:
        results = []
        for c in self._active:
            for book in c.get_all_books():
                results.append({**book, "_instance_id": c.instance_id})
        return results

    def find_book_by_filename(self, ebook_filename, allow_refresh=True):
        for c in self._active:
            result = c.find_book_by_filename(ebook_filename, allow_refresh=allow_refresh)
            if result:
                return {**result, "_instance_id": c.instance_id}
        return None

    def search_books(self, search_term) -> list:
        results = []
        for c in self._active:
            for book in c.search_books(search_term):
                results.append({**book, "_instance_id": c.instance_id})
        return results

    def find_audiobook_by_source_id(self, source_id, allow_refresh=True):
        for client in self._active:
            book = client.find_audiobook_by_source_id(source_id, allow_refresh=allow_refresh)
            if book:
                return {**book, "_instance_id": client.instance_id}
        return None

    def find_book_file_by_source_id(self, source_id, allow_refresh=True):
        for client in self._active:
            book = client.find_book_file_by_source_id(source_id, allow_refresh=allow_refresh)
            if book:
                return {**book, "_instance_id": client.instance_id}
        return None

    def get_audio_files(self, source_id):
        for client in self._active:
            if str(source_id).startswith(f"{client.instance_id}:"):
                return client.get_audio_files(source_id)
        return []

    def get_book_file_progress(self, source_id):
        for client in self._active:
            pct, locator = client.get_book_file_progress(source_id)
            if pct is not None:
                return pct, locator
        return None, None

    def get_audiobook_progress(self, source_id):
        for client in self._active:
            pct, locator = client.get_audiobook_progress(source_id)
            if pct is not None:
                return pct, locator
        return None, None

    def update_audiobook_progress(self, source_id, percentage):
        for client in self._active:
            if client.find_audiobook_by_source_id(source_id):
                return client.update_audiobook_progress(source_id, percentage)
        return False

    def download_book(self, book_id, file_id=None):
        """Download from whichever client owns the book.

        book_id may be a plain int/str (legacy, tries all clients) or
        qualified as 'instance_id:book_id'.
        """
        bid_str = str(book_id)
        if ":" in bid_str:
            target_instance, raw_id = bid_str.split(":", 1)
            for c in self._active:
                if c.instance_id == target_instance:
                    return c.download_book(raw_id, file_id=file_id)
            return None

        for c in self._active:
            result = c.download_book(book_id, file_id=file_id)
            if result:
                return result
        return None

    @property
    def base_url(self):
        for c in self._active:
            return c.base_url
        return None

    def remove_from_shelf(self, ebook_filename, shelf_name=None, instance_id=None):
        for c in self._active:
            if instance_id is not None and str(c.instance_id) != str(instance_id):
                continue
            if c.remove_from_shelf(ebook_filename, shelf_name):
                return True
        return False

    def add_to_shelf(self, ebook_filename, shelf_name=None):
        for c in self._active:
            if c.add_to_shelf(ebook_filename, shelf_name):
                return True
        return False

    def get_progress(self, ebook_filename, instance_id=None):
        for c in self._active:
            if instance_id is not None and str(c.instance_id) != str(instance_id):
                continue
            pct, cfi = c.get_progress(ebook_filename)
            if pct is not None:
                return pct, cfi
        return None, None
