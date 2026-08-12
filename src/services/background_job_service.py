import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.db.models import Job
from src.services.storyteller_submission_service import StorytellerDeferral
from src.sync_clients.abs_sync_client import TRANSCRIPT_DB_MANAGED
from src.utils.epub_resolver import get_local_epub
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


class BackgroundJobService:
    """Handles background transcription/alignment jobs for books."""

    def __init__(
        self,
        database_service,
        abs_client,
        grimmory_client,
        ebook_parser,
        transcriber,
        alignment_service,
        library_service,
        storyteller_client,
        storyteller_submission_service,
        epub_cache_dir,
        data_dir,
        books_dir,
    ):
        self.database_service = database_service
        self.abs_client = abs_client
        self.grimmory_client = grimmory_client
        self.ebook_parser = ebook_parser
        self.transcriber = transcriber
        self.alignment_service = alignment_service
        self.library_service = library_service
        self.storyteller_client = storyteller_client
        self.storyteller_submission_service = storyteller_submission_service
        self.epub_cache_dir = epub_cache_dir
        self.data_dir = data_dir
        self.books_dir = books_dir

        self._job_thread = None
        self._job_lock = threading.Lock()

    def prune_hardcover_sync_logs(self):
        """Delete Hardcover sync log entries older than the configured retention period."""
        try:
            retention_days = int(os.getenv("HARDCOVER_LOG_RETENTION_DAYS", 90))
            cutoff = datetime.now(UTC) - timedelta(days=retention_days)
            deleted = self.database_service.prune_hardcover_sync_logs(cutoff)
            if deleted:
                logger.info(f"Pruned {deleted} Hardcover sync log entries older than {retention_days} days")
        except Exception as e:
            logger.warning(f"Could not prune Hardcover sync logs: {e}")

    def cleanup_stale_jobs(self):
        """Reset jobs that were interrupted mid-process on restart."""
        try:
            # Get books with crashed status and reset them to active
            crashed_books = self.database_service.get_books_by_status("crashed")
            for book in crashed_books:
                book.status = "active"
                self.database_service.save_book(book)
                logger.info(f"Reset crashed book status: {sanitize_log_data(book.title)}")

            # Check stale books — recover if alignment exists, else handle per-status
            candidates = (
                self.database_service.get_books_by_status("processing")
                + self.database_service.get_books_by_status("failed_retry_later")
                + self.database_service.get_books_by_status("pending")
                + self.database_service.get_books_by_status("failed_permanent")
            )

            for book in candidates:
                has_alignment = False
                if self.alignment_service:
                    has_alignment = self.alignment_service.has_alignment(book.id)

                if has_alignment:
                    if book.status != "active":
                        logger.info(
                            f"Found orphan alignment for '{book.status}' book: {sanitize_log_data(book.title)} — Marking ACTIVE"
                        )
                        book.status = "active"
                        self.database_service.save_book(book)
                elif book.status == "processing":
                    logger.info(f"Recovering interrupted job: {sanitize_log_data(book.title)}")
                    book.status = "failed_retry_later"
                    self.database_service.save_book(book)

                    existing_job = self.database_service.get_latest_job(book.id)
                    job = Job(
                        abs_id=book.abs_id,
                        book_id=book.id,
                        last_attempt=time.time(),
                        retry_count=existing_job.retry_count if existing_job else 0,
                        last_error="Interrupted by restart",
                    )
                    self.database_service.save_job(job)

        except Exception as e:
            logger.error(f"Error cleaning up stale jobs: {e}")

    def check_pending_jobs(self):
        """
        Check for pending jobs and run them in a BACKGROUND thread
        so we don't block the sync cycle.
        """
        with self._job_lock:
            if self._job_thread and self._job_thread.is_alive():
                return

            target_book = None
            eligible_books = []
            max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))
            retry_delay_mins = int(os.getenv("JOB_RETRY_DELAY_MINS", 15))

            pending_books = self.database_service.get_books_by_status("pending")
            for book in pending_books:
                eligible_books.append(book)
                if not target_book:
                    target_book = book

            if not target_book:
                failed_books = self.database_service.get_books_by_status("failed_retry_later")
                for book in failed_books:
                    job = self.database_service.get_latest_job(book.id)
                    if job:
                        retry_count = job.retry_count or 0
                        last_attempt = job.last_attempt or 0

                        if retry_count >= max_retries:
                            continue

                        if time.time() - last_attempt > retry_delay_mins * 60:
                            eligible_books.append(book)
                            if not target_book:
                                target_book = book

            if not target_book:
                return

            total_jobs = len(eligible_books)
            job_idx = (eligible_books.index(target_book) + 1) if total_jobs else 1

            logger.info(
                f"[{job_idx}/{total_jobs}] Starting background transcription: {sanitize_log_data(target_book.title)}"
            )

            target_book.status = "processing"
            self.database_service.save_book(target_book)

            # Create or update job record, preserving existing retry_count
            existing_job = self.database_service.get_latest_job(target_book.id)
            job = Job(
                abs_id=target_book.abs_id,
                book_id=target_book.id,
                last_attempt=time.time(),
                retry_count=existing_job.retry_count if existing_job else 0,
                last_error=None,
                progress=0.0,
            )
            self.database_service.save_job(job)

            self._job_thread = threading.Thread(
                target=self._run_background_job, args=(target_book, job_idx, total_jobs), daemon=True
            )
            self._job_thread.start()

    def _run_background_job(self, book, job_idx=1, job_total=1):
        """Threaded worker that handles transcription without blocking the main loop."""
        abs_id = book.abs_id
        book_title = book.title or "Unknown"
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))

        logger.info(f"[{job_idx}/{job_total}] Processing '{sanitize_log_data(book_title)}'")

        def update_progress(local_pct, phase):
            """Map local phase progress to global 0-100% (Phase 1: 0-10%, Phase 2: 10-90%, Phase 3: 90-100%)."""
            phase_map = {1: (0.0, 0.1), 2: (0.1, 0.8), 3: (0.9, 0.1)}
            base, span = phase_map.get(phase, (0.0, 0.0))
            self.database_service.update_latest_job(book.id, progress=base + (local_pct * span))

        try:
            epub_path, item_details = self._phase_acquire_epub(book, update_progress)
            raw_transcript, transcript_source, book_text, chapters = self._phase_transcription(
                book, abs_id, book_title, epub_path, item_details, update_progress
            )
            self._phase_alignment(
                book,
                abs_id,
                book_title,
                epub_path,
                raw_transcript,
                transcript_source,
                book_text,
                chapters,
                update_progress,
            )

        except StorytellerDeferral as e:
            logger.info(f"{sanitize_log_data(book_title)}: {e}")
            job = self.database_service.get_latest_job(book.id)
            self.database_service.save_job(
                Job(
                    abs_id=abs_id,
                    book_id=book.id,
                    last_attempt=time.time(),
                    retry_count=job.retry_count if job else 0,
                    last_error=str(e),
                    progress=job.progress if job else 0.0,
                )
            )
            book.status = "failed_retry_later"
            self.database_service.save_book(book)

        except Exception as e:
            logger.error(f"{sanitize_log_data(book_title)}: {e}")
            job = self.database_service.get_latest_job(book.id)
            new_retry_count = (job.retry_count if job else 0) + 1
            self.database_service.save_job(
                Job(
                    abs_id=abs_id,
                    book_id=book.id,
                    last_attempt=time.time(),
                    retry_count=new_retry_count,
                    last_error=str(e),
                    progress=job.progress if job else 0.0,
                )
            )
            if new_retry_count >= max_retries:
                book.status = "failed_permanent"
                logger.warning(f"{sanitize_log_data(book_title)}: Max retries exceeded")
                if self.data_dir:
                    import shutil

                    audio_cache_dir = Path(self.data_dir) / "audio_cache" / abs_id
                    if audio_cache_dir.exists():
                        try:
                            shutil.rmtree(audio_cache_dir)
                            logger.info(f"Cleaned up audio cache for {sanitize_log_data(book_title)}")
                        except Exception as cleanup_err:
                            logger.warning(f"Failed to clean audio cache: {cleanup_err}")
            else:
                book.status = "failed_retry_later"
            self.database_service.save_book(book)

    def _phase_acquire_epub(self, book, update_progress):
        """Phase 1 (0-10%): Locate or download the EPUB file."""
        abs_id = book.abs_id
        ebook_filename = book.ebook_filename
        update_progress(0.0, 1)

        item_details = self.abs_client.get_item_details(abs_id)

        epub_path = None
        if self.library_service and item_details:
            try:
                epub_path = self.library_service.acquire_ebook(item_details)
            except Exception as e:
                logger.warning(
                    f"Failed to acquire ebook from library service for '{sanitize_log_data(ebook_filename)}': {e}"
                )

        if not epub_path:
            epub_path = get_local_epub(ebook_filename, self.books_dir, self.epub_cache_dir, self.grimmory_client)

        update_progress(1.0, 1)
        if not epub_path:
            raise FileNotFoundError(f"Could not locate or download: {ebook_filename}")

        epub_path = Path(epub_path)

        # Lock KOSync hash from original EPUB before any Storyteller modifications
        try:
            if not book.kosync_doc_id:
                logger.info(f"Locking KOSync ID from original EPUB: {epub_path.name}")
                computed_hash = self.ebook_parser.get_kosync_id(epub_path)
                if computed_hash:
                    book.kosync_doc_id = computed_hash
                    if not book.original_ebook_filename:
                        book.original_ebook_filename = book.ebook_filename
                    self.database_service.save_book(book)
                    from src.services.kosync_service import ensure_kosync_document

                    ensure_kosync_document(book, self.database_service)
                    logger.info(f"Locked KOSync ID: {computed_hash}")
        except Exception as e:
            logger.warning(f"Failed to eager-lock KOSync ID: {e}")

        return epub_path, item_details

    def _phase_transcription(self, book, abs_id, book_title, epub_path, item_details, update_progress):
        """Phase 2 (10-90%): Generate transcript via Storyteller, SMIL, or Whisper."""
        raw_transcript = None
        transcript_source = None

        chapters = item_details.get("media", {}).get("chapters", []) if item_details else []
        book_text, _ = self.ebook_parser.extract_text_and_map(epub_path)

        # Priority 1: Storyteller wordTimeline
        storyteller_force = os.getenv("STORYTELLER_FORCE_MODE", "false").lower() == "true"
        transcript_source = self._try_storyteller_alignment(book, abs_id, book_text, update_progress)

        if transcript_source == "STORYTELLER_PENDING":
            raise StorytellerDeferral("Storyteller processing not yet complete, deferring until ready")

        if storyteller_force and transcript_source != "STORYTELLER_NATIVE":
            if book.storyteller_uuid:
                raise StorytellerDeferral(
                    "Storyteller alignment not available yet (force mode enabled, skipping Whisper)"
                )
            else:
                auto_submitted = self._auto_submit_to_storyteller(book, abs_id, book_title, epub_path)
                if auto_submitted:
                    raise StorytellerDeferral(
                        "Auto-submitted to Storyteller (force mode enabled, waiting for processing)"
                    )
                else:
                    logger.warning(
                        f"Force Storyteller mode is on but auto-submission unavailable for "
                        f"'{sanitize_log_data(book_title)}' — falling back to SMIL/Whisper"
                    )

        if transcript_source != "STORYTELLER_NATIVE":
            # Priority 2: SMIL extraction
            if not transcript_source and hasattr(self.transcriber, "transcribe_from_smil"):
                try:
                    raw_transcript = self.transcriber.transcribe_from_smil(
                        abs_id,
                        epub_path,
                        chapters,
                        full_book_text=book_text,
                        progress_callback=lambda p: update_progress(p, 2),
                    )
                except Exception as e:
                    raw_transcript = None
                    transcript_source = None
                    logger.warning(f"SMIL extraction failed for '{book.title}': {e}")
                if raw_transcript:
                    transcript_source = "SMIL"

            # Priority 3: Whisper transcription
            if not raw_transcript and transcript_source != "STORYTELLER_NATIVE":
                logger.info("SMIL extraction skipped/failed, falling back to Whisper transcription")
                audio_source_id = getattr(book, "grimmory_audio_source_id", None)
                if abs_id:
                    transcription_id = abs_id
                    audio_files = self.abs_client.get_audio_files(abs_id)
                elif audio_source_id:
                    transcription_id = audio_source_id
                    audio_files = self.grimmory_client.get_audio_files(audio_source_id)
                else:
                    transcription_id = f"book-{book.id}"
                    audio_files = []
                raw_transcript = self.transcriber.process_audio(
                    transcription_id,
                    audio_files,
                    full_book_text=book_text,
                    progress_callback=lambda p: update_progress(p, 2),
                )
                if raw_transcript:
                    transcript_source = "WHISPER"
            elif transcript_source == "SMIL":
                update_progress(1.0, 2)

        return raw_transcript, transcript_source, book_text, chapters

    def _phase_alignment(
        self,
        book,
        abs_id,
        book_title,
        epub_path,
        raw_transcript,
        transcript_source,
        book_text,
        chapters,
        update_progress,
    ):
        """Phase 3 (90-100%): Align transcript to book text and finalize."""
        if transcript_source == "STORYTELLER_NATIVE":
            update_progress(0.5, 3)
            success = True
        else:
            if not raw_transcript:
                raise Exception("Failed to generate transcript from both SMIL and Whisper.")
            if not self.alignment_service:
                raise Exception("Cannot align transcript: alignment_service not available")

            logger.info(f"Aligning transcript ({transcript_source}) using Anchored Alignment...")
            update_progress(0.1, 3)
            success = self.alignment_service.align_and_store(
                book.id, raw_transcript, book_text, chapters, source=transcript_source.lower()
            )
            update_progress(0.5, 3)

        if not success:
            raise Exception("Alignment failed to generate valid map.")

        update_progress(1.0, 3)

        book.transcript_file = TRANSCRIPT_DB_MANAGED
        book.ebook_filename = epub_path.name
        book.status = "active"
        self.database_service.save_book(book)

        job = self.database_service.get_latest_job(book.id)
        if job:
            job.retry_count = 0
            job.last_error = None
            job.progress = 1.0
            self.database_service.save_job(job)
        else:
            logger.warning(f"Job record not found for completed book: {abs_id}")

        logger.info(f"Completed: {sanitize_log_data(book_title)}")

    def _try_storyteller_alignment(self, book, abs_id, book_text, update_progress) -> str | None:
        """Attempt Storyteller word-timeline alignment.

        Returns "STORYTELLER_NATIVE" on success, "STORYTELLER_PENDING" if a
        submission is still processing, or None on failure/unavailable.
        """
        if not book.storyteller_uuid and not self.storyteller_client:
            return None

        # If the book already has a UUID, try alignment directly first —
        # don't defer to submission status when data might already be available
        if book.storyteller_uuid and self.storyteller_client and os.environ.get("STORYTELLER_ASSETS_DIR", "").strip():
            try:
                st_chapters = self.storyteller_client.get_word_timeline_chapters(book.storyteller_uuid)
                if st_chapters:
                    # Transcriptions are ready — mark any active submission as done and align
                    submission = self.database_service.get_active_storyteller_submission_by_book_id(book.id)
                    if submission and submission.status not in ("ready", "failed"):
                        if self.storyteller_submission_service:
                            self.storyteller_submission_service._update_submission_status(submission, "ready")
                    return self._do_storyteller_alignment(book, abs_id, st_chapters, book_text, update_progress)
            except Exception as e:
                logger.debug(f"Direct Storyteller alignment check failed for '{sanitize_log_data(book.title)}': {e}")

        # Fall through: check submission status if no direct alignment was possible
        submission = self.database_service.get_active_storyteller_submission_by_book_id(book.id)
        if submission and submission.status in ("queued", "processing"):
            if self.storyteller_submission_service:
                fresh_status = self.storyteller_submission_service.check_status(abs_id)
                if fresh_status == "ready":
                    # Storyteller finished! Update the book's storyteller_uuid and proceed to alignment
                    updated_sub = self.database_service.get_storyteller_submission_by_book_id(book.id)
                    if updated_sub and updated_sub.storyteller_uuid and not book.storyteller_uuid:
                        book.storyteller_uuid = updated_sub.storyteller_uuid
                        self.database_service.save_book(book)
                    logger.info(f"Storyteller processing complete for '{sanitize_log_data(book.title)}'")
                else:
                    logger.info(f"Storyteller processing not yet complete for '{sanitize_log_data(book.title)}'")
                    return "STORYTELLER_PENDING"
            else:
                logger.info(f"Storyteller processing not yet complete for '{sanitize_log_data(book.title)}'")
                return "STORYTELLER_PENDING"

        if not (
            book.storyteller_uuid and self.storyteller_client and os.environ.get("STORYTELLER_ASSETS_DIR", "").strip()
        ):
            return None

        try:
            st_chapters = self.storyteller_client.get_word_timeline_chapters(book.storyteller_uuid)
            if not st_chapters:
                return None
            return self._do_storyteller_alignment(book, abs_id, st_chapters, book_text, update_progress)
        except Exception as e:
            logger.warning(f"Storyteller wordTimeline failed for '{sanitize_log_data(book.title)}': {e}")
        return None

    def _do_storyteller_alignment(self, book, abs_id, st_chapters, book_text, update_progress) -> str | None:
        """Run Storyteller alignment once chapters are available."""
        if not self.alignment_service:
            logger.warning(
                f"Skipping Storyteller alignment for '{sanitize_log_data(book.title)}': alignment_service not available"
            )
            return None
        logger.info(
            f"Using Storyteller wordTimeline for '{sanitize_log_data(book.title)}' ({len(st_chapters)} chapters)"
        )
        update_progress(0.5, 2)
        success = self.alignment_service.align_storyteller_and_store(book.id, st_chapters, book_text)
        if success:
            update_progress(1.0, 2)
            return "STORYTELLER_NATIVE"
        return None

    def _auto_submit_to_storyteller(self, book, abs_id, book_title, epub_path) -> bool:
        """Auto-submit a book to Storyteller when force mode is on.

        Returns True if submission was successful and the job should defer.
        """
        if not self.storyteller_submission_service or not self.storyteller_submission_service.is_available():
            logger.warning(
                f"Cannot auto-submit '{sanitize_log_data(book_title)}' to Storyteller: "
                "submission service not available (check STORYTELLER_IMPORT_DIR)"
            )
            return False

        audio_files = self.abs_client.get_audio_files(abs_id)
        if not audio_files:
            logger.warning(f"Cannot auto-submit '{sanitize_log_data(book_title)}' to Storyteller: no audio files found")
            return False

        logger.info(f"Auto-submitting '{sanitize_log_data(book_title)}' to Storyteller (force mode)")
        result = self.storyteller_submission_service.submit_book(
            abs_id=abs_id,
            title=book_title,
            ebook_path=epub_path,
            audio_files=audio_files,
        )
        if result.success:
            logger.info(
                f"Auto-submitted '{sanitize_log_data(book_title)}' to Storyteller: "
                f"{len(result.files_copied)} files copied"
            )
            return True
        else:
            logger.error(f"Auto-submission to Storyteller failed for '{sanitize_log_data(book_title)}': {result.error}")
            return False
