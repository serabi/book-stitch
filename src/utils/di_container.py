#!/usr/bin/env python3
"""
Dependency Injection Container for PageKeeper.
Using python-dependency-injector library for proper DI functionality.
"""

import logging
import os
from pathlib import Path

from dependency_injector import containers, providers

# Import all the classes we'll be using
from src.api.api_clients import ABSClient, KoSyncClient
from src.api.bookfusion_client import BookFusionClient
from src.api.booklore_client import BookloreClient
from src.api.cwa_client import CWAClient
from src.api.hardcover_client import HardcoverClient
from src.api.storyteller_api import StorytellerAPIClient
from src.db.database_service import DatabaseService
from src.services.abs_service import ABSService
from src.services.alignment_service import AlignmentService
from src.services.background_job_service import BackgroundJobService
from src.services.library_service import LibraryService
from src.services.migration_service import MigrationService
from src.services.suggestion_service import SuggestionService
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.hardcover_sync_client import HardcoverSyncClient
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.sync_manager import SyncManager
from src.utils.ebook_utils import EbookParser
from src.utils.polisher import Polisher
from src.utils.smil_extractor import SmilExtractor
from src.utils.transcriber import AudioTranscriber

logger = logging.getLogger(__name__)

class Container(containers.DeclarativeContainer):
    """Main dependency injection container using dependency-injector library."""

    # Configuration
    config = providers.Configuration()

    # Configuration values from environment (Lazy evaluation)
    data_dir = providers.Factory(
        lambda: Path(os.environ.get("DATA_DIR", "/data"))
    )

    books_dir = providers.Factory(
        lambda: Path(os.environ.get("BOOKS_DIR", "/books"))
    )

    db_file = providers.Factory(
        lambda data_dir: data_dir / "mapping_db.json",
        data_dir=data_dir
    )
    state_file = providers.Factory(
        lambda data_dir: data_dir / "last_state.json",
        data_dir=data_dir
    )
    epub_cache_dir = providers.Factory(
        lambda data_dir: data_dir / "epub_cache",
        data_dir=data_dir
    )

    # Lazy load specific config values
    delta_abs_thresh = providers.Factory(lambda: float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60)))
    delta_kosync_thresh = providers.Factory(lambda: float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0)
    kosync_use_percentage_from_server = providers.Factory(lambda: os.getenv("KOSYNC_USE_PERCENTAGE_FROM_SERVER", "false").lower() == "true")

    # API Clients
    abs_client = providers.Singleton(ABSClient)

    # ABS Service (guarded wrapper)
    abs_service = providers.Singleton(
        ABSService,
        abs_client
    )

    kosync_client = providers.Singleton(KoSyncClient)

    # SQLAlchemy Database Service - Moved up for dependency injection
    database_service = providers.Singleton(
        DatabaseService,
        providers.Factory(
            lambda data_dir: str(data_dir / "database.db"),
            data_dir=data_dir
        )
    )

    booklore_client = providers.Singleton(
        BookloreClient,
        database_service=database_service,
        config_prefix="BOOKLORE",
        source_tag="booklore"
    )

    hardcover_client = providers.Singleton(HardcoverClient)

    cwa_client = providers.Singleton(CWAClient)

    bookfusion_client = providers.Singleton(BookFusionClient)

    # Ebook parser
    ebook_parser = providers.Singleton(
        EbookParser,
        books_dir,
        epub_cache_dir=epub_cache_dir
    )

    # Smil Extractor Provider
    smil_extractor = providers.Singleton(
        SmilExtractor
    )

    polisher = providers.Singleton(
        Polisher
    )

    alignment_service = providers.Singleton(
        AlignmentService,
        database_service=database_service,
        polisher=polisher
    )

    library_service = providers.Singleton(
        LibraryService,
        database_service=database_service,
        booklore_client=booklore_client,
        cwa_client=cwa_client,
        abs_client=abs_client,
        epub_cache_dir=epub_cache_dir
    )

    migration_service = providers.Singleton(
        MigrationService,
        database_service=database_service,
        alignment_service=alignment_service,
        data_dir=data_dir
    )

    # Storyteller client with factory
    storyteller_client = providers.Singleton(
        StorytellerAPIClient
    )

    # Transcriber
    transcriber = providers.Singleton(
        AudioTranscriber,
        data_dir,
        smil_extractor,
        polisher
    )

    # Sync clients
    abs_sync_client = providers.Singleton(
        ABSSyncClient,
        abs_client,
        transcriber,
        ebook_parser,
        alignment_service,
        data_dir
    )

    kosync_sync_client = providers.Singleton(
        KoSyncSyncClient,
        kosync_client,
        ebook_parser
    )

    storyteller_sync_client = providers.Singleton(
        StorytellerSyncClient,
        storyteller_client,
        ebook_parser,
        database_service
    )

    booklore_sync_client = providers.Singleton(
        BookloreSyncClient,
        booklore_client,
        ebook_parser,
        client_name="BookLore"
    )

    abs_ebook_sync_client = providers.Singleton(
        ABSEbookSyncClient,
        abs_client,
        ebook_parser
    )

    hardcover_sync_client = providers.Singleton(
        HardcoverSyncClient,
        hardcover_client,
        ebook_parser,
        abs_client,
        database_service
    )

    # Suggestion Service
    suggestion_service = providers.Singleton(
        SuggestionService,
        database_service=database_service,
        abs_client=abs_client,
        booklore_clients=providers.List(booklore_client),
        storyteller_client=storyteller_client,
        library_service=library_service,
        books_dir=books_dir,
        ebook_parser=ebook_parser,
    )

    # Background Job Service
    background_job_service = providers.Singleton(
        BackgroundJobService,
        database_service=database_service,
        abs_client=abs_client,
        booklore_clients=providers.List(booklore_client),
        ebook_parser=ebook_parser,
        transcriber=transcriber,
        alignment_service=alignment_service,
        library_service=library_service,
        storyteller_client=storyteller_client,
        epub_cache_dir=epub_cache_dir,
        data_dir=data_dir,
        books_dir=books_dir,
    )

    # Sync clients dictionary for reuse
    sync_clients = providers.Dict(
        ABS=abs_sync_client,
        ABSEbook=abs_ebook_sync_client,
        KoSync=kosync_sync_client,
        Storyteller=storyteller_sync_client,
        BookLore=booklore_sync_client,
        Hardcover=hardcover_sync_client
    )

    # Sync Manager
    sync_manager = providers.Singleton(
        SyncManager,
        abs_client=abs_client,
        booklore_client=booklore_client,
        hardcover_client=hardcover_client,
        storyteller_client=storyteller_client,
        transcriber=transcriber,
        ebook_parser=ebook_parser,
        database_service=database_service,
        sync_clients=sync_clients,

        alignment_service=alignment_service,
        library_service=library_service,
        migration_service=migration_service,
        suggestion_service=suggestion_service,
        background_job_service=background_job_service,

        epub_cache_dir=epub_cache_dir,
        data_dir=data_dir,
        books_dir=books_dir
    )


# Global container instance
container = Container()

def create_container() -> Container:
    """Create and configure the DI container with all application dependencies."""
    return container
