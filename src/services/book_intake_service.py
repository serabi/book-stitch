"""Book intake orchestration for matching and import flows."""

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy.exc import IntegrityError

from src.db.book_repository import KoSyncOwnershipConflict
from src.db.models import Book, StorytellerSubmission
from src.services.kosync_service import ensure_kosync_document
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntakeResult:
    book: Book | None = None
    error: str | None = None
    status_code: int = 400
    conflict_code: str | None = None
    conflict_book_id: int | None = None
    conflict_book_title: str | None = None


@dataclass(frozen=True)
class _PreparedMapping:
    abs_id: str
    ebook_filename: str
    ebook_source_id: str | None
    kosync_doc_id: str
    current_book: Book | None
    merge_book: Book | None
    grimmory_book: dict | None
    grimmory_client: object | None


class _MergeIdentityChanged(Exception):
    """The confirmed merge source changed before the transactional write."""


class _ClaimOwnershipLost(Exception):
    """The detected-book lease no longer belongs to this intake request."""


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
        confirm_combine=False,
        confirmed_merge_book_id=None,
    ) -> IntakeResult:
        prepared = self._prepare_mapping(abs_id, ebook_filename, ebook_source_id, expected_ebook_kosync_id)
        if isinstance(prepared, IntakeResult):
            return prepared

        processing_token = None
        if detected_source and detected_source_id:
            processing_token = self.database_service.claim_detected_book(
                detected_source_id, source=detected_source
            )
            if not processing_token:
                detected = self.database_service.get_detected_book(detected_source_id, source=detected_source)
                if getattr(detected, "status", None) == "resolved" and self._mapping_matches(prepared):
                    return IntakeResult(book=prepared.current_book)
                return IntakeResult(error="This pairing is already being processed or is no longer active", status_code=409)

        confirmed_merge_id = None
        try:
            confirmed_merge_id = int(confirmed_merge_book_id) if confirmed_merge_book_id is not None else None
        except (TypeError, ValueError):
            pass
        merge_unconfirmed = prepared.merge_book and (
            not confirm_combine or confirmed_merge_id != prepared.merge_book.id
        )
        if merge_unconfirmed:
            if processing_token:
                self.database_service.restore_detected_book(
                    detected_source_id, processing_token, source=detected_source
                )
            return self._combine_conflict(prepared.merge_book)

        def _renew_claim():
            return self.database_service.renew_detected_book_claim(
                detected_source_id,
                processing_token,
                source=detected_source,
            )

        def _complete_claim():
            return self.database_service.complete_detected_book(
                detected_source_id,
                processing_token,
                source=detected_source,
            )

        renew_claim = _renew_claim if processing_token else None
        complete_claim = _complete_claim if processing_token else None

        try:
            result = self._apply_mapping(
                prepared,
                title=title,
                duration=duration,
                storyteller_uuid=storyteller_uuid,
                storyteller_submit=storyteller_submit,
                author=author,
                subtitle=subtitle,
                resolve_detected=not processing_token,
                renew_claim=renew_claim,
                complete_claim=complete_claim,
            )
            return result
        except _MergeIdentityChanged:
            if processing_token:
                self.database_service.restore_detected_book(
                    detected_source_id, processing_token, source=detected_source
                )
            return IntakeResult(
                error="The existing book changed before it could be combined. Review the pairing again.",
                status_code=409,
                conflict_code="combine_changed",
                conflict_book_id=confirmed_merge_id,
            )
        except _ClaimOwnershipLost:
            if processing_token:
                self.database_service.restore_detected_book(
                    detected_source_id, processing_token, source=detected_source
                )
            return IntakeResult(
                error="This pairing lost its processing lease. Try again.",
                status_code=409,
                conflict_code="claim_lost",
            )
        except Exception:
            if processing_token:
                self.database_service.restore_detected_book(
                    detected_source_id, processing_token, source=detected_source
                )
            raise

    def link_grimmory_audiobook_ebook(
        self,
        *,
        audio_source_id,
        ebook_filename,
        ebook_source_id=None,
        expected_ebook_kosync_id=None,
        detected_source,
        detected_source_id,
    ) -> IntakeResult:
        """Link an exact Grimmory audiobook to an ebook without ABS side effects."""
        if not audio_source_id or not ebook_filename or not detected_source or not detected_source_id:
            return IntakeResult(error="An audiobook, ebook, and detected source are required", status_code=400)

        audio_group = self.container.grimmory_client_group()
        audio_info = audio_group.find_audiobook_by_source_id(audio_source_id)
        if not audio_info:
            return IntakeResult(error="The selected Grimmory audiobook is no longer available", status_code=409)

        prepared = self._prepare_mapping(None, ebook_filename, ebook_source_id, expected_ebook_kosync_id)
        if isinstance(prepared, IntakeResult):
            return prepared
        initial_kosync_id = prepared.kosync_doc_id
        target, conflict = self._grimmory_audio_target(audio_source_id, prepared.merge_book, prepared.kosync_doc_id)
        if conflict:
            return conflict

        processing_token = self.database_service.claim_detected_book(detected_source_id, source=detected_source)
        if not processing_token:
            detected = self.database_service.get_detected_book(detected_source_id, source=detected_source)
            if getattr(detected, "status", None) == "resolved" and target:
                return IntakeResult(book=target)
            return IntakeResult(error="This match is already being processed or is no longer active", status_code=409)

        def restore_claim():
            self.database_service.restore_detected_book(
                detected_source_id,
                processing_token,
                source=detected_source,
            )

        try:
            audio_info = audio_group.find_audiobook_by_source_id(audio_source_id)
            if not audio_info:
                restore_claim()
                return IntakeResult(error="The selected Grimmory audiobook changed. Review the match again.", status_code=409)

            prepared = self._prepare_mapping(
                None,
                ebook_filename,
                ebook_source_id,
                expected_ebook_kosync_id or initial_kosync_id,
            )
            if isinstance(prepared, IntakeResult):
                restore_claim()
                return prepared
            target, conflict = self._grimmory_audio_target(
                audio_source_id,
                prepared.merge_book,
                prepared.kosync_doc_id,
            )
            if conflict:
                restore_claim()
                return conflict
            if not self.database_service.renew_detected_book_claim(
                detected_source_id,
                processing_token,
                source=detected_source,
            ):
                return IntakeResult(error="This match lost its processing lease. Try again.", status_code=409)

            if target:
                target.ebook_filename = prepared.ebook_filename
                target.kosync_doc_id = prepared.kosync_doc_id
                target.grimmory_audio_source_id = audio_source_id
                target.sync_mode = "audiobook"
                book = self.database_service.save_book_with_kosync_ownership(target)
            else:
                book = self.database_service.save_book_with_kosync_ownership(
                    Book(
                        title=audio_info.get("title") or Path(prepared.ebook_filename).stem,
                        author=audio_info.get("authors") or None,
                        ebook_filename=prepared.ebook_filename,
                        kosync_doc_id=prepared.kosync_doc_id,
                        grimmory_audio_source_id=audio_source_id,
                        status="pending",
                        sync_mode="audiobook",
                    )
                )
        except (IntegrityError, KoSyncOwnershipConflict):
            restore_claim()
            return IntakeResult(error="The audiobook or ebook was linked by another request", status_code=409)
        except Exception:
            restore_claim()
            raise

        try:
            completed = self.database_service.complete_detected_book(
                detected_source_id,
                processing_token,
                source=detected_source,
            )
        except Exception as exc:
            logger.warning("Grimmory audiobook link committed but its detection could not be completed: %s", exc)
            completed = False
        if not completed:
            return IntakeResult(book=book)

        try:
            self._record_grimmory_source(prepared.kosync_doc_id, prepared.ebook_source_id)
            self.database_service.resolve_detected_book(audio_source_id, source="grimmory")
            self.database_service.resolve_detected_book(prepared.kosync_doc_id, source="kosync")
            self._resolve_grimmory_detection(prepared.ebook_filename, prepared.ebook_source_id)
        except Exception as exc:
            logger.warning("Grimmory audiobook link committed but companion cleanup failed: %s", exc)
        return IntakeResult(book=book)

    def _grimmory_audio_target(self, audio_source_id, ebook_book, kosync_doc_id):
        audio_book = self.database_service.get_book_by_grimmory_audio_source_id(audio_source_id)
        if audio_book and ebook_book and audio_book.id != ebook_book.id:
            return None, IntakeResult(error="The audiobook and ebook already belong to different books", status_code=409)

        target = audio_book or ebook_book
        if target and getattr(target, "grimmory_audio_source_id", None) not in (None, audio_source_id):
            return None, IntakeResult(error="The ebook already belongs to another Grimmory audiobook", status_code=409)
        if target and getattr(target, "kosync_doc_id", None) not in (None, kosync_doc_id):
            return None, IntakeResult(error="The audiobook already belongs to another ebook", status_code=409)
        return target, None

    def inspect_audiobook_ebook(
        self,
        *,
        abs_id,
        ebook_filename,
        ebook_source_id=None,
        expected_ebook_kosync_id=None,
    ) -> IntakeResult:
        prepared = self._prepare_mapping(abs_id, ebook_filename, ebook_source_id, expected_ebook_kosync_id)
        if isinstance(prepared, IntakeResult):
            return prepared
        if prepared.merge_book:
            return self._combine_conflict(prepared.merge_book)
        return IntakeResult(book=prepared.current_book)

    def _prepare_mapping(self, abs_id, ebook_filename, ebook_source_id, expected_ebook_kosync_id):
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

        current_book = self.database_service.get_book_by_ref(abs_id)
        existing_book = self.database_service.get_book_by_kosync_id(kosync_doc_id)
        if not isinstance(getattr(existing_book, "id", None), int):
            existing_book = None
        merge_book = existing_book if existing_book and getattr(existing_book, "id", None) != getattr(current_book, "id", None) else None
        return _PreparedMapping(
            abs_id=abs_id,
            ebook_filename=ebook_filename,
            ebook_source_id=ebook_source_id,
            kosync_doc_id=kosync_doc_id,
            current_book=current_book,
            merge_book=merge_book,
            grimmory_book=bl_match,
            grimmory_client=bl_match_client,
        )

    @staticmethod
    def _combine_conflict(book):
        return IntakeResult(
            error="This ebook already belongs to another PageKeeper entry",
            status_code=409,
            conflict_code="combine_required",
            conflict_book_id=book.id,
            conflict_book_title=getattr(book, "title", None),
        )

    def _mapping_matches(self, prepared):
        book = prepared.current_book
        if not book or book.ebook_filename != prepared.ebook_filename or book.kosync_doc_id != prepared.kosync_doc_id:
            return False
        if not prepared.ebook_source_id:
            return True
        doc = self.database_service.get_kosync_document(prepared.kosync_doc_id)
        return bool(doc and doc.grimmory_id == prepared.ebook_source_id)

    def _apply_mapping(
        self,
        prepared,
        *,
        title,
        duration,
        storyteller_uuid,
        storyteller_submit,
        author,
        subtitle,
        resolve_detected,
        renew_claim,
        complete_claim,
    ):
        existing_book = prepared.merge_book
        original_ebook_filename = None
        self._require_claim_ownership(renew_claim)
        if existing_book:
            logger.info("Merging exact book id=%s into '%s'", existing_book.id, prepared.abs_id)
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

        canonical_book = existing_book or prepared.current_book
        desired = Book(
            abs_id=prepared.abs_id,
            title=title,
            ebook_filename=prepared.ebook_filename,
            kosync_doc_id=prepared.kosync_doc_id,
            transcript_file=getattr(canonical_book, "transcript_file", None),
            status="pending",
            duration=duration,
            author=author,
            subtitle=subtitle,
            **merge_metadata,
        )
        self._require_claim_ownership(renew_claim)
        if existing_book:
            overrides = {
                attr: getattr(desired, attr)
                for attr in (
                    "title",
                    "author",
                    "subtitle",
                    "ebook_filename",
                    "original_ebook_filename",
                    "kosync_doc_id",
                    "transcript_file",
                    "status",
                    "duration",
                    "sync_mode",
                    "storyteller_uuid",
                    "abs_ebook_item_id",
                    "ebook_item_id",
                    "custom_cover_url",
                    "started_at",
                    "finished_at",
                    "rating",
                    "read_count",
                )
            }
            book = self.database_service.migrate_book_data_by_id(
                existing_book.id,
                prepared.abs_id,
                expected_kosync_doc_id=prepared.kosync_doc_id,
                expected_abs_id=existing_book.abs_id,
                overrides=overrides,
            )
            if not book:
                raise _MergeIdentityChanged
        else:
            book = self.database_service.save_book(desired, is_new=True)

        # The canonical mapping is committed. Follow-up bookkeeping and remote
        # integrations are best effort and must never make it look retryable.
        if complete_claim:
            try:
                completed = complete_claim()
            except Exception as exc:
                completed = False
                logger.warning("Pairing committed but its detection could not be completed: %s", exc)
            if not completed:
                logger.warning("Pairing committed after its processing lease expired")
                return IntakeResult(book=book)

        try:
            ensure_kosync_document(book, self.database_service)
            self._record_grimmory_source(prepared.kosync_doc_id, prepared.ebook_source_id)
            if storyteller_submit:
                self._create_storyteller_reservation(prepared.abs_id)
            self._resolve_mapping_suggestions(
                prepared.abs_id,
                prepared.kosync_doc_id,
                prepared.ebook_filename,
                prepared.ebook_source_id,
                resolve_detected=resolve_detected,
            )
        except Exception as exc:
            logger.warning("Pairing committed but local follow-up failed: %s", exc)

        try:
            collection_added = self.abs_service.add_to_collection(prepared.abs_id, self.collection_name)
            if collection_added is False:
                logger.warning("Pairing committed but Audiobookshelf collection update failed")
        except Exception as exc:
            logger.warning("Pairing committed but Audiobookshelf collection update failed: %s", exc)

        try:
            self.attempt_hardcover_automatch(self.container, book)
            if prepared.grimmory_client:
                self._add_to_grimmory_shelf(
                    prepared.grimmory_client, original_ebook_filename or prepared.ebook_filename
                )
            if storyteller_submit:
                self._submit_to_storyteller_async(prepared.abs_id, title, prepared.ebook_filename)
        except Exception as exc:
            logger.warning("Pairing committed but external follow-up failed: %s", exc)

        return IntakeResult(book=book)

    @staticmethod
    def _require_claim_ownership(renew_claim):
        if renew_claim and not renew_claim():
            raise _ClaimOwnershipLost

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

    def _resolve_mapping_suggestions(
        self,
        abs_id,
        kosync_doc_id,
        ebook_filename,
        ebook_source_id=None,
        *,
        resolve_detected=True,
    ):
        self.database_service.resolve_suggestion(abs_id)
        self.database_service.resolve_suggestion(kosync_doc_id)
        if resolve_detected:
            self.database_service.resolve_detected_book(abs_id, source="abs")
            if kosync_doc_id:
                self.database_service.resolve_detected_book(kosync_doc_id, source="kosync")
            self._resolve_grimmory_detection(ebook_filename, ebook_source_id)
        try:
            device_doc = self.database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                self.database_service.resolve_suggestion(device_doc.document_hash)
                if resolve_detected:
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
            "grimmory_audio_source_id": getattr(existing_book, "grimmory_audio_source_id", None),
        }
        if overrides:
            metadata.update({key: value for key, value in overrides.items() if value is not None})
        return metadata
