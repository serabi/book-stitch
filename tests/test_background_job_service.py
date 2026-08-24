"""Tests for BackgroundJobService — focused on error paths and job lifecycle."""

import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.db.models import Job
from src.services.background_job_service import BackgroundJobService
from src.services.storyteller_submission_service import StorytellerDeferral


def _make_service(**overrides):
    """Create a BackgroundJobService with mock dependencies."""
    defaults = dict(
        database_service=Mock(),
        abs_client=Mock(),
        grimmory_client=Mock(),
        ebook_parser=Mock(),
        transcriber=Mock(),
        alignment_service=Mock(),
        library_service=Mock(),
        storyteller_client=None,
        storyteller_submission_service=None,
        epub_cache_dir="/tmp/epub_cache",
        data_dir="/tmp/data",
        books_dir="/tmp/books",
    )
    defaults.update(overrides)
    return BackgroundJobService(**defaults)


def _make_book(**kwargs):
    """Create a mock book object with sensible defaults."""
    book = Mock()
    book.id = kwargs.get("id", 1)
    book.abs_id = kwargs.get("abs_id", "abs-123")
    book.title = kwargs.get("title", "Test Book")
    book.status = kwargs.get("status", "pending")
    book.ebook_filename = kwargs.get("ebook_filename", "test.epub")
    book.storyteller_uuid = kwargs.get("storyteller_uuid", None)
    book.kosync_doc_id = kwargs.get("kosync_doc_id", None)
    book.original_ebook_filename = kwargs.get("original_ebook_filename", None)
    book.transcript_file = kwargs.get("transcript_file", None)
    book.grimmory_audio_source_id = kwargs.get("grimmory_audio_source_id", None)
    return book


def _make_job(**kwargs):
    """Create a mock job object."""
    job = Mock(spec=Job)
    job.retry_count = kwargs.get("retry_count", 0)
    job.last_attempt = kwargs.get("last_attempt", 0)
    job.last_error = kwargs.get("last_error", None)
    job.progress = kwargs.get("progress", 0.0)
    job.book_id = kwargs.get("book_id", 1)
    job.abs_id = kwargs.get("abs_id", "abs-123")
    return job


# ---------------------------------------------------------------------------
# check_pending_jobs
# ---------------------------------------------------------------------------


class TestCheckPendingJobs:
    def test_no_pending_or_failed_jobs(self):
        db = Mock()
        db.get_books_by_status.return_value = []
        service = _make_service(database_service=db)

        service.check_pending_jobs()

        # Called for "pending", then "failed_retry_later"
        assert db.get_books_by_status.call_count == 2
        db.save_book.assert_not_called()
        db.save_job.assert_not_called()

    def test_skips_if_thread_alive(self):
        db = Mock()
        service = _make_service(database_service=db)
        service._job_thread = Mock()
        service._job_thread.is_alive.return_value = True

        service.check_pending_jobs()

        db.get_books_by_status.assert_not_called()

    def test_starts_thread_for_pending_book(self):
        book = _make_book()
        db = Mock()
        db.get_books_by_status.return_value = [book]
        db.get_latest_job.return_value = None
        service = _make_service(database_service=db)

        with patch("threading.Thread") as mock_thread:
            mock_instance = Mock()
            mock_thread.return_value = mock_instance
            mock_instance.is_alive.return_value = False

            service.check_pending_jobs()

            assert book.status == "processing"
            db.save_book.assert_called_with(book)
            db.save_job.assert_called_once()
            mock_thread.assert_called_once()
            mock_instance.start.assert_called_once()

    def test_retry_respects_max_retries(self):
        """Books at max retries are skipped."""
        book = _make_book(status="failed_retry_later")
        job = _make_job(retry_count=5, last_attempt=0)

        db = Mock()
        db.get_books_by_status.side_effect = lambda s: (
            [] if s == "pending" else [book] if s == "failed_retry_later" else []
        )
        db.get_latest_job.return_value = job

        service = _make_service(database_service=db)

        with patch.dict("os.environ", {"JOB_MAX_RETRIES": "5"}):
            service.check_pending_jobs()

        db.save_book.assert_not_called()

    def test_retry_respects_delay(self):
        """Books within retry delay window are skipped."""
        book = _make_book(status="failed_retry_later")
        job = _make_job(retry_count=1, last_attempt=time.time())  # just now

        db = Mock()
        db.get_books_by_status.side_effect = lambda s: (
            [] if s == "pending" else [book] if s == "failed_retry_later" else []
        )
        db.get_latest_job.return_value = job

        service = _make_service(database_service=db)

        with patch.dict("os.environ", {"JOB_RETRY_DELAY_MINS": "15"}):
            service.check_pending_jobs()

        db.save_book.assert_not_called()

    def test_retry_picks_up_eligible_book(self):
        """Book past retry delay and under max retries gets picked up."""
        book = _make_book(status="failed_retry_later")
        job = _make_job(retry_count=1, last_attempt=time.time() - 3600)

        db = Mock()
        db.get_books_by_status.side_effect = lambda s: (
            [] if s == "pending" else [book] if s == "failed_retry_later" else []
        )
        db.get_latest_job.return_value = job

        service = _make_service(database_service=db)

        with (
            patch("threading.Thread") as mock_thread,
            patch.dict("os.environ", {"JOB_MAX_RETRIES": "5", "JOB_RETRY_DELAY_MINS": "15"}),
        ):
            mock_instance = Mock()
            mock_thread.return_value = mock_instance
            mock_instance.is_alive.return_value = False

            service.check_pending_jobs()

            assert book.status == "processing"
            mock_instance.start.assert_called_once()

    def test_preserves_existing_retry_count(self):
        """When creating a job record for a pending book with existing job, retry_count is preserved."""
        book = _make_book()
        existing_job = _make_job(retry_count=3)

        db = Mock()
        db.get_books_by_status.return_value = [book]
        db.get_latest_job.return_value = existing_job

        service = _make_service(database_service=db)

        with patch("threading.Thread") as mock_thread:
            mock_instance = Mock()
            mock_thread.return_value = mock_instance
            mock_instance.is_alive.return_value = False

            service.check_pending_jobs()

            saved_job = db.save_job.call_args[0][0]
            assert saved_job.retry_count == 3


