"""
Unified SQLAlchemy database service for PageKeeper.
Facade that delegates to domain-specific repositories.
"""

import json
import logging
from contextlib import contextmanager
from pathlib import Path

from .book_repository import BookRepository
from .integration_repository import IntegrationRepository
from .kosync_repository import KoSyncRepository
from .models import (
    Base,
    DatabaseManager,
)
from .reading_repository import VALID_JOURNAL_EVENTS, ReadingRepository
from .settings_repository import SettingsRepository
from .suggestion_repository import SuggestionRepository

logger = logging.getLogger(__name__)


class DatabaseService:
    """
    Unified database service providing direct model operations.

    Delegates to domain-specific repositories while maintaining a single
    public interface for backward compatibility.
    """

    VALID_JOURNAL_EVENTS = VALID_JOURNAL_EVENTS

    def __init__(self, db_path: str):
        import os
        self.db_path = Path(os.path.abspath(db_path))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_manager = DatabaseManager(str(self.db_path))

        # Run Alembic migrations to ensure schema is up to date
        self._run_alembic_migrations()

        # Ensure all tables exist (covers new models not yet in migrations)
        Base.metadata.create_all(self.db_manager.engine)

        # Safety net: add any model columns missing from existing tables
        self._ensure_model_columns()

        # Initialize domain repositories
        self._settings = SettingsRepository(self.db_manager)
        self._books = BookRepository(self.db_manager)
        self._kosync = KoSyncRepository(self.db_manager)
        self._reading = ReadingRepository(self.db_manager)
        self._suggestions = SuggestionRepository(self.db_manager)
        self._integrations = IntegrationRepository(self.db_manager)

        # One-time data cleanup
        self._cleanup_bookfusion_md_titles()
        self._normalize_dismissed_suggestions()

    def _run_alembic_migrations(self):
        """Run Alembic migrations to bring the database schema up to date."""
        try:
            from alembic.config import Config
            from alembic.runtime.migration import MigrationContext

            from alembic import command

            alembic_dir = Path(__file__).parent.parent.parent / "alembic"
            alembic_ini = alembic_dir.parent / "alembic.ini"

            if not alembic_ini.exists():
                logger.debug("alembic.ini not found, skipping migrations")
                return

            alembic_cfg = Config(str(alembic_ini))
            alembic_cfg.set_main_option("script_location", str(alembic_dir))
            alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")

            # Check current revision
            from sqlalchemy import create_engine
            from sqlalchemy import inspect as sa_inspect
            engine = create_engine(f"sqlite:///{self.db_path}")

            try:
                inspector = sa_inspect(engine)
                tables = inspector.get_table_names()

                if tables and 'alembic_version' not in tables:
                    # Legacy database without Alembic — stamp it at head
                    logger.info("Legacy database detected — stamping at current Alembic head")
                    command.stamp(alembic_cfg, "head")
                else:
                    # Normal migration path
                    with engine.connect() as conn:
                        context = MigrationContext.configure(conn)
                        current_rev = context.get_current_revision()

                    if current_rev is None and not tables:
                        # Fresh database — will be created by create_all, then stamp
                        Base.metadata.create_all(engine)
                        command.stamp(alembic_cfg, "head")
                    else:
                        command.upgrade(alembic_cfg, "head")

                logger.debug("Alembic migrations completed successfully")
            finally:
                engine.dispose()

        except Exception as e:
            logger.warning(f"Alembic migration failed (non-fatal): {e}")

    def _ensure_model_columns(self):
        """Safety net: add any model columns missing from existing tables."""
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text
        try:
            inspector = sa_inspect(self.db_manager.engine)
            for table_name, model in Base.metadata.tables.items():
                if table_name not in inspector.get_table_names():
                    continue
                existing_cols = {c['name'] for c in inspector.get_columns(table_name)}
                for col in model.columns:
                    if col.name not in existing_cols:
                        col_type = col.type.compile(self.db_manager.engine.dialect)
                        default_clause = ""
                        if col.default is not None:
                            default_val = col.default.arg
                            if callable(default_val):
                                try:
                                    default_val = default_val()
                                except TypeError:
                                    try:
                                        default_val = default_val(None)
                                    except Exception:
                                        default_val = None
                            if isinstance(default_val, bool):
                                default_clause = f" DEFAULT {'TRUE' if default_val else 'FALSE'}"
                            elif isinstance(default_val, str):
                                escaped = default_val.replace("'", "''")
                                default_clause = f" DEFAULT '{escaped}'"
                            elif isinstance(default_val, (int, float)):
                                default_clause = f" DEFAULT {default_val}"

                        alter = f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}{default_clause}"
                        with self.db_manager.engine.connect() as conn:
                            conn.execute(text(alter))
                            conn.commit()
                        logger.info(f"Added missing column: {table_name}.{col.name}")
        except Exception as e:
            logger.warning(f"Column check failed (non-fatal): {e}")

    def _cleanup_bookfusion_md_titles(self):
        """One-time cleanup: strip .md suffix from BookFusion titles."""
        try:
            from .models import BookfusionBook
            with self.get_session() as session:
                dirty = session.query(BookfusionBook).filter(
                    BookfusionBook.title.like('%.md')
                ).all()
                for b in dirty:
                    stripped = b.title[:-3].strip()
                    b.title = stripped if stripped else b.title
                if dirty:
                    logger.info(f"Cleaned {len(dirty)} BookFusion .md titles")
        except Exception as e:
            logger.debug(f"BookFusion title cleanup skipped: {e}")

    @contextmanager
    def get_session(self):
        """Context manager for database sessions with automatic commit/rollback."""
        session = self.db_manager.get_session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            session.close()

    # ── Settings (delegates to SettingsRepository) ──

    def get_setting(self, key, default=None):
        return self._settings.get_setting(key, default)

    def set_setting(self, key, value):
        return self._settings.set_setting(key, value)

    def get_all_settings(self):
        return self._settings.get_all_settings()

    def delete_setting(self, key):
        return self._settings.delete_setting(key)

    # ── Books, States, Jobs (delegates to BookRepository) ──

    def get_book(self, abs_id):
        return self._books.get_book(abs_id)

    def get_book_by_kosync_id(self, kosync_id):
        return self._books.get_book_by_kosync_id(kosync_id)

    def get_all_books(self):
        return self._books.get_all_books()

    def get_books_by_status(self, status):
        return self._books.get_books_by_status(status)

    def get_book_by_ebook_filename(self, filename):
        return self._books.get_book_by_ebook_filename(filename)

    def create_book(self, book):
        return self._books.create_book(book)

    def save_book(self, book):
        return self._books.save_book(book)

    def delete_book(self, abs_id):
        return self._books.delete_book(abs_id)

    def migrate_book_data(self, old_abs_id, new_abs_id):
        return self._books.migrate_book_data(old_abs_id, new_abs_id)

    def get_state(self, abs_id, client_name):
        return self._books.get_state(abs_id, client_name)

    def get_states_for_book(self, abs_id):
        return self._books.get_states_for_book(abs_id)

    def get_all_states(self):
        return self._books.get_all_states()

    def save_state(self, state):
        return self._books.save_state(state)

    def delete_states_for_book(self, abs_id):
        return self._books.delete_states_for_book(abs_id)

    def get_latest_job(self, abs_id):
        return self._books.get_latest_job(abs_id)

    def get_jobs_for_book(self, abs_id):
        return self._books.get_jobs_for_book(abs_id)

    def get_all_jobs(self):
        return self._books.get_all_jobs()

    def save_job(self, job):
        return self._books.save_job(job)

    def update_latest_job(self, abs_id, **kwargs):
        return self._books.update_latest_job(abs_id, **kwargs)

    def delete_jobs_for_book(self, abs_id):
        return self._books.delete_jobs_for_book(abs_id)

    def get_books_with_recent_activity(self, limit=10):
        return self._books.get_books_with_recent_activity(limit)

    def get_failed_jobs(self, limit=20):
        return self._books.get_failed_jobs(limit)

    def get_statistics(self):
        return self._books.get_statistics()

    # ── KoSync (delegates to KoSyncRepository) ──

    def get_kosync_document(self, document_hash):
        return self._kosync.get_kosync_document(document_hash)

    def save_kosync_document(self, doc):
        return self._kosync.save_kosync_document(doc)

    def get_all_kosync_documents(self):
        return self._kosync.get_all_kosync_documents()

    def get_unlinked_kosync_documents(self):
        return self._kosync.get_unlinked_kosync_documents()

    def get_linked_kosync_documents(self):
        return self._kosync.get_linked_kosync_documents()

    def link_kosync_document(self, document_hash, abs_id):
        return self._kosync.link_kosync_document(document_hash, abs_id)

    def unlink_kosync_document(self, document_hash):
        return self._kosync.unlink_kosync_document(document_hash)

    def delete_kosync_document(self, document_hash):
        return self._kosync.delete_kosync_document(document_hash)

    def get_kosync_document_by_linked_book(self, abs_id):
        return self._kosync.get_kosync_document_by_linked_book(abs_id)

    def get_kosync_documents_for_book(self, abs_id):
        return self._kosync.get_kosync_documents_for_book(abs_id)

    def get_kosync_doc_by_filename(self, filename):
        return self._kosync.get_kosync_doc_by_filename(filename)

    def get_kosync_doc_by_booklore_id(self, booklore_id):
        return self._kosync.get_kosync_doc_by_booklore_id(booklore_id)

    # ── Suggestions (delegates to SuggestionRepository) ──

    def get_pending_suggestion(self, source_id):
        return self._suggestions.get_pending_suggestion(source_id)

    def get_suggestion(self, source_id):
        return self._suggestions.get_suggestion(source_id)

    def suggestion_exists(self, source_id):
        return self._suggestions.suggestion_exists(source_id)

    def is_suggestion_ignored(self, source_id):
        return self._suggestions.is_suggestion_ignored(source_id)

    def save_pending_suggestion(self, suggestion):
        return self._suggestions.save_pending_suggestion(suggestion)

    def is_hash_linked_to_device(self, doc_hash):
        return self._suggestions.is_hash_linked_to_device(doc_hash)

    def get_all_pending_suggestions(self):
        return self._suggestions.get_all_pending_suggestions()

    def get_all_actionable_suggestions(self):
        return self._suggestions.get_all_actionable_suggestions()

    def get_hidden_suggestions(self):
        return self._suggestions.get_hidden_suggestions()

    def delete_pending_suggestion(self, source_id):
        return self._suggestions.delete_pending_suggestion(source_id)

    def resolve_suggestion(self, source_id):
        return self._suggestions.resolve_suggestion(source_id)

    def hide_suggestion(self, source_id):
        return self._suggestions.hide_suggestion(source_id)

    def unhide_suggestion(self, source_id):
        return self._suggestions.unhide_suggestion(source_id)

    def ignore_suggestion(self, source_id):
        return self._suggestions.ignore_suggestion(source_id)

    def clear_stale_suggestions(self):
        return self._suggestions.clear_stale_suggestions()

    def _normalize_dismissed_suggestions(self):
        try:
            updated = self._suggestions.normalize_dismissed_suggestions()
            if updated:
                logger.info(f"Normalized {updated} dismissed suggestions to hidden")
        except Exception as e:
            logger.debug(f"Suggestion status normalization skipped: {e}")

    # ── Reading Tracker (delegates to ReadingRepository) ──

    def update_book_reading_fields(self, abs_id, **kwargs):
        return self._reading.update_book_reading_fields(abs_id, **kwargs)

    def get_reading_journals(self, abs_id):
        return self._reading.get_reading_journals(abs_id)

    def get_reading_journal(self, journal_id):
        return self._reading.get_reading_journal(journal_id)

    def add_reading_journal(self, abs_id, event, entry=None, percentage=None, created_at=None):
        return self._reading.add_reading_journal(abs_id, event, entry, percentage, created_at)

    def update_reading_journal(self, journal_id, *, entry=None):
        return self._reading.update_reading_journal(journal_id, entry=entry)

    def cleanup_bookfusion_import_notes(self, abs_id=None):
        return self._reading.cleanup_bookfusion_import_notes(abs_id)

    def delete_reading_journal(self, journal_id):
        return self._reading.delete_reading_journal(journal_id)

    def get_reading_goal(self, year):
        return self._reading.get_reading_goal(year)

    def save_reading_goal(self, year, target_books):
        return self._reading.save_reading_goal(year, target_books)

    def get_reading_stats(self, year):
        return self._reading.get_reading_stats(year)

    # ── Integrations: Hardcover (delegates to IntegrationRepository) ──

    def get_hardcover_details(self, abs_id):
        return self._integrations.get_hardcover_details(abs_id)

    def save_hardcover_details(self, details):
        return self._integrations.save_hardcover_details(details)

    def delete_hardcover_details(self, abs_id):
        return self._integrations.delete_hardcover_details(abs_id)

    def get_all_hardcover_details(self):
        return self._integrations.get_all_hardcover_details()

    # ── Integrations: Booklore (delegates to IntegrationRepository) ──

    def get_booklore_book(self, filename):
        return self._integrations.get_booklore_book(filename)

    def get_all_booklore_books(self):
        return self._integrations.get_all_booklore_books()

    def save_booklore_book(self, booklore_book):
        return self._integrations.save_booklore_book(booklore_book)

    def delete_booklore_book(self, filename):
        return self._integrations.delete_booklore_book(filename)

    # ── Integrations: BookFusion (delegates to IntegrationRepository) ──

    def save_bookfusion_highlights(self, highlights):
        return self._integrations.save_bookfusion_highlights(highlights)

    def get_bookfusion_highlights(self):
        return self._integrations.get_bookfusion_highlights()

    def get_unmatched_bookfusion_highlights(self):
        return self._integrations.get_unmatched_bookfusion_highlights()

    def link_bookfusion_highlight(self, highlight_id, abs_id):
        return self._integrations.link_bookfusion_highlight(highlight_id, abs_id)

    def link_bookfusion_book(self, bookfusion_book_id, abs_id):
        return self._integrations.link_bookfusion_book(bookfusion_book_id, abs_id)

    def get_bookfusion_highlights_for_book(self, abs_id):
        return self._integrations.get_bookfusion_highlights_for_book(abs_id)

    def get_bookfusion_sync_cursor(self):
        return self._settings.get_setting('BOOKFUSION_SYNC_CURSOR')

    def set_bookfusion_sync_cursor(self, cursor):
        return self._settings.set_setting('BOOKFUSION_SYNC_CURSOR', cursor)

    def save_bookfusion_books(self, books):
        return self._integrations.save_bookfusion_books(books)

    def get_bookfusion_books(self):
        return self._integrations.get_bookfusion_books()

    def is_bookfusion_linked(self, abs_id):
        return self._integrations.is_bookfusion_linked(abs_id)

    def set_bookfusion_books_hidden(self, bookfusion_ids, hidden):
        return self._integrations.set_bookfusion_books_hidden(bookfusion_ids, hidden)

    def set_bookfusion_book_match(self, bookfusion_id, abs_id):
        return self._integrations.set_bookfusion_book_match(bookfusion_id, abs_id)

    def get_bookfusion_book(self, bookfusion_id):
        return self._integrations.get_bookfusion_book(bookfusion_id)

    def get_bookfusion_book_by_abs_id(self, abs_id):
        return self._integrations.get_bookfusion_book_by_abs_id(abs_id)

    def unlink_bookfusion_by_abs_id(self, abs_id):
        return self._integrations.unlink_bookfusion_by_abs_id(abs_id)

    def get_bookfusion_highlight_date_range(self, bookfusion_book_ids):
        return self._integrations.get_bookfusion_highlight_date_range(bookfusion_book_ids)

    def get_bookfusion_linked_abs_ids(self):
        return self._integrations.get_bookfusion_linked_abs_ids()

    def get_bookfusion_highlight_counts(self):
        return self._integrations.get_bookfusion_highlight_counts()


