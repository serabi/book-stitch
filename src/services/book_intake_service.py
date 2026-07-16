"""Book intake orchestration for matching and import flows."""

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.db.models import Book, StorytellerSubmission
from src.services.kosync_service import ensure_kosync_document
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntakeResult:
    book: Book | None = None
    error: str | None = None
    status_code: int = 400


class BookIntakeService:
    """Deep Module for creating and joining PageKeeper books from user intent.

    The Interface is intentionally shaped around the match UI's intents, while
    the implementation keeps the cross-service side effects local.
    """

    def __init__(
        self,
        *,
        container,
        database_service,
        abs_service,
        collection_name: str,
        books_dir: str,
        epub_cache_dir: str,
        find_in_grimmory: Callable,
        get_kosync_id_for_ebook: Callable,
        attempt_hardcover_automatch: Callable,
    ):
        self.container = container
        self.database_service = database_service
        self.abs_service = abs_service
        self.collection_name = collection_name
        self.books_dir = books_dir
        self.epub_cache_dir = epub_cache_dir
        self.find_in_grimmory = find_in_grimmory
        self.get_kosync_id_for_ebook = get_kosync_id_for_ebook
        self.attempt_hardcover_automatch = attempt_hardcover_automatch

    def import_audio_only(self, *, abs_id, title, duration, author=None, subtitle=None) -> Book:
        book = Book(
            abs_id=abs_id,
            title=title,
            ebook_filename=None,
            kosync_doc_id=None,
            status="not_started",
            duration=duration,
            sync_mode="audiobook",
            author=author,
            subtitle=subtitle,
        )
        self.database_service.save_book(book, is_new=True)
        self.abs_service.add_to_collection(abs_id, self.collection_name)
        self.attempt_hardcover_automatch(self.container, book)
        self.database_service.resolve_suggestion(abs_id)
        self.database_service.resolve_detected_book(abs_id, source="abs")
        return book

    def import_ebook_only(
        self,
        *,
        ebook_filename=None,
        ebook_source_id=None,
        ebook_display_name="",
        storyteller_uuid=None,
        storyteller_title="",
    ) -> IntakeResult:
        if not ebook_filename and not storyteller_uuid:
            return IntakeResult(error="An ebook or Storyteller selection is required", status_code=400)

        kosync_doc_id = None
        if ebook_filename:
            bl_book, bl_client = self._find_grimmory_book(ebook_filename, ebook_source_id)
            if ebook_source_id and not bl_book:
                return IntakeResult(error="Selected Grimmory book was not found", status_code=404)
            grimmory_id = bl_book.get("id") if bl_book else None
            kosync_doc_id = self.get_kosync_id_for_ebook(ebook_filename, grimmory_id, bl_client=bl_client)
            if not kosync_doc_id:
                return IntakeResult(error="Could not compute KOSync ID for ebook", status_code=404)
            title = ebook_display_name or (bl_book.get("title") if bl_book else None) or Path(ebook_filename).stem
        else:
            title = storyteller_title or ebook_display_name or "Storyteller Book"
            ebook_filename = None

        book = Book(
            abs_id=None,
            title=title,
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            status="not_started",
            sync_mode="ebook_only",
            storyteller_uuid=storyteller_uuid,
        )
        self.database_service.save_book(book, is_new=True)
        ensure_kosync_document(book, self.database_service)
        self._record_grimmory_source(kosync_doc_id, ebook_source_id)
        if kosync_doc_id:
            self.database_service.resolve_suggestion(kosync_doc_id, source="kosync")
            self.database_service.resolve_detected_book(kosync_doc_id, source="kosync")
        if storyteller_uuid:
            self.database_service.resolve_suggestion(storyteller_uuid, source="storyteller")
            self.database_service.resolve_detected_book(storyteller_uuid, source="storyteller")
        if ebook_filename:
            self._resolve_grimmory_detection(ebook_filename, ebook_source_id)
        return IntakeResult(book=book)

    def attach_ebook(self, *, abs_id, ebook_filename, ebook_source_id=None) -> IntakeResult:
        if not abs_id or not ebook_filename:
            return IntakeResult(error="Missing book ID or ebook filename", status_code=400)

        book = self.database_service.get_book_by_ref(abs_id)
        if not book:
            return IntakeResult(error="Book not found", status_code=404)

        bl_book, bl_client = self._find_grimmory_book(ebook_filename, ebook_source_id)
        if ebook_source_id and not bl_book:
            return IntakeResult(error="Selected Grimmory book was not found", status_code=404)
        grimmory_id = bl_book.get("id") if bl_book else None
        kosync_doc_id = self.get_kosync_id_for_ebook(ebook_filename, grimmory_id, bl_client=bl_client)
        if not kosync_doc_id:
            return IntakeResult(error="Could not compute KOSync ID for ebook", status_code=404)

        book.ebook_filename = ebook_filename
        book.kosync_doc_id = kosync_doc_id
        book.status = "pending"
        self.database_service.save_book(book)
        ensure_kosync_document(book, self.database_service)
        self._record_grimmory_source(kosync_doc_id, ebook_source_id)
        self._add_to_grimmory_shelf(bl_client, ebook_filename)
        self.database_service.resolve_suggestion(kosync_doc_id)
        self.database_service.resolve_detected_book(kosync_doc_id, source="kosync")
        self._resolve_grimmory_detection(ebook_filename, ebook_source_id)
        return IntakeResult(book=book)

    def attach_audiobook(self, *, source_book_id, abs_id, title, duration, author=None, subtitle=None) -> IntakeResult:
        if not source_book_id or not abs_id:
            return IntakeResult(error="Missing book ID or audiobook ID", status_code=400)

        book = self.database_service.get_book_by_ref(source_book_id)
        if not book:
            return IntakeResult(error="Book not found", status_code=404)

        new_book = Book(
            abs_id=abs_id,
            title=title,
            ebook_filename=book.ebook_filename,
            kosync_doc_id=book.kosync_doc_id,
            status=book.status or "not_started",
            duration=duration,
            sync_mode="audiobook",
            author=author,
            subtitle=subtitle,
            **self._copy_book_merge_metadata(
                book,
                {
                    "storyteller_uuid": book.storyteller_uuid,
                    "original_ebook_filename": book.original_ebook_filename,
                },
            ),
        )
        self.database_service.save_book(new_book)
        ensure_kosync_document(new_book, self.database_service)
        self._migrate_source_identity(book.abs_id or source_book_id, abs_id)
        self.abs_service.add_to_collection(abs_id, self.collection_name)
        self.attempt_hardcover_automatch(self.container, new_book)
        self.database_service.resolve_suggestion(abs_id)
        self.database_service.resolve_detected_book(abs_id, source="abs")
        if new_book.kosync_doc_id:
            self.database_service.resolve_suggestion(new_book.kosync_doc_id)
            self.database_service.resolve_detected_book(new_book.kosync_doc_id, source="kosync")
        return IntakeResult(book=new_book)

    def map_audiobook_ebook(
        self,
        *,
        abs_id,
        title,
        ebook_filename,
        duration,
        ebook_source_id=None,
        storyteller_uuid=None,
        storyteller_submit=False,
        author=None,
        subtitle=None,
        detected_source=None,
        detected_source_id=None,
        expected_ebook_kosync_id=None,
    ) -> IntakeResult:
        bl_match, bl_match_client = self._find_grimmory_book(ebook_filename, ebook_source_id)
        if ebook_source_id and not bl_match:
            return IntakeResult(error="Selected Grimmory book was not found", status_code=404)
        grimmory_id = bl_match.get("id") if bl_match else None

        kosync_doc_id = self.get_kosync_id_for_ebook(ebook_filename, grimmory_id, bl_client=bl_match_client)
        if not kosync_doc_id:
            logger.warning("Cannot compute KOSync ID for '%s'", sanitize_log_data(ebook_filename))
            return IntakeResult(error="Could not compute KOSync ID for ebook", status_code=404)

        if expected_ebook_kosync_id and kosync_doc_id != expected_ebook_kosync_id:
            return IntakeResult(error="The selected ebook no longer matches the detected edition", status_code=409)

        current_book_entry = self.database_service.get_book_by_ref(abs_id)
        if self._is_same_mapping(current_book_entry, ebook_filename, ebook_source_id, kosync_doc_id):
            self._resolve_exact_detection(detected_source, detected_source_id)
            return IntakeResult(book=current_book_entry)

        if current_book_entry and current_book_entry.kosync_doc_id:
            logger.info("Preserving existing hash '%s' for '%s'", current_book_entry.kosync_doc_id, abs_id)
            kosync_doc_id = current_book_entry.kosync_doc_id

        existing_book = self.database_service.get_book_by_kosync_id(kosync_doc_id)
        migration_source_id = None
        original_ebook_filename = None

        if existing_book and existing_book.abs_id != abs_id:
            logger.info("Merging existing '%s' into '%s'", existing_book.abs_id, abs_id)
            migration_source_id = existing_book.abs_id or existing_book.id
            ebook_item_id = existing_book.ebook_item_id or existing_book.abs_ebook_item_id or existing_book.abs_id
            original_ebook_filename = existing_book.original_ebook_filename or existing_book.ebook_filename
            merge_metadata = self._copy_book_merge_metadata(
                existing_book,
                {
                    "abs_ebook_item_id": ebook_item_id,
                    "ebook_item_id": ebook_item_id,
                    "original_ebook_filename": original_ebook_filename,
                    "storyteller_uuid": storyteller_uuid or existing_book.storyteller_uuid,
                },
            )
        else:
            merge_metadata = {
                "storyteller_uuid": storyteller_uuid,
                "original_ebook_filename": None,
                "abs_ebook_item_id": None,
                "ebook_item_id": None,
            }

        book = Book(
            abs_id=abs_id,
            title=title,
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=None,
            status="pending",
            duration=duration,
            author=author,
            subtitle=subtitle,
            **merge_metadata,
        )
        self.database_service.save_book(book, is_new=True)
        ensure_kosync_document(book, self.database_service)
        self._record_grimmory_source(kosync_doc_id, ebook_source_id)

        if storyteller_submit:
            self._create_storyteller_reservation(abs_id)

        if migration_source_id:
            self._migrate_source_identity(migration_source_id, abs_id)
            self.abs_service.add_to_collection(abs_id, self.collection_name)
        else:
            self.abs_service.add_to_collection(abs_id, self.collection_name)

        self.attempt_hardcover_automatch(self.container, book)

        if bl_match_client:
            self._add_to_grimmory_shelf(bl_match_client, original_ebook_filename or ebook_filename)

        if storyteller_submit:
            self._submit_to_storyteller_async(abs_id, title, ebook_filename)

        self._resolve_mapping_suggestions(abs_id, kosync_doc_id, ebook_filename, ebook_source_id)
        self._resolve_exact_detection(detected_source, detected_source_id)
        return IntakeResult(book=book)

    def _is_same_mapping(self, book, ebook_filename, ebook_source_id, kosync_doc_id):
        if not book or book.ebook_filename != ebook_filename or book.kosync_doc_id != kosync_doc_id:
            return False
        if not ebook_source_id:
            return True
        doc = self.database_service.get_kosync_document(kosync_doc_id)
        return bool(doc and doc.grimmory_id == ebook_source_id)

    def _resolve_exact_detection(self, source, source_id):
        if source and source_id:
            self.database_service.resolve_detected_book(source_id, source=source)

    def _create_storyteller_reservation(self, abs_id):
        book = self.database_service.get_book_by_ref(abs_id)
        if not book:
            logger.warning("Cannot create Storyteller reservation: book not found for abs_id=%s", abs_id)
            return None
        storyteller_uuid = book.storyteller_uuid
        submission = StorytellerSubmission(
            abs_id=abs_id,
            book_id=book.id,
            status="queued",
            storyteller_uuid=storyteller_uuid,
        )
        self.database_service.save_storyteller_submission(submission)
        return submission

    def _submit_to_storyteller_async(self, abs_id, book_title, ebook_filename):
        def _do_submit():
            submission_succeeded = False
            try:
                st_sub_svc = self.container.storyteller_submission_service()
                if not st_sub_svc.is_available():
                    logger.warning("Storyteller submission skipped for '%s': service not available", book_title)
                    return

                from src.utils.epub_resolver import get_local_epub

                epub_path = get_local_epub(
                    ebook_filename,
                    self.books_dir,
                    self.epub_cache_dir,
                    self.container.grimmory_client(),
                )
                audio_files = self.container.abs_client().get_audio_files(abs_id)
                if epub_path and audio_files:
                    result = st_sub_svc.submit_book(abs_id, book_title, Path(epub_path), audio_files)
                    submission_succeeded = result.success
                    if not submission_succeeded:
                        logger.warning("Storyteller submission failed for '%s': %s", book_title, result.error)
                else:
                    logger.warning(
                        "Storyteller submission skipped for '%s': epub=%s, audio=%s files",
                        book_title,
                        "found" if epub_path else "missing",
                        len(audio_files or []),
                    )
            except Exception as e:
                logger.warning("Storyteller submission error for '%s': %s", book_title, e)
            finally:
                if not submission_succeeded:
                    self._mark_storyteller_submission_failed(abs_id)

        threading.Thread(target=_do_submit, daemon=True).start()

    def _mark_storyteller_submission_failed(self, abs_id):
        try:
            book = self.database_service.get_book_by_abs_id(abs_id)
            submission = self.database_service.get_active_storyteller_submission_by_book_id(book.id) if book else None
            if submission:
                self.database_service.update_storyteller_submission_status(submission.id, "failed")
        except Exception as e:
            logger.debug("Failed to mark Storyteller submission as failed: %s", e)

    def _migrate_source_identity(self, source_id, target_abs_id):
        try:
            self.database_service.migrate_book_data(source_id, target_abs_id)
            logger.info("Successfully merged %s into %s", source_id, target_abs_id)
        except Exception as e:
            logger.error("Failed to merge book data: %s", e)
            raise

    def _add_to_grimmory_shelf(self, bl_client, ebook_filename):
        if not bl_client:
            return
        try:
            bl_client.add_to_shelf(ebook_filename)
        except Exception as e:
            logger.warning("Grimmory add_to_shelf failed for '%s': %s", sanitize_log_data(ebook_filename), e)

    def _resolve_mapping_suggestions(self, abs_id, kosync_doc_id, ebook_filename, ebook_source_id=None):
        self.database_service.resolve_suggestion(abs_id)
        self.database_service.resolve_suggestion(kosync_doc_id)
        self.database_service.resolve_detected_book(abs_id, source="abs")
        if kosync_doc_id:
            self.database_service.resolve_detected_book(kosync_doc_id, source="kosync")
        self._resolve_grimmory_detection(ebook_filename, ebook_source_id)
        try:
            device_doc = self.database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                self.database_service.resolve_suggestion(device_doc.document_hash)
                self.database_service.resolve_detected_book(device_doc.document_hash, source="kosync")
        except Exception as e:
            logger.warning("Failed to check/resolve device hash: %s", e)

    def _find_grimmory_book(self, ebook_filename, ebook_source_id):
        if ebook_source_id:
            return self.find_in_grimmory(ebook_filename, ebook_source_id)
        return self.find_in_grimmory(ebook_filename)

    def _record_grimmory_source(self, kosync_doc_id, ebook_source_id):
        if not kosync_doc_id or not ebook_source_id:
            return
        doc = self.database_service.get_kosync_document(kosync_doc_id)
        if doc:
            doc.source = "grimmory"
            doc.grimmory_id = ebook_source_id
            self.database_service.save_kosync_document(doc)

    def _resolve_grimmory_detection(self, ebook_filename, ebook_source_id):
        if not ebook_filename:
            return
        if not ebook_source_id or ":" not in str(ebook_source_id):
            self.database_service.resolve_suggestion(ebook_filename, source="grimmory")
            return
        instance_id = str(ebook_source_id).split(":", 1)[0]
        detected_source_id = f"{instance_id}:{ebook_filename}"
        self.database_service.resolve_suggestion(detected_source_id, source="grimmory")
        self.database_service.resolve_detected_book(detected_source_id, source="grimmory")

    @staticmethod
    def _copy_book_merge_metadata(existing_book, overrides=None):
        metadata = {
            "storyteller_uuid": existing_book.storyteller_uuid,
            "original_ebook_filename": existing_book.original_ebook_filename,
            "abs_ebook_item_id": existing_book.abs_ebook_item_id,
            "ebook_item_id": existing_book.ebook_item_id or existing_book.abs_ebook_item_id,
            "custom_cover_url": existing_book.custom_cover_url,
            "started_at": existing_book.started_at,
            "finished_at": existing_book.finished_at,
            "rating": existing_book.rating,
            "read_count": existing_book.read_count or 1,
        }
        if overrides:
            metadata.update({key: value for key, value in overrides.items() if value is not None})
        return metadata
