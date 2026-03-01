"""Covers blueprint — /covers/<filename> and /api/cover-proxy/<abs_id>."""

import logging

import requests
from flask import Blueprint, send_from_directory, Response

from src.blueprints.helpers import get_container, get_database_service, get_covers_dir

logger = logging.getLogger(__name__)

covers_bp = Blueprint('covers', __name__)


@covers_bp.route('/covers/<path:filename>')
def serve_cover(filename):
    """Serve cover images with lazy extraction."""
    COVERS_DIR = get_covers_dir()
    doc_hash = filename.replace('.jpg', '')

    # 1. Check if file exists
    cover_path = COVERS_DIR / filename
    if cover_path.exists():
        return send_from_directory(COVERS_DIR, filename)

    # 2. Try to extract
    database_service = get_database_service()
    container = get_container()
    book = database_service.get_book_by_kosync_id(doc_hash)

    if book and book.ebook_filename:
        try:
            parser = container.ebook_parser()
            full_book_path = parser.resolve_book_path(book.ebook_filename)

            if parser.extract_cover(full_book_path, cover_path):
                return send_from_directory(COVERS_DIR, filename)
        except Exception as e:
            logger.debug(f"Lazy cover extraction failed: {e}")

    return "Cover not found", 404


@covers_bp.route('/api/cover-proxy/<abs_id>')
def proxy_cover(abs_id):
    """Proxy cover access to allow loading covers from local network ABS instances."""
    try:
        container = get_container()
        token = container.abs_client().token
        base_url = container.abs_client().base_url
        if not token or not base_url:
            return "ABS not configured", 500

        url = f"{base_url.rstrip('/')}/api/items/{abs_id}/cover?token={token}"

        req = requests.get(url, stream=True, timeout=10)
        if req.status_code == 200:
            return Response(req.iter_content(chunk_size=1024), content_type=req.headers.get('content-type', 'image/jpeg'))
        else:
            return "Cover not found", 404
    except Exception as e:
        logger.error(f"Error proxying cover for '{abs_id}': {e}")
        return "Error loading cover", 500
