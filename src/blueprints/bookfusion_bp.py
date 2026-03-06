"""BookFusion blueprint — upload books and sync highlights."""

import difflib
import logging
import os
import re

from flask import Blueprint, jsonify, render_template, request

from src.blueprints.helpers import get_booklore_clients, get_container, get_database_service

logger = logging.getLogger(__name__)

bookfusion_bp = Blueprint('bookfusion', __name__)

SUPPORTED_FORMATS = {'.epub', '.mobi', '.azw3', '.pdf', '.azw', '.fb2', '.cbz', '.cbr'}


def _is_supported(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in SUPPORTED_FORMATS)


@bookfusion_bp.route('/bookfusion')
def bookfusion_page():
    return render_template('bookfusion.html')


@bookfusion_bp.route('/api/bookfusion/booklore-books')
def booklore_books():
    """List Booklore books for upload selection, filtered by supported formats."""
    q = request.args.get('q', '').strip()
    results = []

    for client in get_booklore_clients():
        if not client.is_configured():
            continue
        try:
            label = os.environ.get(f"{client.config_prefix}_LABEL", "Booklore")
            books = client.search_books(q) if q else client.get_all_books()
            for b in (books or []):
                fname = b.get('fileName', '')
                if not _is_supported(fname):
                    continue
                results.append({
                    'id': b.get('id'),
                    'title': b.get('title', ''),
                    'authors': b.get('authors', ''),
                    'fileName': fname,
                    'source': label,
                    'source_tag': client.source_tag,
                })
        except Exception as e:
            logger.warning(f"Booklore ({client.source_tag}) search failed: {e}")

    return jsonify(results)