# ---------------------------------------------------------------------------
# _run_background_job
# ---------------------------------------------------------------------------


class TestRunBackgroundJob:
    def test_success_flow(self):
        """All three phases succeed — book becomes active."""
        book = _make_book()
        db = Mock()
        job = _make_job()
        db.get_latest_job.return_value = job
        service = _make_service(database_service=db)

        service._phase_acquire_epub = Mock(return_value=(Path("/tmp/test.epub"), {}))
        service._phase_transcription = Mock(return_value=("transcript", "WHISPER", "text", []))
        service._phase_alignment = Mock()

        service._run_background_job(book)

        service._phase_acquire_epub.assert_called_once()
        service._phase_transcription.assert_called_once()
        service._phase_alignment.assert_called_once()

    def test_storyteller_deferral_does_not_increment_retry(self):
        """StorytellerDeferral sets failed_retry_later without incrementing retry_count."""
        book = _make_book()
        db = Mock()
        existing_job = _make_job(retry_count=2, progress=0.3)
        db.get_latest_job.return_value = existing_job
        service = _make_service(database_service=db)

        service._phase_acquire_epub = Mock(side_effect=StorytellerDeferral("waiting"))

        service._run_background_job(book)

        assert book.status == "failed_retry_later"
        db.save_book.assert_called_with(book)
        saved_job = db.save_job.call_args[0][0]
        assert saved_job.retry_count == 2  # not incremented

    def test_exception_increments_retry_count(self):
        """Generic exception increments retry_count."""
        book = _make_book()
        db = Mock()
        existing_job = _make_job(retry_count=1, progress=0.1)
        db.get_latest_job.return_value = existing_job
        service = _make_service(database_service=db)

        service._phase_acquire_epub = Mock(side_effect=RuntimeError("disk full"))

        with patch.dict("os.environ", {"JOB_MAX_RETRIES": "5"}):
            service._run_background_job(book)

        assert book.status == "failed_retry_later"
        saved_job = db.save_job.call_args[0][0]
        assert saved_job.retry_count == 2
        assert saved_job.last_error == "disk full"

    def test_max_retries_marks_permanent_failure(self):
        """Hitting max retries sets status to failed_permanent."""
        book = _make_book()
        db = Mock()
        existing_job = _make_job(retry_count=4, progress=0.5)
        db.get_latest_job.return_value = existing_job
        service = _make_service(database_service=db)

        service._phase_acquire_epub = Mock(side_effect=RuntimeError("always fails"))

        with patch.dict("os.environ", {"JOB_MAX_RETRIES": "5"}):
            service._run_background_job(book)

        assert book.status == "failed_permanent"

    def test_max_retries_cleans_audio_cache(self):
        """On permanent failure, audio cache directory is cleaned up."""
        book = _make_book(abs_id="abc-456")
        db = Mock()
        existing_job = _make_job(retry_count=4)
        db.get_latest_job.return_value = existing_job
        service = _make_service(database_service=db, data_dir="/tmp/data")

        service._phase_acquire_epub = Mock(side_effect=RuntimeError("fail"))

        with (
            patch.dict("os.environ", {"JOB_MAX_RETRIES": "5"}),
            patch("shutil.rmtree") as mock_rmtree,
            patch.object(Path, "exists", return_value=True),
        ):
            service._run_background_job(book)

            mock_rmtree.assert_called_once()

    def test_max_retries_audio_cache_cleanup_failure_logged(self):
        """Cache cleanup failure is caught and logged, not re-raised."""
        book = _make_book(abs_id="abc-456")
        db = Mock()
        existing_job = _make_job(retry_count=4)
        db.get_latest_job.return_value = existing_job
        service = _make_service(database_service=db, data_dir="/tmp/data")

        service._phase_acquire_epub = Mock(side_effect=RuntimeError("fail"))

        with (
            patch.dict("os.environ", {"JOB_MAX_RETRIES": "5"}),
            patch("shutil.rmtree", side_effect=OSError("permission denied")),
            patch.object(Path, "exists", return_value=True),
        ):
            # Should not raise
            service._run_background_job(book)

        assert book.status == "failed_permanent"

    def test_exception_with_no_existing_job(self):
        """When get_latest_job returns None, retry_count starts from 0."""
        book = _make_book()
        db = Mock()
        db.get_latest_job.return_value = None
        service = _make_service(database_service=db)

        service._phase_acquire_epub = Mock(side_effect=RuntimeError("oops"))

        with patch.dict("os.environ", {"JOB_MAX_RETRIES": "5"}):
            service._run_background_job(book)

        saved_job = db.save_job.call_args[0][0]
        assert saved_job.retry_count == 1
        assert book.status == "failed_retry_later"


