"""Kobo integration service: ingest KoboReader.sqlite copies and match device
books to the library.

Data arrives as database copies in a watched directory (KOBO_DB_DIR) or as
uploads (data_dir/kobo). Newest file wins for progress; bookmarks merge across
all copies so highlights survive book deletions on the device (kobuddy's
multi-database trick).
"""

import logging
import os
from pathlib import Path

from src.utils.kobo_reader import iter_bookmarks, iter_books
from src.utils.title_utils import normalize_title

logger = logging.getLogger(__name__)

UPLOAD_DIR_NAME = "kobo"


class KoboService:
    def __init__(self, database_service, data_dir: Path | None = None):
        self.database_service = database_service
        self.data_dir = data_dir
        self._last_signatures: dict[str, tuple[int, int]] = {}

    @property
    def upload_dir(self) -> Path | None:
        if self.data_dir is None:
            return None
        return Path(self.data_dir) / UPLOAD_DIR_NAME

    def is_configured(self) -> bool:
        enabled_val = os.environ.get("KOBO_ENABLED", "").lower()
        if enabled_val == "false":
            return False
        return bool(self.database_copies())

    def database_copies(self) -> list[Path]:
        """All Kobo sqlite copies we know about (watched dir + uploads)."""
        files: list[Path] = []
        watched = os.environ.get("KOBO_DB_DIR", "").strip()
        for d in filter(None, [watched, str(self.upload_dir) if self.upload_dir else ""]):
            path = Path(d)
            if path.is_dir():
                files.extend(path.glob("*.sqlite"))
        return sorted(set(files))

    @staticmethod
    def _signature(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return (stat.st_size, stat.st_mtime_ns)

    def refresh_if_changed(self) -> bool:
        """Re-parse database copies whose file signatures changed."""
        files = self.database_copies()
        changed = []
        current: dict[str, tuple[int, int]] = {}
        for f in files:
            key = str(f)
            try:
                sig = self._signature(f)
            except OSError:
                continue
            current[key] = sig
            if self._last_signatures.get(key) != sig:
                changed.append(f)
        if not changed:
            self._last_signatures = current
            return False

        self._last_signatures = current
        newest = max(files, key=lambda f: f.stat().st_mtime_ns)
        logger.info("Kobo: %d database copy/copies changed, ingesting from %s", len(changed), newest.name)
        self._ingest_books(newest)
        self._ingest_bookmarks(files)
        self._auto_match_unmatched()
        return True

    def _ingest_books(self, db_file: Path) -> None:
        books = []
        try:
            for snap in iter_books(db_file):
                books.append(
                    {
                        "content_id": snap.content_id,
                        "title": snap.title,
                        "author": snap.author,
                        "isbn": snap.isbn,
                        "percent": snap.percent,
                        "read_status": snap.read_status,
                        "date_last_read": snap.date_last_read,
                        "time_spent_seconds": snap.time_spent_seconds,
                    }
                )
        except Exception as e:
            logger.warning("Kobo: failed reading books from %s: %s", db_file.name, e)
            return
        if books:
            result = self.database_service.save_kobo_books(books)
            logger.info("Kobo: ingested %d books (%d new, %d updated)", len(books), result["saved"], result["updated"])

    def _ingest_bookmarks(self, db_files: list[Path]) -> None:
        bookmarks = []
        seen = set()
        for db_file in db_files:
            try:
                for snap in iter_bookmarks(db_file):
                    if snap.bookmark_id in seen:
                        continue
                    seen.add(snap.bookmark_id)
                    bookmarks.append(
                        {
                            "bookmark_id": snap.bookmark_id,
                            "content_id": snap.volume_id,
                            "kind": snap.kind,
                            "text": snap.text,
                            "annotation": snap.annotation,
                            "chapter_progress": snap.chapter_progress,
                            "highlighted_at": snap.created,
                        }
                    )
            except Exception as e:
                logger.debug("Kobo: failed reading bookmarks from %s: %s", db_file.name, e)
        if bookmarks:
            result = self.database_service.save_kobo_bookmarks(bookmarks)
            if result["saved"]:
                logger.info("Kobo: ingested %d new bookmarks/highlights", result["saved"])

    def _auto_match_unmatched(self) -> None:
        """Link unmatched Kobo books to library books by normalized title."""
        unmatched = [b for b in self.database_service.get_kobo_books() if not b.matched_book_id]
        if not unmatched:
            return
        library = self.database_service.get_all_books()
        exact = {}
        for book in library:
            for title in filter(None, {book.title, getattr(book, "title_override", None)}):
                exact.setdefault(normalize_title(title), []).append(book)

        for kobo_book in unmatched:
            target = normalize_title(kobo_book.title or "")
            matches = exact.get(target, [])
            if len(matches) == 1:
                self.link_book(kobo_book.content_id, matches[0].id)
                logger.info("Kobo: auto-matched '%s' to book %d", kobo_book.title, matches[0].id)

    def link_book(self, content_id: str, book_id: int) -> None:
        self.database_service.set_kobo_book_match(content_id, book_id)
        self.database_service.link_kobo_bookmarks_by_content_id(content_id, book_id)

    def unlink_book(self, content_id: str) -> None:
        self.database_service.unlink_kobo_book(content_id)

    def kobo_books_for(self, book_id: int) -> list:
        self.refresh_if_changed()
        return [b for b in self.database_service.get_kobo_books_by_matched_book_id(book_id) if not b.hidden]

    def kobo_books_by_library_id(self) -> dict[int, list]:
        """Matched, non-hidden device books keyed by library book id."""
        self.refresh_if_changed()
        by_id: dict[int, list] = {}
        for kobo_book in self.database_service.get_kobo_books():
            if kobo_book.matched_book_id is not None:
                by_id.setdefault(kobo_book.matched_book_id, []).append(kobo_book)
        return by_id

    def progress_for_book(self, book_id: int) -> float | None:
        """Best device progress (0-1) for a library book, or None if the book
        isn't on any ingested Kobo database."""
        kobo_books = self.kobo_books_for(book_id)
        if not kobo_books:
            return None
        best = max(kobo_books, key=lambda b: (b.percent, b.read_status))
        if best.read_status == 2:
            return 1.0
        return best.percent / 100.0

    @staticmethod
    def _journal_quote_key(text: str) -> str:
        """Dedupe key: the normalized first line of a journal entry.

        Works for Kobo entries (quote on line 1) and BookFusion entries
        (quote before the chapter citation marker) alike, so the same passage
        imported from both sources is imported once.
        """
        if not text:
            return ""
        first_line = text.split("\n", 1)[0].strip()
        return " ".join(first_line.lower().split())

    def save_bookmarks_to_journal(self, book_id: int) -> dict:
        """Import a matched Kobo book's highlights/notes as journal entries.

        Idempotent: passages already present as journal highlights (from Kobo
        or BookFusion) are skipped by normalized quote.
        """
        book = self.database_service.get_book_by_id(book_id)
        if not book:
            return {"saved": 0, "skipped": 0, "error": "book not found"}

        bookmarks = [
            b for b in self.database_service.get_kobo_bookmarks_for_book_by_book_id(book.id) if b.text
        ]
        existing_entries = self.database_service.get_reading_journal_entries_for_book(book.id, "highlight")
        existing_keys = {self._journal_quote_key(e.entry) for e in existing_entries if e.entry}

        saved = 0
        skipped = 0
        for bm in bookmarks:
            quote = (bm.text or "").strip()
            if not quote:
                continue
            key = self._journal_quote_key(quote)
            if key in existing_keys:
                skipped += 1
                continue
            entry = quote
            note = (bm.annotation or "").strip()
            if note:
                entry += f"\n> {note}"
            self.database_service.add_reading_journal(
                book.id, "highlight", entry=entry, created_at=bm.highlighted_at, abs_id=book.abs_id
            )
            existing_keys.add(key)
            saved += 1
        if saved:
            logger.info("Kobo: imported %d journal highlights for book %d", saved, book.id)
        return {"saved": saved, "skipped": skipped}
