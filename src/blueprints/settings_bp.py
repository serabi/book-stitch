"""Settings blueprint — GET/POST /settings."""

import logging
import os
import threading

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from src.blueprints.helpers import get_container, get_database_service, restart_server

logger = logging.getLogger(__name__)

settings_bp = Blueprint('settings_page', __name__)


@settings_bp.route('/settings', methods=['GET', 'POST'])
def settings():
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
            'SHELFMARK_ENABLED',
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
            if key in bool_keys:
                continue

            clean_value = value.strip()

            url_keys = [
                'SHELFMARK_URL', 'ABS_SERVER', 'BOOKLORE_SERVER', 'BOOKLORE_2_SERVER',
                'STORYTELLER_API_URL', 'CWA_SERVER', 'KOSYNC_SERVER'
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
            threading.Thread(target=restart_server).start()
            session['message'] = "Settings saved. Application is restarting..."
            session['is_error'] = False
        except Exception as e:
            session['message'] = f"Error saving settings: {e}"
            session['is_error'] = True
            logger.error(f"Error saving settings: {e}")

        return redirect(url_for('settings_page.settings'))

    # GET Request
    message = session.pop('message', None)
    is_error = session.pop('is_error', False)

    return render_template('settings.html',
                           message=message,
                           is_error=is_error)


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
