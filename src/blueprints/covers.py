"""Covers blueprint — /covers/<filename> and /api/cover-proxy/booklore/."""

import logging
import re

import requests
from flask import Blueprint, Response, send_from_directory

from src.blueprints.helpers import get_booklore_clients, get_container, get_covers_dir, get_database_service

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
        resp = send_from_directory(COVERS_DIR, filename)
        resp.headers['Cache-Control'] = 'public, max-age=86400, immutable'
        return resp

    # 2. Try to extract
    database_service = get_database_service()
    container = get_container()
    book = database_service.get_book_by_kosync_id(doc_hash)

    if book and book.ebook_filename:
        try:
            parser = container.ebook_parser()
            full_book_path = parser.resolve_book_path(book.ebook_filename)

            if parser.extract_cover(full_book_path, cover_path):
                resp = send_from_directory(COVERS_DIR, filename)
                resp.headers['Cache-Control'] = 'public, max-age=86400, immutable'
                return resp
        except Exception as e:
            logger.debug(f"Lazy cover extraction failed: {e}")

    return "Cover not found", 404


@covers_bp.route('/api/cover-proxy/booklore/<source_tag>/<int:book_id>')
def proxy_booklore_cover(source_tag, book_id):
    """Proxy cover access to Booklore instances (requires Bearer token auth)."""
    if not re.fullmatch(r'[a-zA-Z0-9_\-]+', source_tag):
        return "Invalid source tag", 400

    try:
        bl_client = None
        for client in get_booklore_clients():
            if client.source_tag == source_tag and client.is_configured():
                bl_client = client
                break

        if not bl_client:
            return "Booklore not configured", 404

        token = bl_client._get_fresh_token()
        if not token:
            return "Booklore auth failed", 500

        url = f"{bl_client.base_url}/api/v1/books/{book_id}/cover"
        req = requests.get(url, headers={"Authorization": f"Bearer {token}"}, stream=True, timeout=10)
        if req.status_code == 200:
            resp = Response(req.iter_content(chunk_size=1024), content_type=req.headers.get('content-type', 'image/jpeg'))
            resp.headers['Cache-Control'] = 'public, max-age=86400, immutable'
            return resp
        else:
            return "Cover not found", 404
    except Exception as e:
        logger.error(f"Error proxying Booklore cover for book {book_id} ({source_tag}): {e}")
        return "Error loading cover", 500