# ---------------------------------------------------------------------------
# cleanup_stale_jobs
# ---------------------------------------------------------------------------


class TestCleanupStaleJobs:
    def test_resets_crashed_books(self):
        crashed_book = _make_book(status="crashed")
        db = Mock()
        db.get_books_by_status.side_effect = lambda s: [crashed_book] if s == "crashed" else []
        service = _make_service(database_service=db)

        service.cleanup_stale_jobs()

        assert crashed_book.status == "active"
        db.save_book.assert_called_with(crashed_book)

    def test_recovers_processing_book_with_alignment(self):
        """Processing book with existing alignment is set to active."""
        book = _make_book(status="processing")
        alignment = Mock()
        alignment.has_alignment.return_value = True

        db = Mock()
        db.get_books_by_status.side_effect = lambda s: [book] if s == "processing" else []
        service = _make_service(database_service=db, alignment_service=alignment)

        service.cleanup_stale_jobs()

        assert book.status == "active"

    def test_recovers_processing_book_without_alignment(self):
        """Processing book without alignment is set to failed_retry_later with a job record."""
        book = _make_book(status="processing")
        existing_job = _make_job(retry_count=2)

        alignment = Mock()
        alignment.has_alignment.return_value = False

        db = Mock()
        db.get_books_by_status.side_effect = lambda s: [book] if s == "processing" else []
        db.get_latest_job.return_value = existing_job
        service = _make_service(database_service=db, alignment_service=alignment)

        service.cleanup_stale_jobs()

        assert book.status == "failed_retry_later"
        saved_job = db.save_job.call_args[0][0]
        assert saved_job.retry_count == 2
        assert saved_job.last_error == "Interrupted by restart"

    def test_processing_book_no_existing_job_gets_zero_retries(self):
        """Processing book without existing job record gets retry_count=0."""
        book = _make_book(status="processing")
        alignment = Mock()
        alignment.has_alignment.return_value = False

        db = Mock()
        db.get_books_by_status.side_effect = lambda s: [book] if s == "processing" else []
        db.get_latest_job.return_value = None
        service = _make_service(database_service=db, alignment_service=alignment)

        service.cleanup_stale_jobs()

        saved_job = db.save_job.call_args[0][0]
        assert saved_job.retry_count == 0

    def test_exception_in_cleanup_is_caught(self):
        """Errors during cleanup are logged, not raised."""
        db = Mock()
        db.get_books_by_status.side_effect = RuntimeError("db gone")
        service = _make_service(database_service=db)

        # Should not raise
        service.cleanup_stale_jobs()

    def test_failed_permanent_with_alignment_becomes_active(self):
        """Even failed_permanent books become active if alignment data exists."""
        book = _make_book(status="failed_permanent")
        alignment = Mock()
        alignment.has_alignment.return_value = True

        db = Mock()
        db.get_books_by_status.side_effect = lambda s: [book] if s == "failed_permanent" else []
        service = _make_service(database_service=db, alignment_service=alignment)

        service.cleanup_stale_jobs()

        assert book.status == "active"


