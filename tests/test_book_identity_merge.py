"""Regression coverage for preserving canonical Book rows during identity merges."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.db.database_service import DatabaseService
from src.db.models import (
    Book,
    BookAlignment,
    HardcoverDetails,
    Job,
    KosyncDocument,
    ReadingJournal,
    State,
    StorytellerSubmission,
)

sys.modules.setdefault("nh3", SimpleNamespace(clean=lambda value, tags=None, attributes=None: value))


class _RouteContainer:
    def __init__(self, db, temp_dir):
        self._db = db
        self._temp_dir = Path(temp_dir)
        self._abs_client = Mock()
        self._abs_client.is_configured.return_value = True
        self._abs_client.get_all_audiobooks.return_value = [
            {
                "id": "abs-route-audiobook",
                "media": {
                    "metadata": {"title": "Route ABS Audiobook", "authorName": "Route Author"},
                    "duration": 5400,
                },
            }
        ]
        self._abs_client.add_to_collection.return_value = True

        self._sync_manager = Mock()
        self._sync_manager.abs_client = self._abs_client
        self._sync_manager.get_audiobook_title.side_effect = lambda item: item["media"]["metadata"]["title"]
        self._sync_manager.get_duration.side_effect = lambda item: item["media"]["duration"]

        self._grimmory_client = Mock()
        self._grimmory_client.is_configured.return_value = False
        self._storyteller_client = Mock()
        self._storyteller_client.is_configured.return_value = False
        self._bookfusion_client = Mock()
        self._bookfusion_client.is_configured.return_value = False
        self._hardcover_service = Mock()
        self._hardcover_service.is_configured.return_value = False
        self._hardcover_sync_client = Mock()
        self._reading_date_service = Mock()
        self._reading_date_service.pull_reading_dates.return_value = {}
        self._ebook_parser = Mock()

    def sync_manager(self):
        return self._sync_manager

    def abs_client(self):
        return self._abs_client

    def grimmory_client(self):
        return self._grimmory_client

    def grimmory_client_group(self):
        return self._grimmory_client

    def storyteller_client(self):
        return self._storyteller_client

    def bookfusion_client(self):
        return self._bookfusion_client

    def hardcover_service(self):
        return self._hardcover_service

    def hardcover_sync_client(self):
        return self._hardcover_sync_client

    def reading_date_service(self):
        return self._reading_date_service

    def ebook_parser(self):
        return self._ebook_parser

    def database_service(self):
        return self._db

    def data_dir(self):
        return self._temp_dir

    def books_dir(self):
        return self._temp_dir

    def epub_cache_dir(self):
        return self._temp_dir / "epub_cache"

    def sync_clients(self):
        return {}


class TestBookIdentityMerge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir
        self.db = DatabaseService(str(Path(self.temp_dir) / "identity_merge.db"))

    def tearDown(self):
        if hasattr(self, "db") and hasattr(self.db, "db_manager"):
            self.db.db_manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_attaching_abs_audiobook_preserves_source_book_and_children(self):
        source = self.db.save_book(
            Book(
                abs_id="ebook-local-1",
                title="Local EPUB",
                ebook_filename="local.epub",
                kosync_doc_id="a" * 32,
                status="active",
                sync_mode="ebook_only",
                read_count=2,
            )
        )
        self.db.save_state(
            State(
                abs_id=source.abs_id,
                book_id=source.id,
                client_name="KOReader",
                percentage=0.42,
                timestamp=120,
            )
        )
        self.db.save_job(Job(abs_id=source.abs_id, book_id=source.id, last_attempt=100, progress=0.5))
        self.db.add_reading_journal(source.id, event="note", entry="keep me", abs_id=source.abs_id)
        self.db.save_storyteller_submission(
            StorytellerSubmission(
                abs_id=source.abs_id,
                book_id=source.id,
                status="processing",
                storyteller_uuid="storyteller-source",
            )
        )
        self.db.save_kosync_document(
            KosyncDocument(
                document_hash=source.kosync_doc_id,
                progress="/body/source",
                percentage=0.42,
                linked_abs_id=source.abs_id,
                linked_book_id=source.id,
                filename=source.ebook_filename,
            )
        )

        target = self.db.save_book(
            Book(
                abs_id="abs-audiobook-1",
                title="ABS Audiobook",
                author="Audio Author",
                ebook_filename=source.ebook_filename,
                kosync_doc_id=source.kosync_doc_id,
                status="active",
                duration=3600,
                sync_mode="audiobook",
                read_count=source.read_count,
            )
        )
        self.db.save_state(
            State(
                abs_id=target.abs_id,
                book_id=target.id,
                client_name="KOReader",
                percentage=0.99,
                timestamp=999,
            )
        )
        self.db.save_state(
            State(abs_id=target.abs_id, book_id=target.id, client_name="Audiobookshelf", percentage=0.05)
        )

        self.db.migrate_book_data(source.abs_id, target.abs_id)

        with self.db.get_session() as session:
            books = session.query(Book).all()
            self.assertEqual(len(books), 1)

            survivor = books[0]
            self.assertEqual(survivor.id, source.id)
            self.assertEqual(survivor.abs_id, "abs-audiobook-1")
            self.assertEqual(survivor.title, "ABS Audiobook")
            self.assertEqual(survivor.duration, 3600)
            self.assertEqual(survivor.sync_mode, "audiobook")

            states = session.query(State).order_by(State.client_name).all()
            self.assertEqual([state.client_name for state in states], ["Audiobookshelf", "KOReader"])
            self.assertTrue(all(state.book_id == source.id for state in states))
            self.assertTrue(all(state.abs_id == survivor.abs_id for state in states))
            self.assertEqual(next(state for state in states if state.client_name == "KOReader").percentage, 0.42)

            job = session.query(Job).one()
            self.assertEqual(job.book_id, source.id)
            self.assertEqual(job.abs_id, survivor.abs_id)

            journal = session.query(ReadingJournal).one()
            self.assertEqual(journal.book_id, source.id)
            self.assertEqual(journal.abs_id, survivor.abs_id)

            submission = session.query(StorytellerSubmission).one()
            self.assertEqual(submission.book_id, source.id)
            self.assertEqual(submission.abs_id, survivor.abs_id)

            kosync_doc = session.query(KosyncDocument).filter_by(document_hash=source.kosync_doc_id).one()
            self.assertEqual(kosync_doc.linked_book_id, source.id)
            self.assertEqual(kosync_doc.linked_abs_id, survivor.abs_id)

    def test_merge_preserves_alignment_and_activity_when_target_is_empty(self):
        aligned_source = Book(
            abs_id="ebook-aligned-1",
            title="Aligned EPUB",
            ebook_filename="aligned.epub",
            kosync_doc_id="c" * 32,
            status="active",
            sync_mode="ebook_only",
            transcript_file="DB_MANAGED",
        )
        aligned_source.activity_flag = True
        source = self.db.save_book(aligned_source)

        # A freshly mapped audiobook target never carries these hints.
        target = self.db.save_book(
            Book(
                abs_id="abs-audiobook-aligned-1",
                title="ABS Aligned Audiobook",
                author="Aligned Author",
                ebook_filename=source.ebook_filename,
                kosync_doc_id=source.kosync_doc_id,
                status="active",
                duration=3600,
                sync_mode="audiobook",
                transcript_file=None,
            )
        )

        self.db.migrate_book_data(source.abs_id, target.abs_id)

        with self.db.get_session() as session:
            books = session.query(Book).all()
            self.assertEqual(len(books), 1)

            survivor = books[0]
            self.assertEqual(survivor.id, source.id)
            # Alignment routing + paused-book UX hint survive the merge.
            self.assertEqual(survivor.transcript_file, "DB_MANAGED")
            self.assertTrue(survivor.activity_flag)
            # Legitimately populated target fields still flow through.
            self.assertEqual(survivor.abs_id, "abs-audiobook-aligned-1")
            self.assertEqual(survivor.title, "ABS Aligned Audiobook")
            self.assertEqual(survivor.author, "Aligned Author")
            self.assertEqual(survivor.duration, 3600)
            self.assertEqual(survivor.sync_mode, "audiobook")

    def test_attach_audiobook_route_merges_when_link_book_id_is_numeric(self):
        import src.db.migration_utils

        original_initialize_database = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = lambda data_dir: self.db
        try:
            from src.web_server import create_app

            source = self.db.save_book(
                Book(
                    abs_id="route-ebook-1",
                    title="Route EPUB",
                    ebook_filename="route.epub",
                    kosync_doc_id="b" * 32,
                    status="active",
                    sync_mode="ebook_only",
                )
            )
            self.db.save_state(
                State(abs_id=source.abs_id, book_id=source.id, client_name="KOReader", percentage=0.25)
            )

            app, _ = create_app(test_container=_RouteContainer(self.db, self.temp_dir))
            app.config["TESTING"] = True

            response = app.test_client().post(
                "/match",
                data={
                    "action": "attach_audiobook",
                    "link_book_id": str(source.id),
                    "audiobook_id": "abs-route-audiobook",
                },
            )

            self.assertEqual(response.status_code, 302)
            with self.db.get_session() as session:
                books = session.query(Book).all()
                self.assertEqual(len(books), 1)
                survivor = books[0]
                self.assertEqual(survivor.id, source.id)
                self.assertEqual(survivor.abs_id, "abs-route-audiobook")
                self.assertEqual(survivor.title, "Route ABS Audiobook")

                state = session.query(State).one()
                self.assertEqual(state.book_id, source.id)
                self.assertEqual(state.abs_id, "abs-route-audiobook")
        finally:
            src.db.migration_utils.initialize_database = original_initialize_database

    def test_merge_backfills_unique_children_when_both_books_have_rows(self):
        source = self.db.save_book(
            Book(
                abs_id="ebook-both-children",
                title="Both Children EPUB",
                ebook_filename="both.epub",
                kosync_doc_id="d" * 32,
                status="active",
                sync_mode="ebook_only",
            )
        )
        target = self.db.save_book(
            Book(
                abs_id="abs-both-children",
                title="Both Children Audiobook",
                author="Both Author",
                ebook_filename=source.ebook_filename,
                kosync_doc_id=source.kosync_doc_id,
                status="active",
                duration=3600,
                sync_mode="audiobook",
            )
        )

        # Canonical Hardcover row: identity set, isbn left NULL.
        self.db.save_hardcover_details(
            HardcoverDetails(
                book_id=source.id,
                abs_id=source.abs_id,
                hardcover_book_id="canonical-hc",
                isbn=None,
            )
        )
        # Target Hardcover row: conflicting identity (must lose) + isbn to backfill.
        self.db.save_hardcover_details(
            HardcoverDetails(
                book_id=target.id,
                abs_id=target.abs_id,
                hardcover_book_id="target-hc",
                isbn="9781234567890",
            )
        )

        # Canonical alignment: map present, source NULL. Target: different map + source.
        with self.db.get_session() as session:
            session.add(
                BookAlignment(
                    book_id=source.id,
                    abs_id=source.abs_id,
                    alignment_map_json='{"canonical": true}',
                    source=None,
                )
            )
            session.add(
                BookAlignment(
                    book_id=target.id,
                    abs_id=target.abs_id,
                    alignment_map_json='{"target": true}',
                    source="storyteller",
                )
            )

        with self.db.get_session() as session:
            original_alignment_ts = (
                session.query(BookAlignment).filter(BookAlignment.book_id == source.id).one().last_updated
            )

        self.db.migrate_book_data(source.abs_id, target.abs_id)

        with self.db.get_session() as session:
            self.assertEqual(len(session.query(Book).all()), 1)

            hc_rows = session.query(HardcoverDetails).all()
            self.assertEqual(len(hc_rows), 1)
            hc = hc_rows[0]
            self.assertEqual(hc.book_id, source.id)
            self.assertEqual(hc.abs_id, target.abs_id)
            # Canonical identity wins on conflict; null isbn backfilled from target.
            self.assertEqual(hc.hardcover_book_id, "canonical-hc")
            self.assertEqual(hc.isbn, "9781234567890")
            self.assertEqual(
                session.query(HardcoverDetails).filter(HardcoverDetails.book_id == target.id).count(), 0
            )

            align_rows = session.query(BookAlignment).all()
            self.assertEqual(len(align_rows), 1)
            align = align_rows[0]
            self.assertEqual(align.book_id, source.id)
            self.assertEqual(align.abs_id, target.abs_id)
            # NOT-NULL canonical map wins; nullable source backfilled from target.
            self.assertEqual(align.alignment_map_json, '{"canonical": true}')
            self.assertEqual(align.source, "storyteller")
            # last_updated is an onupdate column: the merge touches the row, so it
            # advances to merge time rather than being copied from the target.
            self.assertGreaterEqual(align.last_updated, original_alignment_ts)
            self.assertEqual(
                session.query(BookAlignment).filter(BookAlignment.book_id == target.id).count(), 0
            )


if __name__ == "__main__":
    unittest.main()
