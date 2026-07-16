import html
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
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
        source_updated_at=datetime.now(UTC),
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
    assert "42% read" in page and "Source activity" in page
    assert page.count('class="btn btn-secondary pairing-dismiss"') == 2
    assert 'data-source="abs" data-source-id="abs-123"' in page
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


def test_configured_grimmory_instance_label_is_shown(client, mock_container):
    audiobook = _detected(
        "abs-123",
        "Ready Book",
        [{"source_family": "grimmory", "source_key": "grimmory:2:ready.epub", "title": "Ready ebook"}],
    )
    ebook = _detected("2:another.epub", "Another Book", [])
    ebook.source = "grimmory"
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = [audiobook, ebook]
    db.get_detected_book_count.return_value = 2

    with patch.dict("os.environ", {"GRIMMORY_2_LABEL": "Family Library"}):
        page = client.get("/suggestions").get_data(as_text=True)

    assert page.count("Family Library") == 2


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


def test_storyteller_only_detection_does_not_offer_unsupported_review_action(client, mock_container):
    detected = _detected("story-1", "Storyteller Book", [])
    detected.source = "storyteller"
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = [detected]
    db.get_detected_book_count.return_value = 1

    page = client.get("/suggestions").get_data(as_text=True)

    assert "Pairing for this source is not available yet." in page
    assert "choose one manually" not in page
    assert "Review pairing" not in page


def test_stale_and_unknown_source_activity_is_collapsed(client, mock_container):
    stale = _detected("abs-old", "Older Book", [{"source_family": "grimmory", "title": "Older Book"}])
    stale.source_updated_at = datetime(2020, 1, 1, tzinfo=UTC)
    unknown = _detected("hash-unknown", "Unknown Activity", [])
    unknown.source_updated_at = None
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = [stale, unknown]
    db.get_detected_book_count.return_value = 2

    page = client.get("/suggestions").get_data(as_text=True)

    assert "Older or unknown activity (2)" in page
    assert "more than 30 days old or has no timestamp" in page
    assert "Source activity time unknown" in page


def test_source_scoped_detected_dismiss_endpoint(client, mock_container):
    db = mock_container.mock_database_service
    db.dismiss_detected_book.return_value = True

    response = client.post("/api/detected/kosync/shared-id/dismiss")

    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    db.dismiss_detected_book.assert_called_once_with("shared-id", source="kosync")


def test_mobile_focus_clearance_is_scoped_to_pairings():
    css = (Path(__file__).parent.parent / "static/css/suggestions.css").read_text()

    assert "scroll-padding-bottom: 84px" in css
    assert ".pairings-inbox button" in css
    assert "scroll-margin-block: 12px 84px" in css


def test_advanced_library_controls_have_programmatic_labels(client, mock_container):
    mock_container.mock_database_service.get_all_actionable_suggestions.return_value = []
    mock_container.mock_bookfusion_client.is_configured.return_value = True

    page = client.get("/suggestions?view=library").get_data(as_text=True)

    assert 'for="suggestion-search"' in page
    assert 'for="confidence-filter"' in page
    assert 'for="bookfusion-filter"' in page