class DatabaseMigrator:
    """Handles migration from JSON files to SQLAlchemy database."""

    def __init__(self, db_service: DatabaseService, json_db_path: str, json_state_path: str):
        self.db_service = db_service
        self.json_db_path = Path(json_db_path)
        self.json_state_path = Path(json_state_path)

    def migrate(self):
        """Perform migration from JSON to SQLAlchemy database."""
        logger.info("Starting migration from JSON to SQLAlchemy database")

        # Migrate mappings/books
        if self.json_db_path.exists():
            with open(self.json_db_path, encoding='utf-8') as f:
                data = json.load(f)

            mappings = data.get('mappings', [])
            if isinstance(mappings, dict):
                mappings = list(mappings.values())

            self._migrate_books(mappings)

        # Migrate states
        if self.json_state_path.exists():
            with open(self.json_state_path, encoding='utf-8') as f:
                state_data = json.load(f)
            self._migrate_states(state_data)

        # Rename old files
        for path in [self.json_db_path, self.json_state_path]:
            if path.exists():
                backup = path.with_suffix(path.suffix + '.migrated')
                path.rename(backup)
                logger.info(f"Renamed {path} to {backup}")

        logger.info("Migration completed successfully")

    def _migrate_books(self, mappings_list):
        """Convert JSON mappings to Book + Job + HardcoverDetails records."""
        from .models import Book, HardcoverDetails, Job
        for m in mappings_list:
            abs_id = m.get('abs_id')
            if not abs_id:
                continue

            book = Book(
                abs_id=abs_id,
                abs_title=m.get('abs_title'),
                ebook_filename=m.get('ebook_filename'),
                original_ebook_filename=m.get('original_ebook_filename'),
                kosync_doc_id=m.get('kosync_doc_id'),
                transcript_file=m.get('transcript_file'),
                status=m.get('status', 'active'),
                duration=m.get('duration'),
                sync_mode=m.get('sync_mode', 'audiobook'),
                storyteller_uuid=m.get('storyteller_uuid'),
                abs_ebook_item_id=m.get('abs_ebook_item_id'),
            )
            self.db_service.save_book(book)

            # Migrate job data if present
            if m.get('last_sync_attempt') or m.get('retry_count'):
                job = Job(
                    abs_id=abs_id,
                    last_attempt=m.get('last_sync_attempt'),
                    retry_count=m.get('retry_count', 0),
                    last_error=m.get('last_error'),
                )
                self.db_service.save_job(job)

            # Migrate hardcover details if present
            hc = {}
            for key in ['hardcover_book_id', 'hardcover_slug', 'hardcover_edition_id',
                       'hardcover_pages', 'hardcover_audio_seconds', 'isbn', 'asin', 'matched_by']:
                if m.get(key):
                    hc[key] = m[key]

            if hc:
                details = HardcoverDetails(abs_id=abs_id, **hc)
                self.db_service.save_hardcover_details(details)

            logger.debug(f"Migrated book: {abs_id}")

    def _migrate_states(self, state_dict):
        """Migrate state data to State models."""
        from .models import State
        for abs_id, data in state_dict.items():
            last_updated = data.get('last_updated')

            if 'kosync_pct' in data:
                self.db_service.save_state(State(
                    abs_id=abs_id, client_name='kosync', last_updated=last_updated,
                    percentage=data['kosync_pct'], xpath=data.get('kosync_xpath'),
                ))

            if 'abs_pct' in data:
                self.db_service.save_state(State(
                    abs_id=abs_id, client_name='abs', last_updated=last_updated,
                    percentage=data['abs_pct'], timestamp=data.get('abs_ts'),
                ))

            if 'absebook_pct' in data:
                self.db_service.save_state(State(
                    abs_id=abs_id, client_name='absebook', last_updated=last_updated,
                    percentage=data['absebook_pct'], cfi=data.get('absebook_cfi'),
                ))

            if 'storyteller_pct' in data:
                self.db_service.save_state(State(
                    abs_id=abs_id, client_name='storyteller', last_updated=last_updated,
                    percentage=data['storyteller_pct'], xpath=data.get('storyteller_xpath'),
                    cfi=data.get('storyteller_cfi'),
                ))

            if 'booklore_pct' in data:
                self.db_service.save_state(State(
                    abs_id=abs_id, client_name='booklore', last_updated=last_updated,
                    percentage=data['booklore_pct'], xpath=data.get('booklore_xpath'),
                    cfi=data.get('booklore_cfi'),
                ))

    def should_migrate(self) -> bool:
        """Check if migration is needed (JSON files exist but no data in SQLAlchemy)."""
        try:
            with self.db_service.get_session() as session:
                from sqlalchemy import text
                count = session.execute(text("SELECT count(*) FROM books")).scalar()
                if count > 0:
                    return False
        except Exception as e:
            logger.debug(f"Could not check books table: {e}")

        return self.json_db_path.exists() or self.json_state_path.exists()
