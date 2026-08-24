"""Settings blueprint — GET/POST /settings."""

import logging
import os

import requests as http_requests
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from src.blueprints.helpers import get_container, get_database_service
from src.utils.config_loader import load_settings, validate_timezone
from src.utils.logging_utils import sanitize_log_data
from src.utils.secret_settings import SECRET_SETTING_KEYS
from src.version import APP_VERSION, get_update_status

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings_page", __name__)

URL_SETTING_KEYS = {
    "ABS_SERVER",
    "GRIMMORY_SERVER",
    "GRIMMORY_2_SERVER",
    "STORYTELLER_API_URL",
    "CWA_SERVER",
    "KOSYNC_SERVER",
    "ABS_WEB_URL",
    "GRIMMORY_WEB_URL",
    "GRIMMORY_2_WEB_URL",
    "STORYTELLER_WEB_URL",
    "CWA_WEB_URL",
    "HARDCOVER_WEB_URL",
    "ABS_WEB_URL_INTERNAL",
    "ABS_WEB_URL_EXTERNAL",
    "STORYTELLER_WEB_URL_INTERNAL",
    "STORYTELLER_WEB_URL_EXTERNAL",
    "GRIMMORY_WEB_URL_INTERNAL",
    "GRIMMORY_WEB_URL_EXTERNAL",
    "GRIMMORY_2_WEB_URL_INTERNAL",
    "GRIMMORY_2_WEB_URL_EXTERNAL",
    "CWA_WEB_URL_INTERNAL",
    "CWA_WEB_URL_EXTERNAL",
    "HARDCOVER_WEB_URL_EXTERNAL",
    "KOSYNC_PUBLIC_URL",
}

BOOL_SETTING_KEYS = [
    "ABS_ENABLED",
    "KOSYNC_USE_PERCENTAGE_FROM_SERVER",
    "SYNC_ABS_EBOOK",
    "XPATH_FALLBACK_TO_PREVIOUS_SEGMENT",
    "KOSYNC_ENABLED",
    "STORYTELLER_ENABLED",
    "STORYTELLER_FORCE_MODE",
    "GRIMMORY_ENABLED",
    "GRIMMORY_2_ENABLED",
    "CWA_ENABLED",
    "HARDCOVER_ENABLED",
    "TELEGRAM_ENABLED",
    "SUGGESTIONS_ENABLED",
    "REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT",
    "INSTANT_SYNC_ENABLED",
    "ABS_SOCKET_ENABLED",
    "BOOKFUSION_ENABLED",
    "LIBBY_ENABLED",
]


def _is_secret_request_authorized() -> bool:
    """Authorize secret reveal requests via admin session or service token."""
    if bool(session.get("is_admin")):
        return True

    expected_token = os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()
    provided_token = request.headers.get("X-Internal-Service-Token", "").strip()
    if expected_token and secrets_compare(expected_token, provided_token):
        return True

    return False


def _mask_secret(value: str) -> str:
    """Return a masked secret showing only the last 4 characters."""
    if not value:
        return ""
    tail = value[-4:] if len(value) >= 4 else value
    return f"{'*' * max(0, len(value) - len(tail))}{tail}"


def secrets_compare(a: str, b: str) -> bool:
    """Constant-time secret comparison."""
    import hmac

    return hmac.compare_digest(a, b)


def _normalize_url_value(value: str) -> str:
    clean_value = value.strip()
    if not clean_value:
        return ""
    lower_val = clean_value.lower()
    if not (lower_val.startswith("http://") or lower_val.startswith("https://")):
        return f"http://{clean_value}"
    return clean_value


def _request_payload() -> dict:
    try:
        if request.method == "POST" and request.is_json:
            return request.get_json(silent=True) or {}
    except RuntimeError:
        pass
    return {}


def _request_value(key: str, env_key: str | None = None, *, secret: bool = False, normalize_url: bool = False) -> str:
    payload = _request_payload()
    source_key = env_key or key

    if key in payload:
        value = str(payload.get(key, "") or "").strip()
        if secret and value == "":
            value = os.environ.get(source_key, "")
        if normalize_url:
            return _normalize_url_value(value)
        return value

    value = os.environ.get(source_key, "")
    if normalize_url:
        return _normalize_url_value(value)
    return value.strip()


