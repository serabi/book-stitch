"""BookFusion API client — upload books via Calibre API and sync highlights via Obsidian API.

Upload logic mirrors the official Calibre plugin (BookFusion/calibre-plugin on GitHub).
The Calibre plugin uses Qt's QHttpMultiPart which omits Content-Type headers on form
parts. Python's `requests` library always adds Content-Type: application/octet-stream,
which the BookFusion API rejects. We build the multipart body manually to match the
plugin's exact wire format.
"""

import base64
import hashlib
import logging
import os
import re
import uuid
from datetime import UTC, datetime

import requests

logger = logging.getLogger(__name__)

BASE_URL = 'https://www.bookfusion.com'
CALIBRE_API = f'{BASE_URL}/calibre-api/v1'
CALIBRE_USER_AGENT = 'BookFusion Calibre Plugin 0.5.2'


def _build_multipart(fields: list[tuple[str, str | tuple[str, bytes]]]) -> tuple[bytes, str]:
    """Build a multipart/form-data body matching Qt's QHttpMultiPart format.

    Each text field gets only a Content-Disposition header (no Content-Type),
    exactly like the Calibre plugin's build_req_part with ContentTypeHeader=None.

    Args:
        fields: list of (name, value) for text fields, or
                (name, (filename, data)) for file fields.

    Returns:
        (body_bytes, content_type_header)
    """
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields:
        if isinstance(value, tuple):
            fname, fdata = value
            parts.append(
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'
                f'\r\n'.encode() + fdata + b'\r\n'
            )
        else:
            parts.append(
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{name}"\r\n'
                f'\r\n'
                f'{value}\r\n'.encode()
            )
    parts.append(f'--{boundary}--\r\n'.encode())
    body = b''.join(parts)
    content_type = f'multipart/form-data; boundary={boundary}'
    return body, content_type


def _calibre_auth_header(api_key: str) -> str:
    """Build Basic auth header matching the Calibre plugin's format (key: with empty password)."""
    token = base64.b64encode(f'{api_key}:'.encode()).decode('ascii')
    return f'Basic {token}'


def _calibre_headers(api_key: str, extra: dict | None = None) -> dict:
    """Standard headers for Calibre API requests."""
    headers = {
        'User-Agent': CALIBRE_USER_AGENT,
        'Authorization': _calibre_auth_header(api_key),
        'Accept': 'application/json',
    }
    if extra:
        headers.update(extra)
    return headers


def _calibre_digest(file_bytes: bytes) -> str:
    """Calculate file digest matching the Calibre plugin's calculate_digest method.

    The plugin hashes: file_size_as_bytes + null byte + file_content (in 64k blocks).
    """
    h = hashlib.sha256()
    h.update(str(len(file_bytes)).encode())
    h.update(b'\0')
    offset = 0
    while offset < len(file_bytes):
        h.update(file_bytes[offset:offset + 65536])
        offset += 65536
    return h.hexdigest()


def _parse_frontmatter_title(frontmatter: str | None) -> str:
    """Extract title from a YAML frontmatter string (e.g. 'title: My Book\\nauthor: ...')."""
    if not frontmatter:
        return ''
    for line in frontmatter.splitlines():
        if line.startswith('title:'):
            return line[len('title:'):].strip().strip('"').strip("'")
    return ''


def _parse_frontmatter(frontmatter: str | None) -> dict:
    """Parse YAML frontmatter into a dict with title, authors, tags, series."""
    result = {'title': '', 'authors': '', 'tags': '', 'series': ''}
    if not frontmatter:
        return result

    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith('title:'):
            result['title'] = line[len('title:'):].strip().strip('"').strip("'")
        elif line.startswith('author:') or line.startswith('authors:'):
            key = 'authors:' if line.startswith('authors:') else 'author:'
            result['authors'] = line[len(key):].strip().strip('"').strip("'")
        elif line.startswith('tags:'):
            result['tags'] = line[len('tags:'):].strip().strip('"').strip("'")
        elif line.startswith('series:'):
            result['series'] = line[len('series:'):].strip().strip('"').strip("'")

    return result


