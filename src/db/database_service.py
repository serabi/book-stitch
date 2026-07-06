# pyright: reportMissingImports=false

"""
Unified SQLAlchemy database service for PageKeeper.
Facade that delegates to domain-specific repositories.
"""

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from .book_repository import BookRepository
from .bookfusion_repository import BookFusionRepository
from .detected_repository import DetectedRepository
from .grimmory_repository import GrimmoryRepository
from .hardcover_repository import HardcoverRepository
from .kosync_repository import KoSyncRepository
from .models import (
    Base,
    DatabaseManager,
    KosyncDocument,
)
from .reading_repository import VALID_JOURNAL_EVENTS, ReadingRepository
from .settings_repository import SettingsRepository
from .storyteller_repository import StorytellerRepository
from .suggestion_repository import SuggestionRepository
from .tbr_repository import TbrRepository

logger = logging.getLogger(__name__)

LEGACY_BASELINE_REVISION = "76886bc89d6e"

# Tables and columns created by the initial Alembic revision (76886bc89d6e).
# Later migrations ALTER every one of these tables, so a database may only be
# stamped at the baseline revision when all of them are present. Extra tables
# or columns are tolerated — pre-Alembic installs evolved manually.
LEGACY_BASELINE_SCHEMA = {
    "books": {
        "abs_id",
        "abs_title",
        "ebook_filename",
        "kosync_doc_id",
        "transcript_file",
        "status",
        "duration",
    },
    "hardcover_details": {
        "abs_id",
        "hardcover_book_id",
        "hardcover_edition_id",
        "hardcover_pages",
        "isbn",
        "asin",
        "matched_by",
    },
    "states": {
        "id",
        "abs_id",
        "client_name",
        "last_updated",
        "percentage",
        "timestamp",
        "xpath",
        "cfi",
    },
    "jobs": {
        "id",
        "abs_id",
        "last_attempt",
        "retry_count",
        "last_error",
    },
}


