"""Settings blueprint — GET/POST /settings."""

import logging
import os

import requests as http_requests
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from src.blueprints.helpers import get_container, get_database_service
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)

settings_bp = Blueprint('settings_page', __name__)


def _is_secret_request_authorized() -> bool:
    """Authorize secret reveal requests.

    Allowed if either:
    - Session indicates an admin user, or
    - Caller presents a valid internal service token.
    """
    if bool(session.get('is_admin')):
        return True

    expected_token = os.environ.get('INTERNAL_SERVICE_TOKEN', '').strip()
    provided_token = request.headers.get('X-Internal-Service-Token', '').strip()
    if expected_token and secrets_compare(expected_token, provided_token):
        return True

    return False


def _mask_secret(value: str) -> str:
    """Return a masked secret showing only the last 4 characters."""
    if not value:
        return ''
    tail = value[-4:] if len(value) >= 4 else value
    return f"{'*' * max(0, len(value) - len(tail))}{tail}"


def secrets_compare(a: str, b: str) -> bool:
    """Constant-time secret comparison."""
    import hmac
    return hmac.compare_digest(a, b)


@settings_bp.route('/settings', methods=['GET', 'POST'])
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

    if request.method == 'POST':
        bool_keys = [
            'ABS_ENABLED',
            'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
            'SYNC_ABS_EBOOK',
            'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT',
            'KOSYNC_ENABLED',
            'STORYTELLER_ENABLED',
            'BOOKLORE_ENABLED',
            'CWA_ENABLED',
            'HARDCOVER_ENABLED',
            'TELEGRAM_ENABLED',
            'SUGGESTIONS_ENABLED',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
            'INSTANT_SYNC_ENABLED',
            'ABS_SOCKET_ENABLED',
            'BOOKFUSION_ENABLED',
        ]

        current_settings = database_service.get_all_settings()

        secret_keys = {
            'ABS_KEY', 'STORYTELLER_PASSWORD', 'BOOKLORE_PASSWORD',
            'CWA_PASSWORD', 'KOSYNC_KEY', 'TELEGRAM_BOT_TOKEN', 'HARDCOVER_TOKEN',
            'DEEPGRAM_API_KEY', 'BOOKFUSION_API_KEY', 'BOOKFUSION_UPLOAD_API_KEY',
        }

        # 1. Handle Boolean Toggles
        for key in bool_keys:
            is_checked = (key in request.form)
            val_str = str(is_checked).lower()
            database_service.set_setting(key, val_str)
            os.environ[key] = val_str

        # 2. Handle Text Inputs
        for key, value in request.form.items():
            if key == '_active_tab':
                continue
            if key in bool_keys:
                continue

            clean_value = value.strip()

            url_keys = [
                'ABS_SERVER', 'BOOKLORE_SERVER',
                'STORYTELLER_API_URL', 'CWA_SERVER', 'KOSYNC_SERVER',
                'ABS_WEB_URL', 'BOOKLORE_WEB_URL',
                'STORYTELLER_WEB_URL', 'CWA_WEB_URL', 'HARDCOVER_WEB_URL',
            ]
            if key in url_keys and clean_value:
                lower_val = clean_value.lower()
                if not (lower_val.startswith("http://") or lower_val.startswith("https://")):
                    clean_value = f"http://{clean_value}"

            if not clean_value and key in secret_keys:
                continue  # preserve existing secret

            if clean_value:
                database_service.set_setting(key, clean_value)
                os.environ[key] = clean_value
            elif key in current_settings:
                database_service.set_setting(key, "")
                os.environ[key] = ""

        try:
            from src.web_server import apply_settings
            apply_settings(current_app._get_current_object())
            session['message'] = "Settings saved successfully."
            session['is_error'] = False
        except Exception as e:
            session['message'] = f"Error applying settings: {e}"
            session['is_error'] = True
            logger.error(f"Error applying settings: {e}")

        active_tab = request.form.get('_active_tab', 'general')
        return redirect(url_for('settings_page.settings', tab=active_tab))

    # GET Request
    message = session.pop('message', None)
    is_error = session.pop('is_error', False)

    return render_template('settings.html',
                           message=message,
                           is_error=is_error)


