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

                    self._cache_book_info(db_book.filename, book_info)

                # Set to 0 to force a refresh/validation against API on next access
                self._cache_timestamp = 0
                logger.info(f"Grimmory: Loaded {len(self._book_cache)} books from database")
            except Exception as e:
                logger.error(f"Failed to load Grimmory cache from DB: {e}")
                self._reset_book_caches()

    def _reset_book_caches(self):
        self._book_cache = {}
        self._book_case_insensitive_cache = {}
        self._book_case_insensitive_filenames = {}
        self._book_id_cache = {}

    def _cache_book_info(self, filename, book_info):
        cache_key = str(filename)
        self._book_cache[cache_key] = book_info
        lookup_key = cache_key.lower()
        self._book_case_insensitive_filenames.setdefault(lookup_key, set()).add(cache_key)
        self._update_case_insensitive_cache_entry(lookup_key)

        bid = book_info.get("id")
        if bid:
            self._book_id_cache[bid] = book_info

    def _remove_cached_filename(self, filename):
        cache_key = str(filename)
        self._book_cache.pop(cache_key, None)
        lookup_key = cache_key.lower()
        filenames = self._book_case_insensitive_filenames.get(lookup_key)
        if filenames is not None:
            filenames.discard(cache_key)
            if not filenames:
                self._book_case_insensitive_filenames.pop(lookup_key, None)
        self._update_case_insensitive_cache_entry(lookup_key)

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
        page = 0
        batch_size = 200

        logger.info("Grimmory: Starting full library scan...")

        while True:
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

            if not current_batch:
                break

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

            if len(current_batch) != batch_size:
                break

            page += 1

        if not all_books_list:
            logger.debug("Grimmory: No books found in library")
            self._reset_book_caches()
            self._cache_timestamp = time.time()
            return True

        logger.info(f"Grimmory: Scan complete. Found {len(all_books_list)} total books.")

        # Prune stale data
        if self.db and all_books_list:
            live_map = {str(b["id"]): b for b in all_books_list if b.get("id")}

            cached_filenames = list(self._book_cache.keys())
            stale_count = 0

            for fname in cached_filenames:
                book_info = self._book_cache[fname]
                bid = book_info.get("id")

                is_stale = False

                if not bid or str(bid) not in live_map:
                    is_stale = True
                    logger.debug(f"   Pruning {fname}: ID {bid} not in live map")
                else:
                    live_book = live_map[str(bid)]
                    raw_live_filename = live_book.get("primaryFile", {}).get("fileName", live_book.get("fileName", ""))
                    live_filename = str(raw_live_filename).strip() if raw_live_filename else ""
                    cached_real_filename = book_info.get("fileName", fname)

                    if live_filename and live_filename != str(cached_real_filename).strip():
                        is_stale = True
                        logger.debug(
                            f"   Pruning {fname}: Filename mismatch. Live: {repr(raw_live_filename)} vs Cache: {repr(cached_real_filename)}"
                        )

                if is_stale:
                    stale_count += 1
                    self._remove_cached_filename(fname)
                    if bid:
                        self._book_id_cache.pop(bid, None)

                    try:
                        self.db.delete_grimmory_book(fname, server_id=self.instance_id)
                    except Exception as e:
                        logger.error(f"Failed to prune stale book {fname}: {e}")

            if stale_count > 0:
                logger.info(f"Grimmory: Pruned {stale_count} stale books from database.")

        for book in all_books_list:
            if book["id"] not in self._book_id_cache:
                self._process_book_detail(book)
            else:
                self._update_cached_progress(book)

        self._cache_timestamp = time.time()
        return True

    def _process_book_detail(self, detail):
        """Process a book detail response and add to cache."""
        if self.target_library_id:
            lid = detail.get("libraryId")
            if lid is not None and str(lid) != str(self.target_library_id):
                return

        primary_file = detail.get("primaryFile", {})
        filename = primary_file.get("fileName", detail.get("fileName", ""))
        filepath = primary_file.get("filePath", detail.get("filePath", ""))
        book_type = primary_file.get("bookType", detail.get("bookType", ""))
        if not filename:
            return

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
        title = metadata.get("title") or detail.get("title") or filename

        book_info = {
            "id": detail.get("id"),
            "fileName": filename,
            "filePath": filepath,
            "title": title,
            "subtitle": subtitle,
            "authors": author_str,
            "bookType": book_type,
            "bookFileId": primary_file.get("id"),
            "lastReadTime": detail.get("lastReadTime"),
            "epubProgress": detail.get("epubProgress"),
            "pdfProgress": detail.get("pdfProgress"),
            "cbxProgress": detail.get("cbxProgress"),
            "koreaderProgress": detail.get("koreaderProgress"),
            # Identifiers for ABS->Grimmory matching (may be absent on older cache rows)
            "isbn13": metadata.get("isbn13"),
            "isbn10": metadata.get("isbn10"),
            "asin": metadata.get("asin"),
            "googleId": metadata.get("googleId"),
        }

        self._cache_book_info(filename, book_info)

        if self.db:
            try:
                b_model = GrimmoryBook(
                    filename=filename,
                    title=title,
                    authors=author_str,
                    raw_metadata=json.dumps(book_info),
                    server_id=self.instance_id,
                )
                self.db.save_grimmory_book(b_model)
            except Exception as e:
                logger.error(f"Failed to persist book {filename} to DB: {e}")

    def _update_cached_progress(self, detail):
        """Update progress fields on an already-cached book in-place."""
        cached = self._book_id_cache.get(detail.get("id"))
        if not cached:
            return
        primary_file = detail.get("primaryFile", {})
        for key in ("epubProgress", "pdfProgress", "cbxProgress", "koreaderProgress"):
            val = detail.get(key)
            if val is not None:
                cached[key] = val
        if primary_file.get("id"):
            cached["bookFileId"] = primary_file["id"]
        if detail.get("lastReadTime"):
            cached["lastReadTime"] = detail["lastReadTime"]

    def extract_progress(self, book_info: dict) -> tuple[float | None, str | None]:
        """Extract (percentage_as_fraction, cfi) from any book type's progress."""
        for key in ("epubProgress", "pdfProgress", "cbxProgress"):
            progress = book_info.get(key)
            if progress is not None and progress.get("percentage") is not None:
                pct = progress["percentage"]
                return (pct / 100.0, progress.get("cfi"))
        return None, None

    def _normalize_string(self, s):
        """Remove non-alphanumeric characters and lowercase."""
        if not s:
            return ""
        return re.sub(r"[\W_]+", "", s.lower())

    def _ensure_fresh_cache(self, allow_refresh=True):
        """Refresh the in-memory book cache if it is empty or older than an hour."""
        if not allow_refresh:
            return
        if not self._book_cache or time.time() - self._cache_timestamp > 3600:
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
        return list(self._book_cache.values())

    def search_books(self, search_term):
        """Search books by title, author, or filename. Returns list of matching books."""
        self._ensure_fresh_cache()

        if not search_term:
            return list(self._book_cache.values())

        search_lower = search_term.lower()
        search_norm = self._normalize_string(search_term)

        results = []
        for book_info in list(self._book_cache.values()):
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

    def download_book(self, book_id):
        """Download book content by ID. Returns bytes or None."""
        token = self._get_fresh_token()
        if not token:
            return None

        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.base_url}/api/v1/books/{book_id}/download"
        logger.debug(f"Downloading book from {url}")

        try:
            response = self.session.get(url, headers=headers, timeout=60)

            if response.status_code == 404:
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

    def get_progress(self, ebook_filename):
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            return None, None
        return self.extract_progress(book)

    def update_progress(self, ebook_filename, percentage, rich_locator: LocatorResult | None = None):
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            logger.debug(f"Grimmory: Book not found: {ebook_filename}")
            return False

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
            logger.warning(f"Grimmory: Unknown book type {book_type} for {sanitize_log_data(ebook_filename)}")
            return False

        response = self._make_request("POST", "/api/v1/books/progress", payload)
        if response and response.status_code in [200, 201, 204]:
            logger.info(f"Grimmory: {sanitize_log_data(ebook_filename)} -> {pct_display:.1f}%")
            try:
                cached = self._book_id_cache.get(book_id)
                if cached:
                    progress_keys = {"EPUB": "epubProgress", "PDF": "pdfProgress", "CBX": "cbxProgress"}
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

        return self.update_read_status_by_id(book["id"], status)

    def update_read_status_by_id(self, book_id, status):
        """Update the read status for a known numeric Grimmory book id.

        Args:
            book_id: The numeric Grimmory book id.
            status: One of 'UNREAD', 'READING', 'RE_READING', 'READ',
                    'PARTIALLY_READ', 'PAUSED', 'WONT_READ', 'ABANDONED'.

        Grimmory auto-sets dateFinished when status is set to READ.
        """
        payload = {"bookIds": [book_id], "status": status}
        response = self._make_request("POST", "/api/v1/books/status", payload)
        if response and response.status_code in [200, 201, 204]:
            logger.info(f"Grimmory: Set read status '{status}' for book {book_id}")
            return True
        resp_status = response.status_code if response else "No response"
        logger.warning(f"Grimmory: Failed to set read status for book {book_id}: {resp_status}")
        return False

    @staticmethod
    def _to_instant(date_finished):
        """Normalize a finish date to a full ISO-8601 instant for Grimmory.

        Grimmory's API binds dateFinished to a java.time.Instant, which cannot be
        deserialized from a bare YYYY-MM-DD. Expand a date-only value to midnight
        UTC; pass an already-instant value (anything with a time component) through.
        """
        if isinstance(date_finished, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_finished):
            return f"{date_finished}T00:00:00Z"
        return date_finished

    def set_finished_date(self, book_id, book_type, date_finished):
        """Set an explicit finish date on a Grimmory book via the progress endpoint.

        Grimmory auto-dates to "now" on status=READ; this carries over the original
        finish date (e.g. from Audiobookshelf). date_finished may be YYYY-MM-DD or
        an ISO-8601 instant; a bare date is expanded to a full instant before POST.
        """
        date_finished = self._to_instant(date_finished)
        payload = {"bookId": book_id, "dateFinished": date_finished}
        response = self._make_request("POST", "/api/v1/books/progress", payload)
        if response and response.status_code in [200, 201, 204]:
            logger.info(f"Grimmory: Set finish date {date_finished} for book {book_id}")
            return True
        resp_status = response.status_code if response else "No response"
        logger.warning(f"Grimmory: Failed to set finish date for book {book_id}: {resp_status}")
        return False

    def add_reading_session(
        self,
        book_id,
        book_type,
        *,
        start_time,
        end_time,
        duration_seconds,
        start_progress=0.0,
        end_progress=1.0,
    ):
        """Record one reading session. Grimmory always inserts, so call once per
        source session to replay full history.

        start_time/end_time are ISO-8601 instants. Required by Grimmory:
        bookId, startTime, endTime, durationSeconds.
        """
        payload = {
            "bookId": book_id,
            "startTime": start_time,
            "endTime": end_time,
            "durationSeconds": int(duration_seconds),
            "startProgress": start_progress,
            "endProgress": end_progress,
        }
        if book_type:
            payload["bookType"] = book_type
        response = self._make_request("POST", "/api/v1/reading-sessions", payload)
        if response and response.status_code in [200, 201, 202, 204]:
            return True
        resp_status = response.status_code if response else "No response"
        logger.warning(f"Grimmory: Failed to record reading session for book {book_id}: {resp_status}")
        return False

    def add_bookmark(self, book_id, *, position_ms, track_index=0, title=None):
        """Create an audiobook bookmark anchored by position (ms) + track index."""
        payload = {"bookId": book_id, "positionMs": int(position_ms), "trackIndex": int(track_index)}
        if title:
            payload["title"] = title
        response = self._make_request("POST", "/api/v1/bookmarks", payload)
        if response and response.status_code in [200, 201, 204]:
            return True
        resp_status = response.status_code if response else "No response"
        logger.warning(f"Grimmory: Failed to create bookmark for book {book_id}: {resp_status}")
        return False

    @staticmethod
    def _norm_isbn(v):
        return re.sub(r"[^0-9Xx]", "", str(v)).upper() if v else ""

    @staticmethod
    def _fuzzy_threshold():
        try:
            raw_threshold = float(os.environ.get("FUZZY_MATCH_THRESHOLD", 80))
        except (TypeError, ValueError):
            raw_threshold = 80.0
        return raw_threshold / 100 if raw_threshold > 1 else raw_threshold

    def _isbn_matches(self, books, isbn):
        target = self._norm_isbn(isbn)
        if not target:
            return []
        matches = []
        for b in books:
            for key in ("isbn13", "isbn10", "isbn"):
                if self._norm_isbn(b.get(key)) == target:
                    matches.append(b)
                    break
        return matches

    def _asin_matches(self, books, asin):
        target = str(asin).strip().lower()
        if not target:
            return []
        return [b for b in books if str(b.get("asin") or "").strip().lower() == target]

    def _title_scored_candidates(self, books, title, author):
        """Score ``books`` by fuzzy title (and title+author) match.

        Returns ``(title_author_candidates, title_candidates)`` where each is a
        list of ``(book, ratio)`` for books at-or-above ``FUZZY_MATCH_THRESHOLD``,
        sorted by ratio descending. ``title_author_candidates`` is the subset whose
        author also overlaps ``author``.
        """
        threshold = self._fuzzy_threshold()
        title_norm = self._normalize_string(title)
        author_norm = self._normalize_string(author) if author else ""

        title_scored = []
        title_author_scored = []
        for b in books:
            b_title_norm = self._normalize_string(b.get("title"))
            if not b_title_norm:
                continue
            t_ratio = SequenceMatcher(None, title_norm, b_title_norm).ratio()
            if t_ratio < threshold:
                continue
            title_scored.append((b, t_ratio))
            if author_norm:
                raw_authors = b.get("authors")
                b_author_norm = self._normalize_string(
                    raw_authors if isinstance(raw_authors, str) else ""
                )
                if b_author_norm and (
                    author_norm in b_author_norm or b_author_norm in author_norm
                ):
                    title_author_scored.append((b, t_ratio))

        title_scored.sort(key=lambda x: x[1], reverse=True)
        title_author_scored.sort(key=lambda x: x[1], reverse=True)
        return title_author_scored, title_scored

    def match_book_by_identifiers(
        self, *, isbn=None, asin=None, title=None, author=None, prefer_book_type=None, exclude_book_id=None
    ):
        """Find a Grimmory book by identifier cascade: isbn -> asin -> title+author -> title.

        Matches client-side against the local book list (Grimmory has no
        search-by-identifier endpoint). Returns (book_info, matched_by) or (None, None).

        ``prefer_book_type`` (e.g. "AUDIOBOOK") disambiguates same-identifier
        multi-format records: within each tier, when several books match equally,
        the one whose ``bookType`` matches is returned instead of the first hit.
        ``None`` (the default) preserves neutral first-hit behavior for every
        other caller.

        ``exclude_book_id`` skips any candidate whose ``id`` equals it in every
        tier, used to find a title's *other* format record (e.g. the ebook
        counterpart of an already-matched audiobook).
        """
        books = self.get_all_books()
        if not books:
            return None, None

        def _excluded(b):
            return exclude_book_id is not None and str(b.get("id")) == str(exclude_book_id)

        def _pick(candidates):
            if prefer_book_type:
                for b in candidates:
                    if (b.get("bookType") or "").upper() == prefer_book_type.upper():
                        return b
            return candidates[0]

        def _pick_scored(scored):
            """Select from ratio-scored ``(book, ratio)`` candidates.

            With ``prefer_book_type`` set, a record of that type can win even at a
            slightly lower ratio than a non-preferred record: real dual-format
            data gives the preferred format a differently-prefixed title (e.g. the
            audiobook "02 Paladin's Strength" at 0.94 vs the ebook "Paladin's
            Strength" at 1.0). Pick the highest-ratio preferred-type record when
            any exists; otherwise fall back to the top-ratio tie group's first hit.
            """
            if prefer_book_type:
                preferred = [
                    (b, r)
                    for b, r in scored
                    if (b.get("bookType") or "").upper() == prefer_book_type.upper()
                ]
                if preferred:
                    return preferred[0][0]
            best_ratio = scored[0][1]
            return _pick([b for b, r in scored if r == best_ratio])

        def _has_preferred(books, scored=False):
            if not prefer_book_type:
                return False
            records = (b for b, _r in books) if scored else books
            return any(
                (b.get("bookType") or "").upper() == prefer_book_type.upper()
                for b in records
            )

        considered = [b for b in books if not _excluded(b)]

        # Build the cascade lazily as ``(matched_by, resolver, has_preferred)`` per
        # non-empty tier: isbn -> asin -> title+author -> title.
        tiers = []
        if isbn:
            matches = self._isbn_matches(considered, isbn)
            if matches:
                tiers.append(("isbn", lambda m=matches: _pick(m), _has_preferred(matches)))
        if asin:
            matches = self._asin_matches(considered, asin)
            if matches:
                tiers.append(("asin", lambda m=matches: _pick(m), _has_preferred(matches)))
        if title:
            title_author_scored, title_scored = self._title_scored_candidates(
                considered, title, author
            )
            if title_author_scored:
                tiers.append((
                    "title_author",
                    lambda s=title_author_scored: _pick_scored(s),
                    _has_preferred(title_author_scored, scored=True),
                ))
            if title_scored:
                tiers.append((
                    "title",
                    lambda s=title_scored: _pick_scored(s),
                    _has_preferred(title_scored, scored=True),
                ))

        if not tiers:
            return None, None

        # With a preference, a correct-format match from a weaker tier outranks a
        # wrong-format match from a stronger one: return the strongest tier that
        # actually contains a ``prefer_book_type`` record. ``_pick``/``_pick_scored``
        # then select that record within the tier.
        if prefer_book_type:
            for matched_by, resolve, has_preferred in tiers:
                if has_preferred:
                    return resolve(), matched_by
                if matched_by in ("isbn", "asin"):
                    break

        # No preference, or no tier held a preferred-type record: keep the neutral
        # first-non-empty-tier behavior (preserves the ebook fallback when no
        # audiobook exists anywhere).
        matched_by, resolve, _ = tiers[0]
        return resolve(), matched_by

    def find_format_counterpart(
        self, *, matched_book, isbn=None, asin=None, title=None, author=None, exclude_book_type="AUDIOBOOK"
    ):
        """Find the best non-``exclude_book_type`` record for an already-matched book.

        Given the audiobook record ``match_book_by_identifiers`` resolved, locate
        the matching *other* format (the ebook). Unlike re-running the generic
        cascade with ``exclude_book_id``, this:

        - excludes records whose ``bookType`` equals ``exclude_book_type`` outright,
          so a lower-ratio audiobook title never wins, and
        - ranks *all* title candidates at-or-above threshold (not only the single
          best-ratio tie group), so a counterpart whose title differs slightly from
          the audiobook still surfaces.

        Returns ``(book, matched_by)`` for the strongest surviving candidate, else
        ``(None, None)``.
        """
        if not matched_book:
            return None, None
        books = self.get_all_books()
        if not books:
            return None, None

        matched_id = str(matched_book.get("id"))
        matched_instance = str(matched_book.get("_instance_id", getattr(self, "instance_id", "default")))
        exclude_type = (exclude_book_type or "").upper()
        candidates = [
            b
            for b in books
            if (
                (
                    str(b.get("id")) != matched_id
                    or str(b.get("_instance_id", getattr(self, "instance_id", "default"))) != matched_instance
                )
                and (b.get("bookType") or "").upper() != exclude_type
            )
        ]
        if not candidates:
            return None, None

        # ISBN / ASIN tiers: strongest, return the first surviving match.
        if isbn:
            matches = self._isbn_matches(candidates, isbn)
            if matches:
                return matches[0], "isbn"
        if asin:
            matches = self._asin_matches(candidates, asin)
            if matches:
                return matches[0], "asin"

        if title:
            title_author_scored, title_scored = self._title_scored_candidates(
                candidates, title, author
            )
            if title_author_scored:
                return title_author_scored[0][0], "title_author"
            if title_scored:
                return title_scored[0][0], "title"

        return None, None

    def get_recent_activity(self, min_progress=0.01):
        if not self._book_cache:
            self._refresh_book_cache()
        results = []
        for _filename, book in list(self._book_cache.items()):
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

    def download_book(self, book_id):
        """Download from whichever client owns the book.

        book_id may be a plain int/str (legacy, tries all clients) or
        qualified as 'instance_id:book_id'.
        """
        bid_str = str(book_id)
        if ":" in bid_str:
            target_instance, raw_id = bid_str.split(":", 1)
            for c in self._active:
                if c.instance_id == target_instance:
                    return c.download_book(raw_id)
            return None

        for c in self._active:
            result = c.download_book(book_id)
            if result:
                return result
        return None

    @property
    def base_url(self):
        for c in self._active:
            return c.base_url
        return None

    def remove_from_shelf(self, ebook_filename, shelf_name=None):
        for c in self._active:
            if c.remove_from_shelf(ebook_filename, shelf_name):
                return True
        return False

    def add_to_shelf(self, ebook_filename, shelf_name=None):
        for c in self._active:
            if c.add_to_shelf(ebook_filename, shelf_name):
                return True
        return False

    def get_progress(self, ebook_filename):
        for c in self._active:
            pct, cfi = c.get_progress(ebook_filename)
            if pct is not None:
                return pct, cfi
        return None, None

    # ── ABS -> Grimmory migration passthroughs ──

    _MATCH_STRENGTH = {"isbn": 3, "asin": 2, "title_author": 1, "title": 0}

    def match_book_by_identifiers(
        self, *, isbn=None, asin=None, title=None, author=None, prefer_book_type=None, exclude_book_id=None
    ):
        """Match across all instances, returning the strongest match.

        Each client falls back isbn -> asin -> title+author -> title, so a weak
        title-only hit on an early instance must not shadow a stronger ISBN/ASIN
        match on a later one. Collect every instance's match and keep the best by
        match-type strength. Returns (book_info_with_instance_id, matched_by).

        ``prefer_book_type`` / ``exclude_book_id`` pass through to each member.
        When ``prefer_book_type`` is set it only breaks ties within the same match
        strength, so exact ISBN/ASIN matches still outrank fuzzy title matches
        across instances.
        """
        best_book = None
        best_matched_by = None
        best_key = (-1, -1)
        for c in self._active:
            book, matched_by = c.match_book_by_identifiers(
                isbn=isbn,
                asin=asin,
                title=title,
                author=author,
                prefer_book_type=prefer_book_type,
                exclude_book_id=exclude_book_id,
            )
            if not book:
                continue
            strength = self._MATCH_STRENGTH.get(matched_by, -1)
            is_preferred = bool(prefer_book_type) and (book.get("bookType") or "").upper() == prefer_book_type.upper()
            key = (strength, 1 if is_preferred else 0)
            if key > best_key:
                best_key = key
                best_book = {**book, "_instance_id": c.instance_id}
                best_matched_by = matched_by
                if strength == self._MATCH_STRENGTH["isbn"] and (not prefer_book_type or is_preferred):
                    break
        if best_book:
            return best_book, best_matched_by
        return None, None

    def find_format_counterpart(
        self, *, matched_book, isbn=None, asin=None, title=None, author=None, exclude_book_type="AUDIOBOOK"
    ):
        """Find the best non-``exclude_book_type`` record across all instances.

        Asks each member for its counterpart, tags the result with ``_instance_id``,
        and keeps the strongest by ``_MATCH_STRENGTH`` -- mirroring the
        cross-instance reconciliation in ``match_book_by_identifiers``. Returns
        ``(book_info_with_instance_id, matched_by)`` or ``(None, None)``.
        """
        best_book = None
        best_matched_by = None
        best_strength = -1
        for c in self._active:
            book, matched_by = c.find_format_counterpart(
                matched_book=matched_book,
                isbn=isbn,
                asin=asin,
                title=title,
                author=author,
                exclude_book_type=exclude_book_type,
            )
            if not book:
                continue
            strength = self._MATCH_STRENGTH.get(matched_by, -1)
            if strength > best_strength:
                best_strength = strength
                best_book = {**book, "_instance_id": c.instance_id}
                best_matched_by = matched_by
                if strength == self._MATCH_STRENGTH["isbn"]:
                    break
        if best_book:
            return best_book, best_matched_by
        return None, None

    def _client_for_instance(self, instance_id):
        for c in self._active:
            if c.instance_id == instance_id:
                return c
        logger.warning(
            "No active Grimmory instance %r; skipping write to avoid wrong-server routing",
            instance_id,
        )
        return None

    def update_read_status_by_id(self, book_id, status, instance_id="default"):
        c = self._client_for_instance(instance_id)
        return c.update_read_status_by_id(book_id, status) if c else False

    def set_finished_date(self, book_id, book_type, date_finished, instance_id="default"):
        c = self._client_for_instance(instance_id)
        return c.set_finished_date(book_id, book_type, date_finished) if c else False

    def add_reading_session(self, book_id, book_type, *, instance_id="default", **kwargs):
        c = self._client_for_instance(instance_id)
        return c.add_reading_session(book_id, book_type, **kwargs) if c else False

    def add_bookmark(self, book_id, *, instance_id="default", **kwargs):
        c = self._client_for_instance(instance_id)
        return c.add_bookmark(book_id, **kwargs) if c else False