class DatabaseSchemaError(RuntimeError):
    """Raised when the database schema cannot be safely migrated or verified.

    Startup must fail closed on this error: proceeding would risk writing to a
    half-migrated or unknown schema and corrupting user data.
    """


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

        # Run Alembic migrations; failure raises DatabaseSchemaError so
        # repositories, cleanup, sync, and web writes never touch a
        # half-migrated schema.
        self._run_alembic_migrations()

        # Ensure all tables exist (covers new models not yet in migrations)
        Base.metadata.create_all(self.db_manager.engine)

        # Fail closed if existing tables are missing model columns
        self._verify_model_columns()

        # Initialize domain repositories
        self._settings = SettingsRepository(self.db_manager)
        self._books = BookRepository(self.db_manager)
        self._kosync = KoSyncRepository(self.db_manager)
        self._reading = ReadingRepository(self.db_manager)
        self._suggestions = SuggestionRepository(self.db_manager)
        self._detected = DetectedRepository(self.db_manager)
        self._hardcover = HardcoverRepository(self.db_manager)
        self._storyteller = StorytellerRepository(self.db_manager)
        self._bookfusion = BookFusionRepository(self.db_manager)
        self._grimmory = GrimmoryRepository(self.db_manager)
        self._tbr = TbrRepository(self.db_manager)

        # Post-startup schema health check
        self._verify_schema_health()

        # One-time data cleanup
        self._cleanup_bookfusion_md_titles()
        self._normalize_dismissed_suggestions()

    def _run_alembic_migrations(self):
        """Run Alembic migrations to bring the database schema up to date."""
        try:
            import alembic.command as alembic_command
            from alembic.config import Config
            from alembic.runtime.migration import MigrationContext

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

                if tables and "alembic_version" not in tables:
                    # Populated database with no revision history. Only a fully
                    # verified pre-Alembic baseline may be stamped and upgraded;
                    # anything else fails closed.
                    self._stamp_verified_baseline_and_upgrade(alembic_cfg, inspector)
                else:
                    # Normal migration path
                    with engine.connect() as conn:
                        context = MigrationContext.configure(conn)
                        current_rev = context.get_current_revision()

                    data_tables = [t for t in tables if t != "alembic_version"]
                    if current_rev is None and not data_tables:
                        # Fresh database — will be created by create_all, then stamp
                        Base.metadata.create_all(engine)
                        alembic_command.stamp(alembic_cfg, "head")
                    elif current_rev is None:
                        # Populated database with an empty alembic_version table,
                        # typically left behind by an interrupted direct
                        # `alembic upgrade head`. Recover only through the
                        # verified baseline path.
                        self._stamp_verified_baseline_and_upgrade(alembic_cfg, inspector)
                    else:
                        alembic_command.upgrade(alembic_cfg, "head")

                logger.debug("Alembic migrations completed successfully")
            finally:
                engine.dispose()

        except DatabaseSchemaError:
            raise
        except Exception as e:
            raise DatabaseSchemaError(
                f"Alembic migration failed — refusing to start with a possibly incomplete schema. Error: {e}"
            ) from e

    def _stamp_verified_baseline_and_upgrade(self, alembic_cfg, inspector):
        """Stamp a verified pre-Alembic database at the baseline, then upgrade.

        Pre-Alembic databases already contain the original schema, so replaying
        the initial revision would fail with "table already exists". Stamping at
        the baseline revision lets later migrations run normally. Stamping is
        only safe when every baseline table and column exists — later migrations
        ALTER them all — so any mismatch raises instead of stamping.
        """
        import alembic.command as alembic_command

        problems = self._pre_alembic_baseline_problems(inspector)
        if problems:
            raise DatabaseSchemaError(
                "Populated database has no Alembic revision and does not match "
                f"the verified pre-Alembic baseline schema: {'; '.join(problems)}. "
                "Refusing to stamp or migrate. Restore from a backup or repair "
                "the schema manually before starting PageKeeper."
            )

        logger.info(
            "Legacy database detected — stamping at initial Alembic revision %s before upgrading to head",
            LEGACY_BASELINE_REVISION,
        )
        alembic_command.stamp(alembic_cfg, LEGACY_BASELINE_REVISION)
        alembic_command.upgrade(alembic_cfg, "head")

    @staticmethod
    def _pre_alembic_baseline_problems(inspector) -> list[str]:
        """Return problems preventing a safe baseline stamp (empty = verified)."""
        tables = set(inspector.get_table_names())
        problems = []
        for table, baseline_cols in LEGACY_BASELINE_SCHEMA.items():
            if table not in tables:
                problems.append(f"missing table '{table}'")
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            missing = sorted(baseline_cols - cols)
            if missing:
                problems.append(f"table '{table}' missing columns {missing}")
        return problems

    def _verify_schema_health(self):
        """Post-startup check that critical columns exist after migrations.

        If migrations failed or were skipped, the schema may be missing key
        columns like books.id or states.book_id. Log a prominent error so
        the user (and Telegram notifications) can see it.
        """
        try:
            from sqlalchemy import inspect as sa_inspect

            inspector = sa_inspect(self.db_manager.engine)
            tables = set(inspector.get_table_names())

            checks = [
                ("books", "id"),
                ("states", "book_id"),
            ]
            missing = []
            for table, column in checks:
                if table not in tables:
                    continue
                cols = {c["name"] for c in inspector.get_columns(table)}
                if column not in cols:
                    missing.append(f"{table}.{column}")

            if missing:
                logger.error(
                    "DATABASE SCHEMA INCOMPLETE — missing critical columns: %s. "
                    "Migrations may have failed during a previous upgrade. "
                    "Check earlier log entries for Alembic errors. "
                    "Recovery: restore from backup or delete the database to start fresh.",
                    ", ".join(missing),
                )
        except Exception as e:
            logger.warning("Schema health check could not run: %s", e)

    def _verify_model_columns(self):
        """Fail closed if any existing table is missing model columns.

        The previous behavior patched missing columns in with raw ALTER TABLE,
        which produced columns without PK/FK/index/nullability semantics. A
        mismatch here means a migration is missing, so refuse to start rather
        than write to a schema the ORM's assumptions do not hold for.
        """
        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(self.db_manager.engine)
        existing_tables = set(inspector.get_table_names())
        missing = []
        for table_name, model in Base.metadata.tables.items():
            if table_name not in existing_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
            missing.extend(f"{table_name}.{col.name}" for col in model.columns if col.name not in existing_cols)

        if missing:
            raise DatabaseSchemaError(
                f"Database schema is missing model columns after migrations: {', '.join(sorted(missing))}. "
                "A migration is missing or failed; refusing to start. Restore "
                "from a backup or repair the schema manually before starting PageKeeper."
            )

    def _cleanup_bookfusion_md_titles(self):
        """One-time cleanup: strip .md suffix from BookFusion titles."""
        try:
            from .models import BookfusionBook

            with self.get_session() as session:
                dirty = session.query(BookfusionBook).filter(cast(Any, BookfusionBook.title).like("%.md")).all()
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

    # ── Auto-delegation to repositories ──
    # Most methods are pure passthrough to a specific repository.
    # Only methods with cross-cutting logic are defined explicitly below.
    # Everything else is resolved via __getattr__.

    _REPOS = (
        "_settings",
        "_books",
        "_kosync",
        "_reading",
        "_suggestions",
        "_detected",
        "_hardcover",
        "_storyteller",
        "_bookfusion",
        "_grimmory",
        "_tbr",
    )

    def _delegated_method_names(self):
        names = set()
        for repo_name in DatabaseService._REPOS:
            repo = object.__getattribute__(self, repo_name)
            for attr in dir(repo):
                if not attr.startswith("_"):
                    names.add(attr)
        return names

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        for repo_name in DatabaseService._REPOS:
            repo = object.__getattribute__(self, repo_name)
            method = getattr(repo, name, None)
            if method is not None:
                return method
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    def __dir__(self):
        return sorted(set(super().__dir__()) | self._delegated_method_names())

    # ── Bulk data helpers (avoid N+1 in views) ──

    def get_states_by_book(self):
        """Return all sync states grouped by book_id: dict[int, list[State]]."""
        all_states = self._books.get_all_states()
        result = {}
        for state in all_states:
            result.setdefault(state.book_id, []).append(state)
        return result

    def get_grimmory_by_filename(self, enabled_server_ids=None):
        """Return all Grimmory books grouped by lowercase filename.

        When *enabled_server_ids* is given, only books from those instances are included.
        """
        all_books = self._grimmory.get_all_grimmory_books()
        result = {}
        for bl_book in all_books:
            if bl_book.filename:
                if enabled_server_ids is not None and bl_book.server_id not in enabled_server_ids:
                    continue
                result.setdefault(bl_book.filename.lower(), []).append(bl_book)
        return result

    # ── Methods with cross-cutting logic (not pure passthrough) ──

    def save_book(self, book, is_new=False):
        result = self._books.save_book(book)
        self._ensure_book_kosync_document(result)
        if is_new and book.title:
            self._tbr.auto_link_by_title(book)
            self._bookfusion.auto_link_by_title(book)
        return result

    def _ensure_book_kosync_document(self, book):
        if not book or not book.kosync_doc_id or not book.id:
            return
        try:
            existing = self._kosync.get_kosync_document(book.kosync_doc_id)
            if existing:
                changed = False
                if not existing.linked_book_id:
                    existing.linked_book_id = book.id
                    existing.linked_abs_id = book.abs_id
                    changed = True
                if not existing.filename and book.ebook_filename:
                    existing.filename = book.ebook_filename
                    changed = True
                if changed:
                    self._kosync.save_kosync_document(existing)
                return

            self._kosync.save_kosync_document(
                KosyncDocument(
                    document_hash=book.kosync_doc_id,
                    linked_book_id=book.id,
                    linked_abs_id=book.abs_id,
                    filename=book.ebook_filename,
                )
            )
        except Exception as exc:
            logger.warning("Failed to ensure KoSync document for book %s: %s", book.id, exc)

    def save_hardcover_details(self, details):
        result = self._hardcover.save_hardcover_details(details)
        # Auto-link: if a TBR item has this hardcover_book_id, set its book_id
        if details.hardcover_book_id and details.book_id:
            try:
                hc_id = int(details.hardcover_book_id)
                tbr_item = self._tbr.find_tbr_by_hardcover_id(hc_id)
                if tbr_item and not tbr_item.book_id:
                    self._tbr.link_tbr_to_book(tbr_item.id, details.book_id)
            except (TypeError, ValueError):
                pass
        return result

    def _normalize_dismissed_suggestions(self):
        try:
            updated = self._suggestions.normalize_dismissed_suggestions()
            if updated:
                logger.info(f"Normalized {updated} dismissed suggestions to hidden")
        except Exception as e:
            logger.debug(f"Suggestion status normalization skipped: {e}")

    # ── Backward-compatible method aliases ──

    def get_bookfusion_sync_cursor(self):
        return self._settings.get_setting("BOOKFUSION_SYNC_CURSOR")

    def set_bookfusion_sync_cursor(self, cursor):
        return self._settings.set_setting("BOOKFUSION_SYNC_CURSOR", cursor)

    def find_tbr_by_book_id(self, book_id):
        return self._tbr.find_by_book_id(book_id)

    def delete_tbr_by_book_id(self, book_id):
        return self._tbr.delete_by_book_id(book_id)

    def get_unlinked_tbr_items(self):
        return self._tbr.get_unlinked_items()

    # ── TBR (To Be Read) List (delegates to TbrRepository) ──

    def get_tbr_items(self, source=None):
        return self._tbr.get_tbr_items(source)

    def get_tbr_item(self, item_id):
        return self._tbr.get_tbr_item(item_id)

    def add_tbr_item(self, title, **kwargs):
        return self._tbr.add_tbr_item(title, **kwargs)

    def update_tbr_item(self, item_id, **fields):
        return self._tbr.update_tbr_item(item_id, **fields)

    def delete_tbr_item(self, item_id):
        return self._tbr.delete_tbr_item(item_id)

    def link_tbr_to_book(self, item_id, book_id):
        return self._tbr.link_tbr_to_book(item_id, book_id)

    def find_tbr_by_hardcover_id(self, hc_book_id):
        return self._tbr.find_tbr_by_hardcover_id(hc_book_id)

    def get_tbr_count(self):
        return self._tbr.get_tbr_count()


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
            with open(self.json_db_path, encoding="utf-8") as f:
                data = json.load(f)

            mappings = data.get("mappings", [])
            if isinstance(mappings, dict):
                mappings = list(mappings.values())

            self._migrate_books(mappings)

        # Migrate states
        if self.json_state_path.exists():
            with open(self.json_state_path, encoding="utf-8") as f:
                state_data = json.load(f)
            self._migrate_states(state_data)

        # Rename old files
        for path in [self.json_db_path, self.json_state_path]:
            if path.exists():
                backup = path.with_suffix(path.suffix + ".migrated")
                path.rename(backup)
                logger.info(f"Renamed {path} to {backup}")

        logger.info("Migration completed successfully")

    def _migrate_books(self, mappings_list):
        """Convert JSON mappings to Book + Job + HardcoverDetails records."""
        from .models import Book, HardcoverDetails, Job

        for m in mappings_list:
            abs_id = m.get("abs_id")
            if not abs_id:
                continue

            book = Book(
                abs_id=abs_id,
                title=m.get("title") or m.get("abs_title"),
                ebook_filename=m.get("ebook_filename"),
                original_ebook_filename=m.get("original_ebook_filename"),
                kosync_doc_id=m.get("kosync_doc_id"),
                transcript_file=m.get("transcript_file"),
                status=m.get("status", "active"),
                duration=m.get("duration"),
                sync_mode=m.get("sync_mode", "audiobook"),
                storyteller_uuid=m.get("storyteller_uuid"),
                abs_ebook_item_id=m.get("abs_ebook_item_id"),
                ebook_item_id=m.get("abs_ebook_item_id"),
            )
            saved_book = self.db_service.save_book(book)

            # Migrate job data if present
            if m.get("last_sync_attempt") or m.get("retry_count"):
                job = Job(
                    abs_id=abs_id,
                    book_id=saved_book.id,
                    last_attempt=m.get("last_sync_attempt"),
                    retry_count=m.get("retry_count", 0),
                    last_error=m.get("last_error"),
                )
                self.db_service.save_job(job)

            # Migrate hardcover details if present
            hc = {}
            for key in [
                "hardcover_book_id",
                "hardcover_slug",
                "hardcover_edition_id",
                "hardcover_pages",
                "hardcover_audio_seconds",
                "isbn",
                "asin",
                "matched_by",
            ]:
                if m.get(key):
                    hc[key] = m[key]

            if hc:
                details = HardcoverDetails(abs_id=abs_id, book_id=saved_book.id, **hc)
                self.db_service.save_hardcover_details(details)

            logger.debug(f"Migrated book: {abs_id}")

    def _migrate_states(self, state_dict):
        """Migrate state data to State models."""
        from .models import State

        for abs_id, data in state_dict.items():
            last_updated = data.get("last_updated")

            # Look up book_id from abs_id
            book = self.db_service.get_book_by_abs_id(abs_id)
            if not book:
                logger.warning(f"Skipping state migration for unknown book: {abs_id}")
                continue
            book_id = book.id

            if "kosync_pct" in data:
                self.db_service.save_state(
                    State(
                        abs_id=abs_id,
                        book_id=book_id,
                        client_name="kosync",
                        last_updated=last_updated,
                        percentage=data["kosync_pct"],
                        xpath=data.get("kosync_xpath"),
                    )
                )

            if "abs_pct" in data:
                self.db_service.save_state(
                    State(
                        abs_id=abs_id,
                        book_id=book_id,
                        client_name="abs",
                        last_updated=last_updated,
                        percentage=data["abs_pct"],
                        timestamp=data.get("abs_ts"),
                    )
                )

            if "absebook_pct" in data:
                self.db_service.save_state(
                    State(
                        abs_id=abs_id,
                        book_id=book_id,
                        client_name="absebook",
                        last_updated=last_updated,
                        percentage=data["absebook_pct"],
                        cfi=data.get("absebook_cfi"),
                    )
                )

            if "storyteller_pct" in data:
                self.db_service.save_state(
                    State(
                        abs_id=abs_id,
                        book_id=book_id,
                        client_name="storyteller",
                        last_updated=last_updated,
                        percentage=data["storyteller_pct"],
                        xpath=data.get("storyteller_xpath"),
                        cfi=data.get("storyteller_cfi"),
                    )
                )

            if "grimmory_pct" in data:
                self.db_service.save_state(
                    State(
                        abs_id=abs_id,
                        book_id=book_id,
                        client_name="grimmory",
                        last_updated=last_updated,
                        percentage=data["grimmory_pct"],
                        xpath=data.get("grimmory_xpath"),
                        cfi=data.get("grimmory_cfi"),
                    )
                )

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
