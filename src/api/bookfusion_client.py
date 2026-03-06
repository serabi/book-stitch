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
from datetime import datetime

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
                f'\r\n'.encode('utf-8') + fdata + b'\r\n'
            )
        else:
            parts.append(
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{name}"\r\n'
                f'\r\n'
                f'{value}\r\n'.encode('utf-8')
            )
    parts.append(f'--{boundary}--\r\n'.encode('utf-8'))
    body = b''.join(parts)
    content_type = f'multipart/form-data; boundary={boundary}'
    return body, content_type


def _calibre_auth_header(api_key: str) -> str:
    """Build Basic auth header matching the Calibre plugin's format (key: with empty password)."""
    token = base64.b64encode(f'{api_key}:'.encode('utf-8')).decode('ascii')
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
    h.update(bytes(len(file_bytes)))
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


def _parse_highlight_date(content: str) -> datetime | None:
    """Parse the Date Created timestamp from a BookFusion highlight markdown string."""
    m = re.search(r'\*\*Date Created\*\*:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*UTC', content)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
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
            resp = requests.get(
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
        try:
            resp = requests.get(
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
            init_resp = requests.post(
                f'{CALIBRE_API}/uploads/init',
                headers={**headers, 'Content-Type': ct},
                data=body,
                timeout=15,
            )
            if init_resp.status_code not in (200, 201):
                logger.error(f"BookFusion upload init failed: HTTP {init_resp.status_code} — {init_resp.text[:500]}")
                return None
            init_data = init_resp.json()
        except requests.RequestException as e:
            logger.error(f"BookFusion upload init error: {e}")
            return None

        # Step 2: Upload to S3 — POST to pre-signed URL (form params + file)
        s3_url = init_data.get('url')
        s3_params = init_data.get('params', {})
        if not s3_url:
            logger.error("BookFusion upload init returned no S3 URL")
            return None

        try:
            s3_fields: list[tuple[str, str | tuple[str, bytes]]] = []
            for k, v in s3_params.items():
                s3_fields.append((k, v))
            s3_fields.append(('file', (filename, file_bytes)))
            s3_body, s3_ct = _build_multipart(s3_fields)

            s3_resp = requests.post(
                s3_url,
                headers={'Content-Type': s3_ct},
                data=s3_body,
                timeout=120,
            )
            if s3_resp.status_code not in (200, 201, 204):
                logger.error(f"BookFusion S3 upload failed: HTTP {s3_resp.status_code} — {s3_resp.text[:500]}")
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
            finalize_resp = requests.post(
                f'{CALIBRE_API}/uploads/finalize',
                headers={**headers, 'Content-Type': ct},
                data=body,
                timeout=30,
            )
            logger.info(f"BookFusion finalize response: HTTP {finalize_resp.status_code} — {finalize_resp.text[:500]}")
            if finalize_resp.status_code in (200, 201):
                logger.info(f"BookFusion upload finalized: {filename}")
                return finalize_resp.json()
            logger.error(f"BookFusion finalize failed: HTTP {finalize_resp.status_code} — {finalize_resp.text[:500]}")
            return None
        except requests.RequestException as e:
            logger.error(f"BookFusion finalize error: {e}")
            return None

    # ── Highlights (Obsidian API, X-Token) ──

    def fetch_highlights(self, cursor: str | None = None) -> dict:
        """Fetch one page of highlights from the Obsidian sync API."""
        resp = self.session.post(
            f'{BASE_URL}/obsidian-api/sync',
            headers={'X-Token': self.highlights_api_key, 'API-Version': '1', 'Content-Type': 'application/json'},
            json={'cursor': cursor},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def sync_all_highlights(self, db_service) -> int:
        """Paginate through all highlights and save to DB. Returns total new count.

        Response structure (from BookFusion Obsidian plugin source):
          { pages: Page[], cursor: str|null, next_sync_cursor: str|null }
        where BookPage = { type:'book', id, filename, frontmatter:str|null,
                           highlights: [{id, content, chapter_heading}], ... }
        """
        cursor = db_service.get_bookfusion_sync_cursor()
        total_new = 0

        while True:
            data = self.fetch_highlights(cursor)
            pages = data.get('pages', [])

            highlights_batch = []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                if page.get('type') != 'book':
                    continue

                book_id = page.get('id', '')
                book_title = _parse_frontmatter_title(page.get('frontmatter')) or page.get('filename', '')

                for hl in page.get('highlights', []):
                    if not isinstance(hl, dict):
                        continue
                    content = hl.get('content', '')
                    highlights_batch.append({
                        'bookfusion_book_id': book_id,
                        'highlight_id': hl.get('id', ''),
                        'content': content,
                        'chapter_heading': hl.get('chapter_heading'),
                        'book_title': book_title,
                        'highlighted_at': _parse_highlight_date(content),
                        'quote_text': _parse_highlight_quote(content),
                    })

            if highlights_batch:
                total_new += db_service.save_bookfusion_highlights(highlights_batch)

            next_cursor = data.get('next_sync_cursor')
            if not next_cursor or next_cursor == cursor:
                if next_cursor:
                    db_service.set_bookfusion_sync_cursor(next_cursor)
                break

            cursor = data.get('cursor')
            if not cursor:
                if next_cursor:
                    db_service.set_bookfusion_sync_cursor(next_cursor)
                break

        return total_new