def _reload_client_config(client_getter, label: str):
    try:
        client = client_getter()
        reload_from_env = getattr(client, "reload_from_env", None)
        if callable(reload_from_env):
            reload_from_env()
            logger.info("%s configuration reloaded from environment", label)
    except Exception as e:
        logger.warning("Failed to reload %s configuration: %s", label, e)


def _reload_integration_clients():
    try:
        container = current_app.config.get("container")
        if container is None:
            from src import app_setup

            container = app_setup.container
    except Exception as e:
        logger.warning("Failed to locate DI container for settings reload: %s", e)
        container = None

    if container is not None:
        _reload_client_config(lambda: container.grimmory_client(), "Grimmory")
        if hasattr(container, "grimmory_client_2"):
            _reload_client_config(lambda: container.grimmory_client_2(), "Grimmory 2")
        _reload_client_config(lambda: container.abs_client(), "Audiobookshelf")
        _reload_client_config(lambda: container.hardcover_client(), "Hardcover")
        if hasattr(container, "kosync_client"):
            _reload_client_config(lambda: container.kosync_client(), "KoSync client")

    _reload_client_config(lambda: current_app.config["kosync_service"], "KoSync service")


def _render_settings(*, message=None, is_error=False, form_values=None, timezone_error=None, status=200):
    latest_version, update_available = get_update_status()
    template_values = {}
    if form_values is not None:
        from src.app_template_context import _get_bool, _get_val

        def submitted_get_val(key, default_val=None):
            return form_values.get(key, "") if key in form_values else _get_val(key, default_val)

        def submitted_get_bool(key):
            return key in form_values if key in BOOL_SETTING_KEYS else _get_bool(key)

        template_values = {"get_val": submitted_get_val, "get_bool": submitted_get_bool}

    return (
        render_template(
            "settings.html",
            message=message,
            is_error=is_error,
            timezone_error=timezone_error,
            app_version=APP_VERSION,
            update_available=update_available,
            latest_version=latest_version,
            **template_values,
        ),
        status,
    )


@settings_bp.route("/settings", methods=["GET", "POST"])
def settings():
    """
    Handle the settings page: persist submitted configuration on POST and render the settings UI on GET.

    On POST, updates persistent settings and corresponding environment variables from the submitted form:
    - Updates boolean toggles.
    - Persists text inputs, preserving secret values when left empty.
    - Normalizes URL-like values by ensuring an http:// or https:// prefix when missing.
    After persisting, attempts to apply the updated settings; on success sets a success message in the session, on failure sets an error message and logs the exception. Redirects back to the settings page preserving the active tab.

    On GET, reads any session message and error flag, removes them from the session, and renders the settings template.

    Returns:
    - A redirect response to the settings page when handling POST.
    - A rendered template response for the settings page when handling GET.
    """
    database_service = get_database_service()

    if request.method == "POST":
        timezone = request.form.get("TZ")
        if timezone is not None and validate_timezone(timezone) is None:
            error = "Invalid timezone. Use an IANA name such as America/New_York."
            return _render_settings(
                message=error,
                is_error=True,
                form_values=request.form,
                timezone_error=error,
                status=400,
            )

        current_settings = database_service.get_all_settings()

        # 1. Handle Boolean Toggles
        for key in BOOL_SETTING_KEYS:
            is_checked = key in request.form
            val_str = str(is_checked).lower()
            database_service.set_setting(key, val_str)
            os.environ[key] = val_str

        # 2. Handle Text Inputs
        for key, value in request.form.items():
            if key == "_active_tab":
                continue
            if key in BOOL_SETTING_KEYS:
                continue

            clean_value = _normalize_url_value(value) if key in URL_SETTING_KEYS else value.strip()

            if not clean_value and key in SECRET_SETTING_KEYS:
                continue  # preserve existing secret

            if clean_value:
                database_service.set_setting(key, clean_value)
                os.environ[key] = clean_value
            elif key in current_settings:
                database_service.set_setting(key, "")
                os.environ[key] = ""

        try:
            load_settings(database_service)
            _reload_integration_clients()

            from src.app_runtime import apply_settings

            apply_settings(current_app._get_current_object())
            session["message"] = "Settings saved successfully."
            session["is_error"] = False
        except Exception as e:
            session["message"] = f"Error applying settings: {e}"
            session["is_error"] = True
            logger.error(f"Error applying settings: {e}")

        active_tab = request.form.get("_active_tab", "general")
        return redirect(url_for("settings_page.settings", tab=active_tab))

    # GET Request
    message = session.pop("message", None)
    is_error = session.pop("is_error", False)

    return _render_settings(message=message, is_error=is_error)


