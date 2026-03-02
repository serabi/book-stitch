import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path

import schedule
from dependency_injector import providers
from flask import Flask

from src.api.hardcover_routes import hardcover_bp, init_hardcover_routes
from src.api.kosync_server import init_kosync_server, kosync_admin_bp, kosync_sync_bp
from src.blueprints import register_blueprints
from src.blueprints.helpers import safe_folder_name
from src.utils.config_loader import ConfigLoader
from src.version import get_update_status


def _reconfigure_logging():
    """Force update of root logger level based on env var."""
    try:
            new_level_str = os.environ.get('LOG_LEVEL', 'INFO').upper()
            new_level = getattr(logging, new_level_str, logging.INFO)

            root = logging.getLogger()
            root.setLevel(new_level)

            logger.info(f"Logging level updated to {new_level_str}")
    except Exception as e:
            logger.warning(f"Failed to reconfigure logging: {e}")

# ---------------- APP SETUP ----------------
container = None
manager = None
database_service = None

def setup_dependencies(app, test_container=None):
    """
    Initialize dependencies for the web server.

    Args:
        test_container: Optional test container for dependency injection during testing.
                       If None, creates production container from environment.
    """
    global container, manager, database_service, DATA_DIR, EBOOK_DIR, COVERS_DIR

    # Initialize Database Service
    from src.db.migration_utils import initialize_database
    database_service = initialize_database(os.environ.get("DATA_DIR", "/data"))

    # Load settings from DB
    if database_service:
        ConfigLoader.bootstrap_config(database_service)
        ConfigLoader.load_settings(database_service)
        logger.info("Settings loaded into environment variables")

        # Migrate ABS_LIBRARY_ID -> ABS_LIBRARY_IDS
        old_lib_id = os.environ.get('ABS_LIBRARY_ID', '')
        new_lib_ids = os.environ.get('ABS_LIBRARY_IDS', '')
        if old_lib_id and not new_lib_ids:
            old_only_search = os.environ.get('ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID', 'false')
            if old_only_search.lower() == 'true':
                database_service.set_setting('ABS_LIBRARY_IDS', old_lib_id)
                os.environ['ABS_LIBRARY_IDS'] = old_lib_id
                logger.info(f"Migrated ABS_LIBRARY_ID '{old_lib_id}' to ABS_LIBRARY_IDS")

        # Force reconfigure logging level based on new settings
        _reconfigure_logging()

    # RELOAD GLOBALS from updated os.environ
    global SYNC_PERIOD_MINS

    def _get_float_env(key, default):
        try:
            return float(os.environ.get(key, str(default)))
        except (ValueError, TypeError):
            logger.warning(f"Invalid '{key}' value, defaulting to {default}")
            return float(default)

    SYNC_PERIOD_MINS = _get_float_env("SYNC_PERIOD_MINS", 5)

    logger.info(f"Globals reloaded from settings (ABS_SERVER={os.environ.get('ABS_SERVER')})")

    if test_container is not None:
        # Use injected test container
        container = test_container
    else:
        # Create production container AFTER loading settings
        from src.utils.di_container import create_container
        container = create_container()

    # Override the container's database_service with our already-initialized instance
    if test_container is None:
        container.database_service.override(providers.Object(database_service))

    # Initialize manager and services
    manager = container.sync_manager()

    # Get data directories
    DATA_DIR = container.data_dir()
    EBOOK_DIR = container.books_dir()

    # Initialize covers directory
    COVERS_DIR = DATA_DIR / "covers"
    if not COVERS_DIR.exists():
        COVERS_DIR.mkdir(parents=True, exist_ok=True)

    # Store shared state on app.config for blueprint access
    app.config['container'] = container
    app.config['sync_manager'] = manager
    app.config['database_service'] = database_service
    app.config['DATA_DIR'] = DATA_DIR
    app.config['EBOOK_DIR'] = EBOOK_DIR
    app.config['COVERS_DIR'] = COVERS_DIR

    # Register KoSync Blueprint and initialize with dependencies
    init_kosync_server(database_service, container, manager, EBOOK_DIR)
    app.register_blueprint(kosync_sync_bp)
    app.register_blueprint(kosync_admin_bp)

    # Register Hardcover Blueprint and initialize with dependencies
    init_hardcover_routes(database_service, container)
    app.register_blueprint(hardcover_bp)

    logger.info(f"Web server dependencies initialized (DATA_DIR={DATA_DIR})")


