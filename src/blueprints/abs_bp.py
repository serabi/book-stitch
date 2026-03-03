"""ABS blueprint — routes specific to Audiobookshelf integration."""

import logging

import requests
from flask import Blueprint, Response, jsonify

from src.blueprints.helpers import get_abs_service, get_container

logger = logging.getLogger(__name__)

abs_bp = Blueprint('abs', __name__)


@abs_bp.route('/api/abs/libraries', methods=['GET'])
def get_abs_libraries():
    """Return available ABS libraries."""
    abs_service = get_abs_service()
    if not abs_service.is_available():
        return jsonify({"error": "ABS not configured"}), 400
    libraries = abs_service.get_libraries()
    return jsonify(libraries)


@abs_bp.route('/api/cover-proxy/<abs_id>')
def proxy_cover(abs_id):
    """Proxy cover access to allow loading covers from local network ABS instances."""
    abs_service = get_abs_service()
    if not abs_service.is_available():
        return "ABS not configured", 404

    try:
        container = get_container()
        token = container.abs_client().token
        base_url = container.abs_client().base_url

        url = f"{base_url.rstrip('/')}/api/items/{abs_id}/cover"

        req = requests.get(url, headers={"Authorization": f"Bearer {token}"}, stream=True, timeout=10)
        if req.status_code == 200:
            resp = Response(req.iter_content(chunk_size=1024), content_type=req.headers.get('content-type', 'image/jpeg'))
            resp.headers['Cache-Control'] = 'public, max-age=86400, immutable'
            return resp
        else:
            return "Cover not found", 404
    except Exception as e:
        logger.error(f"Error proxying cover for '{abs_id}': {e}")
        return "Error loading cover", 500