@settings_bp.route("/api/settings/secret/<key>", methods=["GET"])
def get_secret(key):
    """Return a stored secret value (for reveal-on-demand UI)."""
    if not _is_secret_request_authorized():
        return jsonify({"error": "Unauthorized"}), 403

    allowed = {"KOSYNC_KEY", "KOSYNC_SERVER_KEY"}
    if key not in allowed:
        return jsonify({"error": "Not allowed"}), 403

    caller = request.headers.get("X-Forwarded-For", request.remote_addr)
    logger.info(f"AUDIT: Secret requested (key={key}, caller={caller})")

    value = os.environ.get(key, "")
    return jsonify({"value": value, "present": bool(value)})


@settings_bp.route("/api/libby/connect", methods=["POST"])
def libby_connect():
    """Pair Libby. Body {"code": "NNNNNNNN"} pairs a sync-only chip;
    {"token": "<jwt>"} pairs a full-access browser identity token."""
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()
    token = str(payload.get("token", "")).strip()
    if not code and not token:
        return jsonify({"success": False, "detail": "Provide a setup code or an identity token."}), 400
    if code and token:
        return jsonify({"success": False, "detail": "Provide either a setup code or a token, not both."}), 400

    client = get_container().libby_client()
    result = client.pair_with_setup_code(code) if code else client.pair_with_identity_token(token)
    if not result.get("success"):
        return jsonify({"success": False, "detail": result.get("detail", "Pairing failed.")}), 400

    database_service = get_database_service()
    load_settings(database_service)
    return jsonify(
        {
            "success": True,
            "mode": "code" if code else "token",
            "can_read_positions": client.can_read_positions,
            "cards": result.get("cards", []),
        }
    )


@settings_bp.route("/api/libby/disconnect", methods=["POST"])
def libby_disconnect():
    """Revoke all Libby chips and clear stored credentials."""
    client = get_container().libby_client()
    if not client.identity_token and not client.sync_token:
        return jsonify({"success": True})
    if not client.disconnect():
        return (
            jsonify(
                {"success": False, "detail": "Could not confirm revocation with Libby — not clearing local settings."}
            ),
            502,
        )
    client.clear_credentials()
    database_service = get_database_service()
    database_service.set_setting("LIBBY_ENABLED", "false")
    os.environ["LIBBY_ENABLED"] = "false"
    load_settings(database_service)
    return jsonify({"success": True})


@settings_bp.route("/api/kosync/test", methods=["POST"])
def test_kosync_connection():
    """Test connection to the configured KoSync server (legacy route)."""
    return test_connection("kosync")


@settings_bp.route("/api/test-connection/<service>", methods=["GET", "POST"])
def test_connection(service):
    """Test connectivity to a configured service. Returns JSON with success/detail."""
    testers = {
        "abs": _test_abs,
        "kosync": _test_kosync,
        "storyteller": _test_storyteller,
        "grimmory": _test_grimmory,
        "grimmory2": _test_grimmory2,
        "cwa": _test_cwa,
        "hardcover": _test_hardcover,
        "telegram": _test_telegram,
        "bookfusion": _test_bookfusion,
        "bookfusion_upload": _test_bookfusion_upload,
        "libby": _test_libby,
    }
    tester = testers.get(service)
    if not tester:
        return jsonify({"success": False, "detail": "Unknown service"}), 400
    try:
        success, detail = tester()
    except Exception as e:
        logger.warning(f"Connection test for '{service}' failed: {_redact_secrets(str(sanitize_log_data(e)))}")
        success, detail = False, _test_conn_error(e)
    return jsonify({"success": success, "detail": detail})


