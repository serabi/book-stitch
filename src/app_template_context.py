# pyright: reportMissingImports=false

import os
import time

from flask import current_app, request

TEMPLATE_DEFAULTS = {
    "TZ": "America/New_York",
    "LOG_LEVEL": "INFO",
    "DATA_DIR": "/data",
    "BOOKS_DIR": "/books",
    "ABS_COLLECTION_NAME": "Synced with KOReader",
    "GRIMMORY_SHELF_NAME": "Kobo",
    "SYNC_PERIOD_MINS": "5",
    "SYNC_DELTA_ABS_SECONDS": "60",
    "SYNC_DELTA_KOSYNC_PERCENT": "0.5",
    "SYNC_DELTA_BETWEEN_CLIENTS_PERCENT": "0.5",
    "SYNC_DELTA_KOSYNC_WORDS": "400",
    "FUZZY_MATCH_THRESHOLD": "80",
    "WHISPER_MODEL": "tiny",
    "JOB_MAX_RETRIES": "5",
    "JOB_RETRY_DELAY_MINS": "15",
    "MONITOR_INTERVAL": "3600",
    "AUDIOBOOKS_DIR": "/audiobooks",
    "ABS_PROGRESS_OFFSET_SECONDS": "0",
    "EBOOK_CACHE_SIZE": "3",
    "KOSYNC_HASH_METHOD": "content",
    "TELEGRAM_LOG_LEVEL": "ERROR",
    "ABS_ENABLED": "true",
    "KOSYNC_ENABLED": "false",
    "STORYTELLER_ENABLED": "false",
    "GRIMMORY_ENABLED": "false",
    "HARDCOVER_ENABLED": "false",
    "TELEGRAM_ENABLED": "false",
    "SUGGESTIONS_ENABLED": "false",
    "BOOKFUSION_ENABLED": "false",
    "REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT": "true",
}

_PAIRING_COUNT_CACHE = {"value": None, "expires": 0}


def _get_val(key, default_val=None):
    if key in os.environ:
        return os.environ[key]
    if key in TEMPLATE_DEFAULTS:
        return TEMPLATE_DEFAULTS[key]
    return default_val if default_val is not None else ""


def _get_bool(key):
    return _get_val(key, "false").lower() in ("true", "1", "yes", "on")


def _get_header_service_url(service_name):
    from src.utils.service_url_helper import get_service_web_url

    prefix = service_name.upper()
    if not _get_bool(f"{prefix}_ENABLED"):
        return ""
    return get_service_web_url(prefix)


def _is_active_path(path):
    req_path = request.path.rstrip("/") or "/"
    target_path = path.rstrip("/") or "/"
    if target_path == "/":
        return req_path == "/"
    return req_path == target_path or req_path.startswith(f"{target_path}/")


def _get_pairing_count_cached(db_svc):
    now = time.monotonic()
    if now > _PAIRING_COUNT_CACHE["expires"]:
        _PAIRING_COUNT_CACHE["value"] = db_svc.get_active_detected_book_count()
        _PAIRING_COUNT_CACHE["expires"] = now + 5
    return _PAIRING_COUNT_CACHE["value"]


def inject_global_vars():
    """Provide common template variables and helpers for Jinja templates."""
    pagekeeper_env = os.environ.get("PAGEKEEPER_ENV", "").strip().lower()
    is_dev_container = pagekeeper_env == "dev"
    title_prefix = "[DEV] " if is_dev_container else ""

    suggestion_count = 0
    try:
        db_svc = current_app.config.get("database_service")
        if db_svc:
            suggestion_count = _get_pairing_count_cached(db_svc)
    except Exception:
        current_app.logger.debug("Failed to get Currently Reading pairing count", exc_info=True)

    return dict(
        abs_server=os.environ.get("ABS_SERVER", ""),
        grimmory_server=os.environ.get("GRIMMORY_SERVER", ""),
        pagekeeper_env=pagekeeper_env,
        is_dev_container=is_dev_container,
        title_prefix=title_prefix,
        get_val=_get_val,
        get_bool=_get_bool,
        get_header_service_url=_get_header_service_url,
        is_active_path=_is_active_path,
        suggestion_count=suggestion_count,
    )
