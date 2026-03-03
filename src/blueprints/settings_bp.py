"""Settings blueprint — GET/POST /settings."""

import logging
import os

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from src.blueprints.helpers import get_container, get_database_service

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
            'BOOKLORE_2_ENABLED',
            'CWA_ENABLED',
            'HARDCOVER_ENABLED',
            'TELEGRAM_ENABLED',
            'SUGGESTIONS_ENABLED',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
            'INSTANT_SYNC_ENABLED',
            'ABS_SOCKET_ENABLED',
        ]

        current_settings = database_service.get_all_settings()

        secret_keys = {
            'ABS_KEY', 'STORYTELLER_PASSWORD', 'BOOKLORE_PASSWORD', 'BOOKLORE_2_PASSWORD',
            'CWA_PASSWORD', 'KOSYNC_KEY', 'TELEGRAM_BOT_TOKEN', 'HARDCOVER_TOKEN',
            'DEEPGRAM_API_KEY',
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
                'ABS_SERVER', 'BOOKLORE_SERVER', 'BOOKLORE_2_SERVER',
                'STORYTELLER_API_URL', 'CWA_SERVER', 'KOSYNC_SERVER',
                'ABS_WEB_URL', 'BOOKLORE_WEB_URL', 'BOOKLORE_2_WEB_URL',
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

    if not _is_secret_request_authorized():
        logger.warning(f"AUDIT: Unauthorized secret request denied (key={key}, caller={caller})")
        return jsonify({'error': 'Forbidden'}), 403

    value = os.environ.get(key, '')
    return jsonify({'value': value, 'present': bool(value)})


@settings_bp.route('/api/kosync/test', methods=['POST'])
def test_kosync_connection():
    """Test connection to the configured KoSync server."""
    container = get_container()
    kosync_client = container.kosync_client()
    try:
        success = bool(kosync_client.check_connection())
    except Exception as e:
        logger.warning(f"KoSync connection test failed: {e}")
        success = False
    return jsonify({'success': success})