# ---------------- CONTEXT PROCESSORS ----------------
def inject_global_vars():
    def get_val(key, default_val=None):
        if key in os.environ: return os.environ[key]
        DEFAULTS = {
            'TZ': 'America/New_York',
            'LOG_LEVEL': 'INFO',
            'DATA_DIR': '/data',
            'BOOKS_DIR': '/books',
            'ABS_COLLECTION_NAME': 'Synced with KOReader',
            'BOOKLORE_SHELF_NAME': 'Kobo',
            'SYNC_PERIOD_MINS': '5',
            'SYNC_DELTA_ABS_SECONDS': '60',
            'SYNC_DELTA_KOSYNC_PERCENT': '0.5',
            'SYNC_DELTA_BETWEEN_CLIENTS_PERCENT': '0.5',
            'SYNC_DELTA_KOSYNC_WORDS': '400',
            'FUZZY_MATCH_THRESHOLD': '80',
            'WHISPER_MODEL': 'tiny',
            'JOB_MAX_RETRIES': '5',
            'JOB_RETRY_DELAY_MINS': '15',
            'MONITOR_INTERVAL': '3600',
            'AUDIOBOOKS_DIR': '/audiobooks',
            'ABS_PROGRESS_OFFSET_SECONDS': '0',
            'EBOOK_CACHE_SIZE': '3',
            'KOSYNC_HASH_METHOD': 'content',
            'TELEGRAM_LOG_LEVEL': 'ERROR',
            'SHELFMARK_URL': '',
            'KOSYNC_ENABLED': 'false',
            'STORYTELLER_ENABLED': 'false',
            'BOOKLORE_ENABLED': 'false',
            'HARDCOVER_ENABLED': 'false',
            'TELEGRAM_ENABLED': 'false',
            'SUGGESTIONS_ENABLED': 'false',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT': 'true'
        }
        if key in DEFAULTS: return DEFAULTS[key]
        return default_val if default_val is not None else ''

    def get_bool(key):
        val = os.environ.get(key, 'false')
        return val.lower() in ('true', '1', 'yes', 'on')

    return dict(
        shelfmark_url=os.environ.get("SHELFMARK_URL", ""),
        abs_server=os.environ.get("ABS_SERVER", ""),
        booklore_server=os.environ.get("BOOKLORE_SERVER", ""),
        get_val=get_val,
        get_bool=get_bool
    )


# ---------------- SYNC DAEMON ----------------
def sync_daemon():
    """Background sync daemon running in a separate thread."""
    try:
        schedule.every(int(SYNC_PERIOD_MINS)).minutes.do(manager.sync_cycle)
        schedule.every(1).minutes.do(manager.check_pending_jobs)

        logger.info(f"Sync daemon started (period: {SYNC_PERIOD_MINS} minutes)")

        # Wait for split-port server and other services to initialize
        time.sleep(5)

        # Run initial sync cycle
        try:
            manager.sync_cycle()
        except Exception as e:
            logger.error(f"Initial sync cycle failed: {e}")

        # Main daemon loop
        while True:
            try:
                schedule.run_pending()
                time.sleep(30)
            except Exception as e:
                logger.error(f"Sync daemon error: {e}")
                time.sleep(60)

    except Exception as e:
        logger.error(f"Sync daemon crashed: {e}")


# --- Logger setup ---
logger = logging.getLogger(__name__)


def _get_or_create_secret_key() -> str:
    """Return a persistent random secret key, falling back to ephemeral."""
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    key_file = data_dir / ".flask_secret_key"
    try:
        if key_file.exists():
            key = key_file.read_text().strip()
            if key:
                return key
        key = secrets.token_hex(32)
        data_dir.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key)
        key_file.chmod(0o600)
        return key
    except Exception:
        logger.warning("Could not persist Flask secret key — using ephemeral key")
        return secrets.token_hex(32)


def _log_security_warnings():
    """Log warnings for common security misconfigurations at startup."""
    kosync_user = os.environ.get('KOSYNC_USER', '')
    kosync_key = os.environ.get('KOSYNC_KEY', '')
    kosync_port = os.environ.get('KOSYNC_PORT', '')
    public_url = os.environ.get('KOSYNC_PUBLIC_URL', '')

    if not kosync_user or not kosync_key:
        logger.warning("SECURITY: KOSYNC_USER/KOSYNC_KEY not configured — sync endpoints will reject all requests")
    elif len(kosync_key) < 8:
        logger.warning("SECURITY: KOSYNC_KEY is shorter than 8 characters — consider using a stronger password")

    if not kosync_port or kosync_port == '4477':
        logger.warning("SECURITY: Split-port mode not active — dashboard and sync API share port 4477. "
                        "Set KOSYNC_PORT to a different port before exposing sync to the internet.")

    if public_url:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(public_url)
        safe_netloc = parts.hostname or ""
        if parts.port:
            safe_netloc = f"{safe_netloc}:{parts.port}"
        safe_url = urlunsplit((parts.scheme, safe_netloc, parts.path or "", "", ""))
        logger.info(f"KOSync public URL: {safe_url}")
    elif kosync_port and kosync_port != '4477':
        logger.info("Tip: Set KOSYNC_PUBLIC_URL in settings if you expose KOSync through a reverse proxy")