@settings_bp.route('/api/settings/secret/<key>', methods=['GET'])
def get_secret(key):
    """Return a stored secret value (for reveal-on-demand UI)."""
    allowed = {'KOSYNC_KEY'}
    if key not in allowed:
        return jsonify({'error': 'Not allowed'}), 403

    caller = request.headers.get('X-Forwarded-For', request.remote_addr)
    logger.info(f"AUDIT: Secret requested (key={key}, caller={caller})")

    value = os.environ.get(key, '')
    return jsonify({'value': value, 'present': bool(value)})


@settings_bp.route('/api/kosync/test', methods=['POST'])
def test_kosync_connection():
    """Test connection to the configured KoSync server (legacy route)."""
    return test_connection('kosync')


@settings_bp.route('/api/test-connection/<service>', methods=['GET', 'POST'])
def test_connection(service):
    """Test connectivity to a configured service. Returns JSON with success/detail."""
    testers = {
        'abs': _test_abs,
        'kosync': _test_kosync,
        'storyteller': _test_storyteller,
        'booklore': _test_booklore,
        'cwa': _test_cwa,
        'hardcover': _test_hardcover,
        'telegram': _test_telegram,
        'bookfusion': _test_bookfusion,
        'bookfusion_upload': _test_bookfusion_upload,
    }
    tester = testers.get(service)
    if not tester:
        return jsonify({'success': False, 'detail': 'Unknown service'}), 400
    try:
        success, detail = tester()
    except Exception as e:
        logger.warning(f"Connection test for '{service}' failed: {_redact_secrets(str(sanitize_log_data(e)))}")
        success, detail = False, _test_conn_error(e)
    return jsonify({'success': success, 'detail': detail})


def _redact_secrets(msg: str) -> str:
    """Replace any known secret values in a string with a fixed mask."""
    secret_keys = [
        'ABS_KEY', 'STORYTELLER_PASSWORD', 'BOOKLORE_PASSWORD',
        'CWA_PASSWORD', 'KOSYNC_KEY', 'TELEGRAM_BOT_TOKEN', 'HARDCOVER_TOKEN',
        'DEEPGRAM_API_KEY', 'BOOKFUSION_API_KEY', 'BOOKFUSION_UPLOAD_API_KEY',
    ]
    for key in secret_keys:
        val = os.environ.get(key, '')
        if val and val in msg:
            msg = msg.replace(val, '***')
    return msg


def _test_conn_error(e: Exception) -> str:
    """Return a user-friendly error string from a request exception."""
    msg = str(e)
    if 'ConnectionError' in type(e).__name__ or 'connection' in msg.lower():
        return 'Connection refused — is the server running?'
    if 'Timeout' in type(e).__name__:
        return 'Request timed out'
    if 'NameResolutionError' in msg or 'getaddrinfo' in msg:
        return 'Server hostname could not be resolved — check the URL'
    return _redact_secrets(str(sanitize_log_data(msg)))


_HTTP_FRIENDLY = {
    401: 'Authentication failed — check your username and password',
    403: 'Access denied — your account may not have permission',
    404: 'Endpoint not found — check the server URL',
    500: 'Server returned an internal error',
    502: 'Bad gateway — is a reverse proxy misconfigured?',
    503: 'Service unavailable — the server may be starting up',
}


def _http_error(status_code: int) -> str:
    """Return a user-friendly message for an HTTP error status."""
    friendly = _HTTP_FRIENDLY.get(status_code)
    if friendly:
        return f'{friendly} (HTTP {status_code})'
    return f'Unexpected response (HTTP {status_code})'