# ---------------------------------------------------------------------------
# prune_hardcover_sync_logs
# ---------------------------------------------------------------------------


class TestPruneHardcoverSyncLogs:
    def test_prune_calls_db(self):
        db = Mock()
        db.prune_hardcover_sync_logs.return_value = 5
        service = _make_service(database_service=db)

        with patch.dict("os.environ", {"HARDCOVER_LOG_RETENTION_DAYS": "30"}):
            service.prune_hardcover_sync_logs()

        db.prune_hardcover_sync_logs.assert_called_once()

    def test_prune_zero_deleted_no_error(self):
        db = Mock()
        db.prune_hardcover_sync_logs.return_value = 0
        service = _make_service(database_service=db)

        service.prune_hardcover_sync_logs()

        db.prune_hardcover_sync_logs.assert_called_once()

    def test_prune_exception_is_caught(self):
        db = Mock()
        db.prune_hardcover_sync_logs.side_effect = RuntimeError("db locked")
        service = _make_service(database_service=db)

        # Should not raise
        service.prune_hardcover_sync_logs()


# ---------------------------------------------------------------------------
# _phase_acquire_epub
# ---------------------------------------------------------------------------


class TestPhaseAcquireEpub:
    def test_library_service_failure_falls_back(self):
        """If library_service.acquire_ebook raises, falls back to get_local_epub."""
        book = _make_book()
        db = Mock()
        lib = Mock()
        lib.acquire_ebook.side_effect = RuntimeError("nfs down")
        abs_client = Mock()
        abs_client.get_item_details.return_value = {"some": "details"}
        ebook_parser = Mock()
        ebook_parser.get_kosync_id.return_value = None

        service = _make_service(
            database_service=db,
            abs_client=abs_client,
            library_service=lib,
            ebook_parser=ebook_parser,
        )

        with patch("src.services.background_job_service.get_local_epub", return_value="/tmp/test.epub"):
            epub_path, details = service._phase_acquire_epub(book, Mock())

        assert epub_path == Path("/tmp/test.epub")

    def test_no_epub_found_raises(self):
        """FileNotFoundError when no epub source works."""
        book = _make_book()
        abs_client = Mock()
        abs_client.get_item_details.return_value = {}
        service = _make_service(abs_client=abs_client, library_service=None)

        with patch("src.services.background_job_service.get_local_epub", return_value=None):
            with pytest.raises(FileNotFoundError, match="Could not locate"):
                service._phase_acquire_epub(book, Mock())

    def test_kosync_lock_failure_is_caught(self):
        """Failure to lock KOSync ID is caught, not raised."""
        book = _make_book(kosync_doc_id=None)
        abs_client = Mock()
        abs_client.get_item_details.return_value = {"id": "test"}  # truthy so library_service is used
        lib = Mock()
        lib.acquire_ebook.return_value = "/tmp/test.epub"
        ebook_parser = Mock()
        ebook_parser.get_kosync_id.side_effect = RuntimeError("epub corrupt")
        db = Mock()

        service = _make_service(
            database_service=db,
            abs_client=abs_client,
            library_service=lib,
            ebook_parser=ebook_parser,
        )

        epub_path, _ = service._phase_acquire_epub(book, Mock())

        assert epub_path == Path("/tmp/test.epub")
        assert book.kosync_doc_id is None  # not set due to error


# ---------------------------------------------------------------------------
# _phase_alignment
# ---------------------------------------------------------------------------