# --- Application Factory ---
def create_app(test_container=None):
    STATIC_DIR = os.environ.get('STATIC_DIR', '/app/static')
    TEMPLATE_DIR = os.environ.get('TEMPLATE_DIR', '/app/templates')
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static', template_folder=TEMPLATE_DIR)
    app.secret_key = _get_or_create_secret_key()

    # Setup dependencies and inject into app context
    setup_dependencies(app, test_container=test_container)

    # Register context processors, jinja globals
    app.context_processor(inject_global_vars)
    app.jinja_env.globals['safe_folder_name'] = safe_folder_name

    # Register all application blueprints
    register_blueprints(app)

    # Return both app and container for external reference
    return app, container


# ---------------- MAIN ----------------
if __name__ == '__main__':

    # Setup signal handlers to catch unexpected kills
    import signal
    def handle_exit_signal(signum, frame):
        logger.warning(f"Received signal {signum} - Shutting down...")
        for handler in logger.handlers:
            handler.flush()
        if hasattr(logging.getLogger(), 'handlers'):
            for handler in logging.getLogger().handlers:
                handler.flush()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_exit_signal)
    signal.signal(signal.SIGINT, handle_exit_signal)

    app, container = create_app()

    logger.info("=== Unified ABS Manager Started (Integrated Mode) ===")
    _log_security_warnings()

    # Start sync daemon in background thread
    sync_daemon_thread = threading.Thread(target=sync_daemon, daemon=True)
    sync_daemon_thread.start()
    threading.Thread(target=get_update_status, daemon=True).start()
    logger.info("Sync daemon thread started")

    # Start ABS Socket.IO listener for real-time / instant sync
    instant_sync_enabled = os.environ.get('INSTANT_SYNC_ENABLED', 'true').lower() != 'false'
    abs_socket_enabled = os.environ.get('ABS_SOCKET_ENABLED', 'true').lower() != 'false'
    if instant_sync_enabled and abs_socket_enabled and container.abs_client().is_configured():
        from src.services.abs_socket_listener import ABSSocketListener
        abs_listener = ABSSocketListener(
            abs_server_url=os.environ.get('ABS_SERVER', ''),
            abs_api_token=os.environ.get('ABS_KEY', ''),
            database_service=database_service,
            sync_manager=manager
        )
        abs_socket_thread = threading.Thread(target=abs_listener.start, daemon=True)
        abs_socket_thread.start()
        logger.info("ABS Socket.IO listener started (instant sync enabled)")
    elif not instant_sync_enabled:
        logger.info("ABS Socket.IO listener disabled (INSTANT_SYNC_ENABLED=false)")
    elif not abs_socket_enabled:
        logger.info("ABS Socket.IO listener disabled (ABS_SOCKET_ENABLED=false)")

    # Start per-client poller
    from src.services.client_poller import ClientPoller
    client_poller = ClientPoller(
        database_service=database_service,
        sync_manager=manager,
        sync_clients_dict=container.sync_clients(),
    )
    poller_thread = threading.Thread(target=client_poller.start, daemon=True)
    poller_thread.start()

    # Check ebook source configuration
    booklore_configured = container.booklore_client().is_configured()
    books_volume_exists = container.books_dir().exists()

    if booklore_configured:
        logger.info("Booklore integration enabled - ebooks sourced from API")
    elif books_volume_exists:
        logger.info(f"Ebooks directory mounted at {container.books_dir()}")
    else:
        logger.info(
            "NO EBOOK SOURCE CONFIGURED: Neither Booklore integration nor /books volume is available. "
            "New book matches will fail. Enable Booklore (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD) "
            "or mount the ebooks directory to /books."
        )

    logger.info("Web interface starting on port 4477")

    # --- Split-Port Mode ---
    sync_port = os.environ.get('KOSYNC_PORT')
    if sync_port and int(sync_port) != 4477:
        def run_sync_only_server(port):
            sync_app = Flask(__name__)
            sync_app.register_blueprint(kosync_sync_bp)
            @sync_app.route('/')
            def sync_health():
                return "Sync Server OK", 200
            sync_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

        threading.Thread(target=run_sync_only_server, args=(int(sync_port),), daemon=True).start()
        logger.info(f"Split-Port Mode Active: Sync-only server on port {sync_port}")

    app.run(host='0.0.0.0', port=4477, debug=False)