def _redact_secrets(msg: str) -> str:
    """Replace any known secret values in a string with a fixed mask."""
    for key in SECRET_SETTING_KEYS:
        val = os.environ.get(key, "")
        if val and val in msg:
            msg = msg.replace(val, "***")
    return msg


def _test_conn_error(e: Exception) -> str:
    """Return a user-friendly error string from a request exception."""
    msg = str(e)
    if "ConnectionError" in type(e).__name__ or "connection" in msg.lower():
        return "Connection refused — is the server running?"
    if "Timeout" in type(e).__name__:
        return "Request timed out"
    if "NameResolutionError" in msg or "getaddrinfo" in msg:
        return "Server hostname could not be resolved — check the URL"
    return str(sanitize_log_data(_redact_secrets(msg)))


_HTTP_FRIENDLY = {
    401: "Authentication failed — check your username and password",
    403: "Access denied — your account may not have permission",
    404: "Endpoint not found — check the server URL",
    500: "Server returned an internal error",
    502: "Bad gateway — is a reverse proxy misconfigured?",
    503: "Service unavailable — the server may be starting up",
}


def _http_error(status_code: int) -> str:
    """Return a user-friendly message for an HTTP error status."""
    friendly = _HTTP_FRIENDLY.get(status_code)
    if friendly:
        return f"{friendly} (HTTP {status_code})"
    return f"Unexpected response (HTTP {status_code})"


