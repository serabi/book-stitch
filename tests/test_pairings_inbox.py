import html
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest


@pytest.fixture(autouse=True)
def _use_repo_templates(flask_app):
    flask_app.template_folder = str(Path(__file__).parent.parent / "templates")


def _detected(source_id, title, matches):
    return SimpleNamespace(
        id=7 if matches else 8,
        source="abs" if matches else "kosync",
        source_id=source_id,
        title=title,
        author="Reader Author",
        cover_url=None,
        progress_percentage=0.42,
        last_seen_at=datetime(2026, 7, 15, tzinfo=UTC),
        device="Kobo" if not matches else None,
        matches=matches,
    )


def test_currently_reading_is_default_and_carries_pairing_identifiers(client, mock_container):
    rows = [
        _detected(
            "abs-123",
            "Ready Book",
            [
                {
                    "source_family": "grimmory",
                    "source_key": "grimmory:2:ready.epub",
                    "title": "Ready Book ebook",
                    "author": "Reader Author",
                    "confidence": "high",
                },
                {
                    "source_family": "filesystem",
                    "filename": "ready-alt.epub",
                    "title": "Ready Book alternate",
                    "confidence": "medium",
                },
            ],
        ),
        _detected("hash-456", "Lonely Book", []),
    ]
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = rows
    db.get_detected_book_count.return_value = 2
    db.get_active_detected_book_count.return_value = 2

    response = client.get("/suggestions")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert page.count("<h1") == 1
    assert "Currently Reading" in page
    assert "Ready to pair" in page
    assert "Needs a companion" in page
    assert "Strong match" in page
    assert "Possible match" in page
    assert "1 alternative" in page
    assert "Audiobookshelf" in page and "Audiobook" in page
    assert "KoSync" in page and "Ebook" in page
    assert "42% read" in page and "Last seen Jul 15, 2026" in page
    assert 'id="suggestion-search"' not in page
    assert "Active books" not in page

    review_href = re.search(r'<a class="btn btn-primary" href="([^"]+)">Review pairing</a>', page).group(1)
    query = parse_qs(urlparse(html.unescape(review_href)).query)
    assert query["detected_id"] == ["7"]
    assert query["detected_source"] == ["abs"]
    assert query["detected_source_id"] == ["abs-123"]
    assert query["candidate_source"] == ["grimmory"]
    assert query["candidate_source_id"] == ["grimmory:2:ready.epub"]
    assert query["abs_id"] == ["abs-123"]


def test_legacy_catalog_only_renders_in_explicit_library_view(client, mock_container):
    db = mock_container.mock_database_service
    db.get_all_actionable_suggestions.return_value = []
    db.get_active_detected_book_count.return_value = 0

    response = client.get("/suggestions?view=library")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Pairing Suggestions" in page
    assert 'id="suggestion-search"' in page
    assert "Currently Reading</h1>" not in page
    db.get_active_detected_books.assert_not_called()


def test_currently_reading_empty_states_use_existing_configuration(client, mock_container):
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = []
    db.get_active_detected_book_count.return_value = 0

    db.get_detected_book_count.return_value = 0
    configured = client.get("/suggestions").get_data(as_text=True)
    assert "Nothing found yet" in configured

    mock_container.mock_abs_service.is_available = lambda: False
    unconfigured = client.get("/suggestions").get_data(as_text=True)
    assert "Connect a reading source" in unconfigured

    db.get_detected_book_count.return_value = 3
    resolved = client.get("/suggestions").get_data(as_text=True)
    assert "All caught up" in resolved
