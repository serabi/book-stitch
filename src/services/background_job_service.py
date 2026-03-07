import logging
import os
import threading
import time
from pathlib import Path

from src.db.models import Job
from src.utils.epub_resolver import get_local_epub
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


class BackgroundJobService:
    """Handles background transcription/alignment jobs for books."""

    def __init__(self,
                 database_service,
                 abs_client,
                 booklore_clients: list,
                 ebook_parser,
                 transcriber,
                 alignment_service,
                 library_service,
                 storyteller_client,
                 epub_cache_dir,
                 data_dir,
                 books_dir):
        self.database_service = database_service
        self.abs_client = abs_client
        self._booklore_clients = booklore_clients
        self.ebook_parser = ebook_parser
        self.transcriber = transcriber
        self.alignment_service = alignment_service
        self.library_service = library_service
        self.storyteller_client = storyteller_client
        self.epub_cache_dir = epub_cache_dir
        self.data_dir = data_dir
        self.books_dir = books_dir

        self._job_thread = None
        self._job_lock = threading.Lock()

    def cleanup_stale_jobs(self):
        """Reset jobs that were interrupted mid-process on restart."""
        try:
            # Get books with crashed status and reset them to active
            crashed_books = self.database_service.get_books_by_status('crashed')
            for book in crashed_books:
                book.status = 'active'
                self.database_service.save_book(book)
                logger.info(f"Reset crashed book status: {sanitize_log_data(book.abs_title)}")

            # Check processing/failed books — recover if alignment exists, else mark failed
            candidates = self.database_service.get_books_by_status('processing') + \
                         self.database_service.get_books_by_status('failed_retry_later')

            for book in candidates:
                has_alignment = False
                if self.alignment_service:
                    has_alignment = bool(self.alignment_service._get_alignment(book.abs_id))

                if has_alignment:
                    if book.status != 'active':
                        logger.info(f"Found orphan alignment for '{book.status}' book: {sanitize_log_data(book.abs_title)} — Marking ACTIVE")
                        book.status = 'active'
                        self.database_service.save_book(book)
                elif book.status == 'processing':
                    logger.info(f"Recovering interrupted job: {sanitize_log_data(book.abs_title)}")
                    book.status = 'failed_retry_later'
                    self.database_service.save_book(book)

                    existing_job = self.database_service.get_latest_job(book.abs_id)
                    job = Job(
                        abs_id=book.abs_id,
                        last_attempt=time.time(),
                        retry_count=existing_job.retry_count if existing_job else 0,
                        last_error='Interrupted by restart'
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

            pending_books = self.database_service.get_books_by_status('pending')
            for book in pending_books:
                eligible_books.append(book)
                if not target_book:
                    target_book = book

            if not target_book:
                failed_books = self.database_service.get_books_by_status('failed_retry_later')
                for book in failed_books:
                    job = self.database_service.get_latest_job(book.abs_id)
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

            logger.info(f"[{job_idx}/{total_jobs}] Starting background transcription: {sanitize_log_data(target_book.abs_title)}")

            target_book.status = 'processing'
            self.database_service.save_book(target_book)

            # Create or update job record, preserving existing retry_count
            existing_job = self.database_service.get_latest_job(target_book.abs_id)
            job = Job(
                abs_id=target_book.abs_id,
                last_attempt=time.time(),
                retry_count=existing_job.retry_count if existing_job else 0,
                last_error=None,
                progress=0.0
            )
            self.database_service.save_job(job)

            self._job_thread = threading.Thread(
                target=self._run_background_job,
                args=(target_book, job_idx, total_jobs),
                daemon=True
            )
            self._job_thread.start()

    def _run_background_job(self, book, job_idx=1, job_total=1):
        """
        Threaded worker that handles transcription without blocking the main loop.
        """
        abs_id = book.abs_id
        abs_title = book.abs_title or 'Unknown'
        ebook_filename = book.ebook_filename
        max_retries = int(os.getenv("JOB_MAX_RETRIES", 5))

        logger.info(f"[{job_idx}/{job_total}] Processing '{sanitize_log_data(abs_title)}'")

        try:
            def update_progress(local_pct, phase):
                """
                Map local phase progress to global 0-100% progress.
                Phase 1: 0-10%
                Phase 2: 10-90%
                Phase 3: 90-100%
                """
                global_pct = 0.0
                if phase == 1:
                    global_pct = 0.0 + (local_pct * 0.1)
                elif phase == 2:
                    global_pct = 0.1 + (local_pct * 0.8)
                elif phase == 3:
                    global_pct = 0.9 + (local_pct * 0.1)

                self.database_service.update_latest_job(abs_id, progress=global_pct)

            # Phase 1: Acquire EPUB
            update_progress(0.0, 1)
            item_details = self.abs_client.get_item_details(abs_id)

            epub_path = None
            if self.library_service and item_details:
                epub_path = self.library_service.acquire_ebook(item_details)

            if not epub_path:
                epub_path = get_local_epub(
                    ebook_filename, self.books_dir, self.epub_cache_dir, self._booklore_clients
                )

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
                        logger.info(f"Locked KOSync ID: {computed_hash}")
            except Exception as e:
                logger.warning(f"Failed to eager-lock KOSync ID: {e}")

            # Phase 2: Transcription (Storyteller → SMIL → Whisper)
            raw_transcript = None
            transcript_source = None

            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
            book_text, _ = self.ebook_parser.extract_text_and_map(epub_path)

            # Priority 1: Storyteller wordTimeline
            if (book.storyteller_uuid
                    and self.storyteller_client
                    and os.environ.get('STORYTELLER_ASSETS_DIR', '').strip()):
                try:
                    st_chapters = self.storyteller_client.get_word_timeline_chapters(book.storyteller_uuid)
                    if st_chapters:
                        if not self.alignment_service:
                            logger.warning(f"Skipping Storyteller alignment for '{book.abs_title}': alignment_service not available")
                        else:
                            logger.info(f"Using Storyteller wordTimeline for '{book.abs_title}' ({len(st_chapters)} chapters)")
                            update_progress(0.5, 2)
                            success = self.alignment_service.align_storyteller_and_store(
                                abs_id, st_chapters, book_text
                            )
                            if success:
                                transcript_source = "STORYTELLER_NATIVE"
                                update_progress(1.0, 2)
                except Exception as e:
                    logger.warning(f"Storyteller wordTimeline failed for '{book.abs_title}': {e}")

            # Priority 2: SMIL extraction
            if not transcript_source and hasattr(self.transcriber, 'transcribe_from_smil'):
                raw_transcript = self.transcriber.transcribe_from_smil(
                    abs_id, epub_path, chapters,
                    full_book_text=book_text,
                    progress_callback=lambda p: update_progress(p, 2)
                )
                if raw_transcript:
                    transcript_source = "SMIL"

            # Priority 3: Whisper transcription
            if not raw_transcript and transcript_source != "STORYTELLER_NATIVE":
                logger.info("SMIL extraction skipped/failed, falling back to Whisper transcription")

                audio_files = self.abs_client.get_audio_files(abs_id)
                raw_transcript = self.transcriber.process_audio(
                    abs_id, audio_files,
                    full_book_text=book_text,
                    progress_callback=lambda p: update_progress(p, 2)
                )
                if raw_transcript:
                    transcript_source = "WHISPER"
            elif transcript_source == "SMIL":
                # SMIL handled transcription — mark phase complete
                update_progress(1.0, 2)

            # Phase 3: Alignment
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
                    abs_id, raw_transcript, book_text, chapters
                )

                update_progress(0.5, 3)

            if not success:
                raise Exception("Alignment failed to generate valid map.")

            update_progress(1.0, 3)

            book.transcript_file = "DB_MANAGED"
            book.ebook_filename = epub_path.name

            book.status = 'active'
            self.database_service.save_book(book)

            job = self.database_service.get_latest_job(abs_id)
            if job:
                job.retry_count = 0
                job.last_error = None
                job.progress = 1.0
                self.database_service.save_job(job)
            else:
                logger.warning(f"Job record not found for completed book: {abs_id}")


            logger.info(f"Completed: {sanitize_log_data(abs_title)}")

        except Exception as e:
            logger.error(f"{sanitize_log_data(abs_title)}: {e}")

            job = self.database_service.get_latest_job(abs_id)
            current_retry_count = job.retry_count if job else 0
            new_retry_count = current_retry_count + 1

            updated_job = Job(
                abs_id=abs_id,
                last_attempt=time.time(),
                retry_count=new_retry_count,
                last_error=str(e),
                progress=job.progress if job else 0.0
            )
            self.database_service.save_job(updated_job)

            if new_retry_count >= max_retries:
                book.status = 'failed_permanent'
                logger.warning(f"{sanitize_log_data(abs_title)}: Max retries exceeded")

                if self.data_dir:
                    import shutil
                    audio_cache_dir = Path(self.data_dir) / "audio_cache" / abs_id
                    if audio_cache_dir.exists():
                        try:
                            shutil.rmtree(audio_cache_dir)
                            logger.info(f"Cleaned up audio cache for {sanitize_log_data(abs_title)}")
                        except Exception as cleanup_err:
                            logger.warning(f"Failed to clean audio cache: {cleanup_err}")
            else:
                book.status = 'failed_retry_later'

            self.database_service.save_book(book)