def _test_abs() -> tuple[bool, str]:
    url = _request_value("server", "ABS_SERVER", normalize_url=True).rstrip("/")
    token = _request_value("token", "ABS_KEY", secret=True)
    if not url or not token:
        return False, "Server URL or API token not configured"
    resp = http_requests.get(
        f"{url}/api/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if resp.status_code == 200:
        username = resp.json().get("username", "unknown")
        return True, f"Connected as {username}"
    return False, _http_error(resp.status_code)


def _test_kosync() -> tuple[bool, str]:
    from urllib.parse import urlparse

    url = _request_value("server", "KOSYNC_SERVER", normalize_url=True).rstrip("/")
    hostname = urlparse(url).hostname or "" if url else ""
    is_external = hostname not in ("127.0.0.1", "::1", "localhost", "")
    if is_external:
        user = _request_value("user", "KOSYNC_SERVER_USER") or _request_value("user", "KOSYNC_USER")
        key = _request_value("key", "KOSYNC_SERVER_KEY", secret=True) or _request_value(
            "key", "KOSYNC_KEY", secret=True
        )
    else:
        user = _request_value("user", "KOSYNC_USER")
        key = _request_value("key", "KOSYNC_KEY", secret=True)
    if not url or not user:
        return False, "Server URL or credentials not configured"

    headers = {}
    if key:
        from src.utils.kosync_headers import hash_kosync_key, kosync_auth_headers

        headers = kosync_auth_headers(user, hash_kosync_key(key))
    try:
        resp = http_requests.get(f"{url}/healthcheck", timeout=5, headers=headers)
        if resp.status_code == 200:
            return True, "Connected"
        fallback = http_requests.get(f"{url}/syncs/progress/test-connection", timeout=5, headers=headers)
        if fallback.status_code == 200:
            return True, "Connected"
        return False, _http_error(fallback.status_code)
    except Exception as e:
        return False, _test_conn_error(e)


def _test_storyteller() -> tuple[bool, str]:
    url = _request_value("api_url", "STORYTELLER_API_URL", normalize_url=True).rstrip("/")
    user = _request_value("user", "STORYTELLER_USER")
    pw = _request_value("password", "STORYTELLER_PASSWORD", secret=True)
    if not url or not user:
        return False, "API URL or credentials not configured"
    resp = http_requests.post(
        f"{url}/api/token",
        data={"username": user, "password": pw},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if resp.status_code == 200:
        return True, "Authenticated"
    return False, _http_error(resp.status_code)


def _test_grimmory() -> tuple[bool, str]:
    return _test_grimmory_instance("GRIMMORY")


def _test_grimmory2() -> tuple[bool, str]:
    return _test_grimmory_instance("GRIMMORY_2")


def _test_grimmory_instance(prefix: str) -> tuple[bool, str]:
    url = _request_value("server", f"{prefix}_SERVER", normalize_url=True).rstrip("/")
    user = _request_value("user", f"{prefix}_USER")
    pw = _request_value("password", f"{prefix}_PASSWORD", secret=True)
    if not url or not user:
        return False, "Server URL or credentials not configured"
    resp = http_requests.post(
        f"{url}/api/v1/auth/login",
        json={"username": user, "password": pw},
        timeout=10,
    )
    if resp.status_code == 200:
        return True, "Authenticated"
    return False, _http_error(resp.status_code)


def _test_cwa() -> tuple[bool, str]:
    url = _request_value("server", "CWA_SERVER", normalize_url=True).rstrip("/")
    user = _request_value("user", "CWA_USERNAME")
    pw = _request_value("password", "CWA_PASSWORD", secret=True)
    if not url or not user:
        return False, "Server URL or credentials not configured"
    resp = http_requests.get(
        f"{url}/opds",
        auth=(user, pw),
        timeout=10,
        allow_redirects=False,
    )
    # CWA redirects to login page on auth failure
    if resp.status_code == 200 and "login" not in resp.text[:500].lower():
        return True, "Connected"
    if resp.status_code in (301, 302):
        return False, "Redirected to login — check credentials"
    return False, _http_error(resp.status_code)


def _test_hardcover() -> tuple[bool, str]:
    token = _request_value("token", "HARDCOVER_TOKEN", secret=True)
    if not token:
        return False, "API token not configured"
    resp = http_requests.post(
        "https://api.hardcover.app/v1/graphql",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": "{ me { id username } }"},
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        if data.get("errors"):
            err_msg = data["errors"][0].get("message", "Unknown GraphQL error")
            return False, f"GraphQL error: {err_msg}"
        me = data.get("data", {}).get("me")
        if isinstance(me, list):
            me = me[0] if me else {}
        elif not isinstance(me, dict):
            me = {}
        if not isinstance(me, dict) or not me.get("id"):
            return False, "Authentication succeeded but user data is missing"
        username = me.get("username", "unknown")
        return True, f"Connected as {username}"
    return False, _http_error(resp.status_code)


def _test_telegram() -> tuple[bool, str]:
    token = _request_value("bot_token", "TELEGRAM_BOT_TOKEN", secret=True)
    chat_id = _request_value("chat_id", "TELEGRAM_CHAT_ID")
    if not token:
        return False, "Bot token not configured"
    if not chat_id:
        return False, "Chat ID not configured"

    me_resp = http_requests.get(
        f"https://api.telegram.org/bot{token}/getMe",
        timeout=10,
    )
    if me_resp.status_code != 200:
        return False, _http_error(me_resp.status_code)

    data = me_resp.json()
    bot_name = data.get("result", {}).get("first_name", "Bot")

    send_resp = http_requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": "PageKeeper test message: Telegram notifications are configured correctly.",
        },
        timeout=10,
    )
    if send_resp.status_code == 200:
        return True, f"Test message sent via {bot_name}"

    try:
        description = send_resp.json().get("description", "")
    except Exception:
        description = ""
    if description:
        return False, _redact_secrets(description)
    return False, _http_error(send_resp.status_code)


def _test_bookfusion() -> tuple[bool, str]:
    container = get_container()
    client = container.bookfusion_client()
    return client.check_connection(api_key_override=_request_value("api_key", "BOOKFUSION_API_KEY", secret=True))


def _test_bookfusion_upload() -> tuple[bool, str]:
    container = get_container()
    client = container.bookfusion_client()
    return client.check_upload_connection(
        api_key_override=_request_value("api_key", "BOOKFUSION_UPLOAD_API_KEY", secret=True)
    )


def _test_libby() -> tuple[bool, str]:
    client = get_container().libby_client()
    if not client.identity_token:
        return False, "Not paired — connect with a setup code first"
    if client.check_connection():
        state = client.get_sync_state() or {}
        cards = len(state.get("cards") or [])
        return True, f"Connected ({cards} card(s))"
    return False, "Libby rejected the stored identity — reconnect required"