class TestPhaseAlignment:
    def test_storyteller_native_skips_transcript(self):
        """STORYTELLER_NATIVE source skips transcript requirement."""
        book = _make_book()
        db = Mock()
        job = _make_job()
        db.get_latest_job.return_value = job
        service = _make_service(database_service=db)

        service._phase_alignment(
            book,
            "abs-123",
            "Test",
            Path("/tmp/t.epub"),
            None,
            "STORYTELLER_NATIVE",
            "text",
            [],
            Mock(),
        )

        assert book.status == "active"

    def test_no_transcript_raises(self):
        """No transcript with non-Storyteller source raises."""
        service = _make_service()
        with pytest.raises(Exception, match="Failed to generate transcript"):
            service._phase_alignment(
                _make_book(),
                "abs-123",
                "Test",
                Path("/tmp/t.epub"),
                None,
                "WHISPER",
                "text",
                [],
                Mock(),
            )

    def test_no_alignment_service_raises(self):
        """Missing alignment_service raises."""
        service = _make_service(alignment_service=None)
        with pytest.raises(Exception, match="alignment_service not available"):
            service._phase_alignment(
                _make_book(),
                "abs-123",
                "Test",
                Path("/tmp/t.epub"),
                "transcript",
                "WHISPER",
                "text",
                [],
                Mock(),
            )

    def test_alignment_failure_raises(self):
        """alignment_service.align_and_store returning False raises."""
        alignment = Mock()
        alignment.align_and_store.return_value = False
        service = _make_service(alignment_service=alignment)

        with pytest.raises(Exception, match="Alignment failed"):
            service._phase_alignment(
                _make_book(),
                "abs-123",
                "Test",
                Path("/tmp/t.epub"),
                "transcript",
                "WHISPER",
                "text",
                [],
                Mock(),
            )

    def test_success_updates_book_and_job(self):
        """Successful alignment updates book status and clears job error."""
        book = _make_book()
        alignment = Mock()
        alignment.align_and_store.return_value = True
        db = Mock()
        job = _make_job(retry_count=2, last_error="prev error")
        db.get_latest_job.return_value = job
        service = _make_service(database_service=db, alignment_service=alignment)

        service._phase_alignment(
            book,
            "abs-123",
            "Test",
            Path("/tmp/test.epub"),
            "transcript",
            "WHISPER",
            "text",
            [],
            Mock(),
        )

        assert book.status == "active"
        assert book.ebook_filename == "test.epub"
        assert job.retry_count == 0
        assert job.last_error is None
        assert job.progress == 1.0

    def test_no_job_record_on_completion_does_not_crash(self):
        """If job record is missing at completion, it logs but does not crash."""
        book = _make_book()
        alignment = Mock()
        alignment.align_and_store.return_value = True
        db = Mock()
        db.get_latest_job.return_value = None
        service = _make_service(database_service=db, alignment_service=alignment)

        # Should not raise
        service._phase_alignment(
            book,
            "abs-123",
            "Test",
            Path("/tmp/test.epub"),
            "transcript",
            "WHISPER",
            "text",
            [],
            Mock(),
        )

        assert book.status == "active"


# ---------------------------------------------------------------------------
# _phase_transcription (SMIL except block)
# ---------------------------------------------------------------------------


class TestPhaseTranscription:
    def test_grimmory_only_book_uses_exact_authenticated_audio_source(self):
        book = _make_book(abs_id=None, grimmory_audio_source_id="default:10:42")
        abs_client = Mock()
        grimmory_client = Mock()
        audio_files = [{"stream_url": "https://grimmory/audio", "headers": {"Authorization": "Bearer token"}}]
        grimmory_client.get_audio_files.return_value = audio_files
        ebook_parser = Mock()
        ebook_parser.extract_text_and_map.return_value = ("book text", {})
        transcriber = Mock()
        transcriber.transcribe_from_smil.return_value = None
        transcriber.process_audio.return_value = "whisper transcript"
        service = _make_service(
            abs_client=abs_client,
            grimmory_client=grimmory_client,
            ebook_parser=ebook_parser,
            transcriber=transcriber,
        )

        raw, source, _, _ = service._phase_transcription(book, None, "Test", Path("/tmp/t.epub"), None, Mock())

        assert (raw, source) == ("whisper transcript", "WHISPER")
        grimmory_client.get_audio_files.assert_called_once_with("default:10:42")
        abs_client.get_audio_files.assert_not_called()
        transcriber.process_audio.assert_called_once_with(
            "default:10:42",
            audio_files,
            full_book_text="book text",
            progress_callback=transcriber.process_audio.call_args.kwargs["progress_callback"],
        )

    def test_smil_failure_falls_back_to_whisper(self):
        """SMIL extraction exception falls through to Whisper."""
        book = _make_book(storyteller_uuid=None)
        abs_client = Mock()
        abs_client.get_audio_files.return_value = ["file.mp3"]
        ebook_parser = Mock()
        ebook_parser.extract_text_and_map.return_value = ("book text", {})
        transcriber = Mock()
        transcriber.transcribe_from_smil.side_effect = RuntimeError("bad smil")
        transcriber.process_audio.return_value = "whisper transcript"

        service = _make_service(
            abs_client=abs_client,
            ebook_parser=ebook_parser,
            transcriber=transcriber,
            storyteller_client=None,
        )

        with patch.dict("os.environ", {"STORYTELLER_FORCE_MODE": "false"}):
            raw, source, text, chapters = service._phase_transcription(
                book, "abs-123", "Test", Path("/tmp/t.epub"), {"media": {"chapters": []}}, Mock()
            )

        assert source == "WHISPER"
        assert raw == "whisper transcript"