def _parse_highlight_date(content: str) -> datetime | None:
    """Parse the Date Created timestamp from a BookFusion highlight markdown string."""
    m = re.search(r'\*\*Date Created\*\*:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*UTC', content)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _parse_highlight_quote(content: str) -> str | None:
    """Extract blockquoted text from a BookFusion highlight markdown string."""
    lines = content.split('\n')
    quote_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('>'):
            txt = stripped.lstrip('>').strip()
            if txt:
                quote_lines.append(txt)
    return ' '.join(quote_lines) if quote_lines else None


class BookFusionClient:

    def __init__(self):
        self.session = requests.Session()

    @property
    def highlights_api_key(self) -> str:
        return os.environ.get('BOOKFUSION_API_KEY', '')

    @property
    def upload_api_key(self) -> str:
        return os.environ.get('BOOKFUSION_UPLOAD_API_KEY', '')

    def is_configured(self) -> bool:
        return bool(self.highlights_api_key) or bool(self.upload_api_key)

    def check_connection(self, api_key_override: str | None = None) -> tuple[bool, str]:
        """Test connectivity by hitting the highlights sync endpoint with a null cursor."""
        key = api_key_override or self.highlights_api_key
        if not key:
            return False, 'Highlights API key not configured'
        try:
            resp = self.session.post(
                f'{BASE_URL}/obsidian-api/sync',
                headers={'X-Token': key, 'API-Version': '1', 'Content-Type': 'application/json'},
                json={'cursor': None},
                timeout=15,
            )
            if resp.status_code == 200:
                return True, 'Connected'
            return False, f'HTTP {resp.status_code}'
        except requests.RequestException as e:
            return False, str(e)

    def check_upload_connection(self, api_key_override: str | None = None) -> tuple[bool, str]:
        """Test connectivity to the Calibre upload API."""
        key = api_key_override or self.upload_api_key
        if not key:
            return False, 'Upload API key not configured'
        try:
            resp = self.session.get(
                f'{CALIBRE_API}/uploads?isbn=test',
                headers=_calibre_headers(key),
                timeout=15,
            )
            if resp.status_code == 200:
                return True, 'Connected'
            return False, f'HTTP {resp.status_code}'
        except requests.RequestException as e:
            return False, str(e)

    # ── Upload (Calibre API — mirrors BookFusion/calibre-plugin) ──

    def check_exists(self, digest: str) -> dict | None:
        """Check if a book already exists on BookFusion by SHA256 digest."""
        if not self.upload_api_key:
            return None
        try:
            resp = self.session.get(
                f'{CALIBRE_API}/uploads/{digest}',
                headers=_calibre_headers(self.upload_api_key),
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except requests.RequestException:
            return None

    def upload_book(self, filename: str, file_bytes: bytes, title: str, authors: str) -> dict | None:
        """Upload a book to BookFusion. Mirrors the Calibre plugin's 3-step flow."""
        if not self.upload_api_key:
            logger.warning("BookFusion upload skipped: upload API key not configured")
            return None
        digest = _calibre_digest(file_bytes)

        existing = self.check_exists(digest)
        if existing:
            logger.info(f"Book already exists on BookFusion: {filename}")
            return existing

        headers = _calibre_headers(self.upload_api_key)

        # Step 1: Init upload — POST /uploads/init (multipart: filename + digest)
        try:
            body, ct = _build_multipart([
                ('filename', filename),
                ('digest', digest),
            ])
            init_resp = self.session.post(
                f'{CALIBRE_API}/uploads/init',
                headers={**headers, 'Content-Type': ct},
                data=body,
                timeout=15,
            )
            if init_resp.status_code not in (200, 201):
                logger.error("BookFusion upload init failed: HTTP %s", init_resp.status_code)
                return None
            init_data = init_resp.json()
        except requests.RequestException as e:
            logger.error(f"BookFusion upload init error: {e}")
            return None

        # Step 2: Upload to S3 — POST to pre-signed URL (form params + file)
        s3_url = init_data.get('url')
        s3_params = init_data.get('params') or {}
        if not s3_url:
            logger.error("BookFusion upload init returned no S3 URL")
            return None

        try:
            s3_fields: list[tuple[str, str | tuple[str, bytes]]] = []
            for k, v in s3_params.items():
                s3_fields.append((k, v))
            s3_fields.append(('file', (filename, file_bytes)))
            s3_body, s3_ct = _build_multipart(s3_fields)

            s3_resp = self.session.post(
                s3_url,
                headers={'Content-Type': s3_ct},
                data=s3_body,
                timeout=120,
            )
            if s3_resp.status_code not in (200, 201, 204):
                logger.error("BookFusion S3 upload failed: HTTP %s", s3_resp.status_code)
                return None
            logger.info(f"BookFusion S3 upload succeeded: HTTP {s3_resp.status_code}")
        except requests.RequestException as e:
            logger.error(f"BookFusion S3 upload error: {e}")
            return None

        # Step 3: Finalize — POST /uploads/finalize (multipart: key, digest, metadata)
        s3_key = s3_params.get('key', '')
        try:
            # Build metadata digest matching the Calibre plugin's get_metadata_digest
            h = hashlib.sha256()
            h.update(title.encode('utf-8'))
            author_list = [a.strip() for a in authors.split(',') if a.strip()]
            for author in author_list:
                h.update(author.encode('utf-8'))
            meta_digest = h.hexdigest()

            finalize_fields: list[tuple[str, str]] = [
                ('key', s3_key),
                ('digest', digest),
                ('metadata[calibre_metadata_digest]', meta_digest),
                ('metadata[title]', title),
            ]
            for author in author_list:
                finalize_fields.append(('metadata[author_list][]', author))

            body, ct = _build_multipart(finalize_fields)
            finalize_resp = self.session.post(
                f'{CALIBRE_API}/uploads/finalize',
                headers={**headers, 'Content-Type': ct},
                data=body,
                timeout=30,
            )
            logger.info("BookFusion finalize response: HTTP %s", finalize_resp.status_code)
            if finalize_resp.status_code in (200, 201):
                logger.info(f"BookFusion upload finalized: {filename}")
                return finalize_resp.json()
            logger.error("BookFusion finalize failed: HTTP %s", finalize_resp.status_code)
            return None
        except requests.RequestException as e:
            logger.error(f"BookFusion finalize error: {e}")
            return None

    # ── Library catalog (Calibre API) ──

    def fetch_library(self) -> list[dict]:
        """Fetch all books in the user's BookFusion library via the Calibre API.

        Returns a list of book dicts with at minimum 'id' and 'title'.
        Paginates through all pages if the API supports it.
        """
        if not self.upload_api_key:
            return []

        all_books = []
        page = 1
        while True:
            try:
                resp = self.session.get(
                    f'{CALIBRE_API}/uploads',
                    headers=_calibre_headers(self.upload_api_key),
                    params={'page': page, 'per_page': 100},
                    timeout=30,
                )
                if resp.status_code != 200:
                    logger.warning(f"BookFusion library fetch failed: HTTP {resp.status_code}")
                    break
                data = resp.json()
                books = data if isinstance(data, list) else data.get('books', data.get('uploads', []))
                if not books:
                    break
                all_books.extend(books)
                if len(books) < 100:
                    break
                page += 1
            except requests.RequestException as e:
                logger.error(f"BookFusion library fetch error: {e}")
                break

        return all_books

    # ── Highlights (Obsidian API, X-Token) ──

    def fetch_highlights(self, cursor: str | None = None) -> dict:
        """Fetch one page of highlights from the Obsidian sync API."""
        if not self.highlights_api_key:
            raise ValueError('Highlights API key not configured')
        resp = self.session.post(
            f'{BASE_URL}/obsidian-api/sync',
            headers={'X-Token': self.highlights_api_key, 'API-Version': '1', 'Content-Type': 'application/json'},
            json={'cursor': cursor},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def sync_all_highlights(self, db_service) -> dict:
        """Paginate through all highlights and save to DB.

        Returns dict with 'new_highlights' and 'books_saved' counts.

        Response structure (from BookFusion Obsidian plugin source):
          { pages: Page[], cursor: str|null, next_sync_cursor: str|null }
        where BookPage = { type:'book', id, filename, frontmatter:str|null,
                           highlights: [{id, content, chapter_heading}], ... }
        """
        cursor = db_service.get_bookfusion_sync_cursor()
        total_new = 0
        all_books = {}
        seen_cursors = set()
        last_next_sync_cursor = None

        while True:
            data = self.fetch_highlights(cursor)
            pages = data.get('pages') or []

            if data.get('next_sync_cursor'):
                last_next_sync_cursor = data['next_sync_cursor']

            highlights_batch = []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                if page.get('type') != 'book':
                    continue

                book_id = str(page.get('id', '')).strip()
                if not book_id:
                    continue
                raw_frontmatter = page.get('frontmatter')
                parsed = _parse_frontmatter(raw_frontmatter)
                book_title = parsed['title'] or page.get('filename', '')
                if book_title.endswith('.md'):
                    book_title = book_title[:-3].strip()

                hl_count = len(page.get('highlights') or [])
                if book_id not in all_books:
                    all_books[book_id] = {
                        'bookfusion_id': book_id,
                        'title': book_title,
                        'authors': parsed['authors'],
                        'filename': page.get('filename', ''),
                        'frontmatter': raw_frontmatter,
                        'tags': parsed['tags'],
                        'series': parsed['series'],
                        'highlight_count': hl_count,
                    }
                else:
                    all_books[book_id]['highlight_count'] += hl_count

                for hl in (page.get('highlights') or []):
                    if not isinstance(hl, dict):
                        continue
                    highlight_id = hl.get('id', '')
                    if not highlight_id:
                        continue
                    content = hl.get('content') or ''
                    highlights_batch.append({
                        'bookfusion_book_id': book_id,
                        'highlight_id': highlight_id,
                        'content': content,
                        'chapter_heading': hl.get('chapter_heading'),
                        'book_title': book_title,
                        'highlighted_at': _parse_highlight_date(content),
                        'quote_text': _parse_highlight_quote(content),
                    })

            if highlights_batch:
                total_new += db_service.save_bookfusion_highlights(highlights_batch)

            next_page = data.get('cursor')
            if next_page is None:
                break
            if next_page == cursor:
                logger.warning("BookFusion sync: next cursor same as current, stopping")
                break
            if next_page in seen_cursors:
                logger.warning("BookFusion sync: pagination loop detected, stopping")
                break

            seen_cursors.add(next_page)
            cursor = next_page

        if last_next_sync_cursor:
            db_service.set_bookfusion_sync_cursor(last_next_sync_cursor)

        # Merge full library catalog
        try:
            library = self.fetch_library()
            for item in library:
                bid = str(item.get('id', '')).strip()
                if not bid or bid in all_books:
                    continue
                title = item.get('title', '') or item.get('filename', '')
                if title.endswith('.md'):
                    title = title[:-3].strip()
                authors = item.get('author', '') or item.get('authors', '')
                if isinstance(authors, list):
                    authors = ', '.join(authors)
                all_books[bid] = {
                    'bookfusion_id': bid,
                    'title': title,
                    'authors': authors,
                    'filename': item.get('filename', ''),
                    'frontmatter': None,
                    'tags': '',
                    'series': '',
                    'highlight_count': 0,
                }
        except Exception as e:
            logger.warning(f"Could not fetch full BookFusion library: {e}")

        books_saved = 0
        if all_books:
            books_saved = db_service.save_bookfusion_books(list(all_books.values()))

        return {'new_highlights': total_new, 'books_saved': books_saved}