def _test_abs() -> tuple[bool, str]:
    url = os.environ.get('ABS_SERVER', '').rstrip('/')
    token = os.environ.get('ABS_KEY', '')
    if not url or not token:
        return False, 'Server URL or API token not configured'
    resp = http_requests.get(
        f"{url}/api/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if resp.status_code == 200:
        username = resp.json().get('username', 'unknown')
        return True, f'Connected as {username}'
    return False, _http_error(resp.status_code)


def _test_kosync() -> tuple[bool, str]:
    container = get_container()
    kosync_client = container.kosync_client()
    success = bool(kosync_client.check_connection())
    return success, 'Connected' if success else 'Healthcheck failed'


def _test_storyteller() -> tuple[bool, str]:
    url = os.environ.get('STORYTELLER_API_URL', '').rstrip('/')
    user = os.environ.get('STORYTELLER_USER', '')
    pw = os.environ.get('STORYTELLER_PASSWORD', '')
    if not url or not user:
        return False, 'API URL or credentials not configured'
    resp = http_requests.post(
        f"{url}/api/token",
        data={"username": user, "password": pw},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if resp.status_code == 200:
        return True, 'Authenticated'
    return False, _http_error(resp.status_code)


def _test_booklore() -> tuple[bool, str]:
    return _test_booklore_instance('BOOKLORE')


def _test_booklore_instance(prefix: str) -> tuple[bool, str]:
    url = os.environ.get(f'{prefix}_SERVER', '').rstrip('/')
    user = os.environ.get(f'{prefix}_USER', '')
    pw = os.environ.get(f'{prefix}_PASSWORD', '')
    if not url or not user:
        return False, 'Server URL or credentials not configured'
    resp = http_requests.post(
        f"{url}/api/v1/auth/login",
        json={"username": user, "password": pw},
        timeout=10,
    )
    if resp.status_code == 200:
        return True, 'Authenticated'
    return False, _http_error(resp.status_code)


def _test_cwa() -> tuple[bool, str]:
    url = os.environ.get('CWA_SERVER', '').rstrip('/')
    user = os.environ.get('CWA_USERNAME', '')
    pw = os.environ.get('CWA_PASSWORD', '')
    if not url or not user:
        return False, 'Server URL or credentials not configured'
    resp = http_requests.get(
        f"{url}/opds",
        auth=(user, pw),
        timeout=10,
        allow_redirects=False,
    )
    # CWA redirects to login page on auth failure
    if resp.status_code == 200 and 'login' not in resp.text[:500].lower():
        return True, 'Connected'
    if resp.status_code in (301, 302):
        return False, 'Redirected to login — check credentials'
    return False, _http_error(resp.status_code)


def _test_hardcover() -> tuple[bool, str]:
    token = os.environ.get('HARDCOVER_TOKEN', '')
    if not token:
        return False, 'API token not configured'
    resp = http_requests.post(
        'https://api.hardcover.app/v1/graphql',
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": "{ me { id username } }"},
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        if data.get('errors'):
            err_msg = data['errors'][0].get('message', 'Unknown GraphQL error')
            return False, f'GraphQL error: {err_msg}'
        me = data.get('data', {}).get('me')
        if isinstance(me, list):
            me = me[0] if me else {}
        elif not isinstance(me, dict):
            me = {}
        if not isinstance(me, dict) or not me.get('id'):
            return False, 'Authentication succeeded but user data is missing'
        username = me.get('username', 'unknown')
        return True, f'Connected as {username}'
    return False, _http_error(resp.status_code)


def _test_telegram() -> tuple[bool, str]:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if not token:
        return False, 'Bot token not configured'
    resp = http_requests.get(
        f"https://api.telegram.org/bot{token}/getMe",
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        bot_name = data.get('result', {}).get('first_name', 'Bot')
        return True, f'Connected ({bot_name})'
    return False, _http_error(resp.status_code)


def _get_api_key_override() -> str | None:
    """Read api_key from POST JSON body only (not query params, to avoid credential leakage)."""
    if request.method == 'POST' and request.is_json:
        key = (request.json or {}).get('api_key', '').strip()
        if key:
            return key
    return None


def _test_bookfusion() -> tuple[bool, str]:
    container = get_container()
    client = container.bookfusion_client()
    return client.check_connection(api_key_override=_get_api_key_override())


def _test_bookfusion_upload() -> tuple[bool, str]:
    container = get_container()
    client = container.bookfusion_client()
    return client.check_upload_connection(api_key_override=_get_api_key_override())