@bookfusion_bp.route('/api/bookfusion/upload', methods=['POST'])
def upload_book():
    """Upload a book from Booklore to BookFusion."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    book_id = data.get('book_id')
    source_tag = data.get('source_tag', 'booklore')
    title = data.get('title', '')
    authors = data.get('authors', '')
    filename = data.get('fileName', '')

    if not book_id:
        return jsonify({'error': 'book_id required'}), 400

    container = get_container()
    bf_client = container.bookfusion_client()

    if not bf_client.upload_api_key:
        return jsonify({'error': 'BookFusion upload API key not configured'}), 400

    # Find the right Booklore client by source_tag
    bl_client = None
    for client in get_booklore_clients():
        if client.source_tag == source_tag and client.is_configured():
            bl_client = client
            break

    if not bl_client:
        return jsonify({'error': f'Booklore instance "{source_tag}" not found'}), 400

    # Download from Booklore
    file_bytes = bl_client.download_book(book_id)
    if not file_bytes:
        return jsonify({'error': 'Failed to download book from Booklore'}), 500

    # Upload to BookFusion
    logger.info(f"BookFusion upload request: title='{title}', authors='{authors}', filename='{filename}'")
    result = bf_client.upload_book(filename, file_bytes, title, authors)
    if result:
        return jsonify({'success': True, 'result': result})
    return jsonify({'error': 'Upload to BookFusion failed'}), 500


@bookfusion_bp.route('/api/bookfusion/sync-highlights', methods=['POST'])
def sync_highlights():
    """Trigger highlight sync from BookFusion."""
    container = get_container()
    bf_client = container.bookfusion_client()
    db_service = get_database_service()

    if not bf_client.highlights_api_key:
        return jsonify({'error': 'BookFusion highlights API key not configured'}), 400

    try:
        new_count = bf_client.sync_all_highlights(db_service)
        matched = _auto_match_highlights(db_service)
        return jsonify({'success': True, 'new_highlights': new_count, 'auto_matched': matched})
    except Exception as e:
        logger.error(f"BookFusion highlight sync failed: {e}")
        return jsonify({'error': str(e)}), 500


STRIP_EXTENSIONS = re.compile(r'\.(epub|mobi|azw3?|pdf|fb2|cbz|cbr|md)$', re.IGNORECASE)


def _normalize_title(title: str) -> str:
    """Normalize a title for matching: strip extensions, lowercase, collapse whitespace."""
    t = STRIP_EXTENSIONS.sub('', title)
    return ' '.join(t.lower().split())


def _auto_match_highlights(db_service) -> int:
    """Auto-match unlinked BookFusion highlights to PageKeeper books by title similarity."""
    unmatched = db_service.get_unmatched_bookfusion_highlights()
    if not unmatched:
        return 0

    books = db_service.get_all_books()
    if not books:
        return 0

    # Build normalized title → abs_id map
    book_map = {}
    for b in books:
        if b.abs_title:
            norm = _normalize_title(b.abs_title)
            book_map[norm] = b.abs_id

    # Group unmatched by book_title
    title_groups: dict[str, list] = {}
    for hl in unmatched:
        title = _clean_book_title(hl.book_title or '')
        title_groups.setdefault(title, []).append(hl)

    matched_count = 0
    norm_keys = list(book_map.keys())

    for bf_title, highlights in title_groups.items():
        norm_bf = _normalize_title(bf_title)
        abs_id = None

        # Exact match
        if norm_bf in book_map:
            abs_id = book_map[norm_bf]
        else:
            # Fuzzy match
            best_ratio = 0.0
            for norm_pk in norm_keys:
                ratio = difflib.SequenceMatcher(None, norm_bf, norm_pk).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    if ratio > 0.85:
                        abs_id = book_map[norm_pk]

        if abs_id:
            raw_title = highlights[0].book_title
            db_service.link_bookfusion_book(raw_title, abs_id)
            matched_count += len(highlights)

    return matched_count


def _clean_book_title(title: str) -> str:
    """Strip .md suffix and wiki-link artifacts from book titles."""
    if title.endswith('.md'):
        title = title[:-3]
    return title.strip()


@bookfusion_bp.route('/api/bookfusion/highlights')
def get_highlights():
    """Return cached highlights from DB, grouped by book."""
    db_service = get_database_service()
    highlights = db_service.get_bookfusion_highlights()

    grouped = {}
    for hl in highlights:
        book = _clean_book_title(hl.book_title or 'Unknown Book')
        if book not in grouped:
            grouped[book] = {'highlights': [], 'matched_abs_id': hl.matched_abs_id}
        date_str = hl.highlighted_at.strftime('%Y-%m-%d %H:%M:%S') if hl.highlighted_at else None
        grouped[book]['highlights'].append({
            'id': hl.id,
            'quote': hl.quote_text or hl.content,
            'date': date_str,
            'chapter_heading': hl.chapter_heading,
            'matched_abs_id': hl.matched_abs_id,
        })

    # Sort highlights within each book by date
    for book in grouped:
        grouped[book]['highlights'].sort(key=lambda h: h['date'] or '', reverse=True)

    cursor = db_service.get_bookfusion_sync_cursor()

    # Include list of PageKeeper books for journal matching
    books = db_service.get_all_books()
    book_list = [{'abs_id': b.abs_id, 'title': b.abs_title} for b in books if b.abs_title]

    return jsonify({'highlights': grouped, 'has_synced': cursor is not None, 'books': book_list})


@bookfusion_bp.route('/api/bookfusion/link-highlight', methods=['POST'])
def link_highlight():
    """Manually link or unlink a BookFusion book's highlights to a PageKeeper book."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    book_title = data.get('book_title')
    abs_id = data.get('abs_id')  # None or empty to unlink

    if not book_title:
        return jsonify({'error': 'book_title required'}), 400

    db_service = get_database_service()
    db_service.link_bookfusion_book(book_title, abs_id or None)
    return jsonify({'success': True})


@bookfusion_bp.route('/api/bookfusion/save-journal', methods=['POST'])
def save_highlight_to_journal():
    """Save BookFusion highlights as reading journal entries for a book."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    abs_id = data.get('abs_id')
    highlights = data.get('highlights', [])

    if not abs_id:
        return jsonify({'error': 'abs_id required'}), 400
    if not highlights:
        return jsonify({'error': 'No highlights provided'}), 400

    db_service = get_database_service()
    book = db_service.get_book(abs_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    saved = 0
    for hl in highlights:
        quote = hl.get('quote', '').strip()
        chapter = hl.get('chapter', '')
        if not quote:
            continue
        entry = f"📖 {quote}"
        if chapter:
            entry += f"\n— {chapter}"
        try:
            db_service.add_reading_journal(abs_id, 'note', entry=entry)
            saved += 1
        except Exception as e:
            logger.warning(f"Failed to save journal entry: {e}")

    return jsonify({'success': True, 'saved': saved})
