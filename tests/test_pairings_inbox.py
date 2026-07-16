import html
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from src.blueprints.matching_bp import _group_detected_pairings


@pytest.fixture(autouse=True)
def _use_repo_templates(flask_app):
    flask_app.template_folder = str(Path(__file__).parent.parent / "templates")


def _detected(
    source_id,
    title,
    matches,
    *,
    source=None,
    media_format=None,
    detected_id=None,
    progress=0.42,
    source_updated_at=None,
):
    source = source or ("abs" if matches else "kosync")
    return SimpleNamespace(
        id=detected_id if detected_id is not None else (7 if matches else 8),
        source=source,
        source_id=source_id,
        title=title,
        author="Reader Author",
        cover_url=None,
        progress_percentage=progress,
        last_seen_at=datetime(2026, 7, 15, tzinfo=UTC),
        source_updated_at=source_updated_at or datetime.now(UTC),
        device="Kobo" if not matches else None,
        matches=matches,
        media_format=media_format or ("audiobook" if source == "abs" else "ebook"),
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
                    "media_format": "ebook",
                },
                {
                    "source_family": "filesystem",
                    "filename": "ready-alt.epub",
                    "title": "Ready Book alternate",
                    "confidence": "medium",
                    "media_format": "ebook",
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
    assert "Library suggestions" in page and "Batch matching" in page
    assert "Ready to pair" not in page
    assert "Needs a companion" not in page
    assert "Strong match" in page
    assert "Possible match" in page
    assert "Companion options" in page
    assert "Audiobookshelf" in page and "Audiobook" in page
    assert "KoSync" in page and "Ebook" in page
    assert "42% read" in page and "Active" in page
    assert page.count('class="btn btn-secondary pairing-dismiss"') == 2
    assert 'data-source="abs" data-source-id="abs-123"' in page
    assert 'id="suggestion-search"' not in page
    assert "Active books" not in page

    review_href = re.search(r'<a class="btn btn-primary" href="([^"]+)"[^>]*>Review match</a>', page).group(1)
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
        [
            {
                "source_family": "grimmory",
                "source_key": "grimmory:2:ready.epub",
                "title": "Ready ebook",
                "media_format": "ebook",
            }
        ],
    )
    ebook = _detected("2:another.epub", "Another Book", [])
    ebook.source = "grimmory"
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = [audiobook, ebook]
    db.get_detected_book_count.return_value = 2

    with patch.dict("os.environ", {"GRIMMORY_2_LABEL": "Family Library"}):
        page = client.get("/suggestions").get_data(as_text=True)

    assert page.count("Family Library") >= 2


@pytest.mark.parametrize(
    ("companion_source", "companion_source_id", "source_key"),
    [
        ("kosync", "hash-exact", "kosync:hash-exact"),
        ("grimmory", "2:44:441", "grimmory:2:44:441"),
    ],
)
def test_explicit_high_confidence_activity_edge_renders_one_group_and_exact_review_url(
    client, mock_container, companion_source, companion_source_id, source_key
):
    match = {
        "source_family": companion_source,
        "source_key": source_key,
        "title": "Exact Book",
        "confidence": "high",
        "media_format": "ebook",
    }
    rows = [
        _detected("abs-exact", "Exact Book", [match], detected_id=11),
        _detected(
            companion_source_id,
            "Exact Book",
            [],
            source=companion_source,
            media_format="ebook",
            detected_id=12,
            progress=0.61,
        ),
    ]
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = rows
    db.get_detected_book_count.return_value = 2

    page = client.get("/suggestions").get_data(as_text=True)

    assert page.count('class="pairing-card"') == 1
    assert page.count(">Exact Book</h2>") == 1
    assert re.search(r'aria-labelledby="currently-reading-\d+"', page)
    assert page.count('class="pairing-activity"') == 2
    assert 'aria-label="Review match for Exact Book from' in page
    assert 'aria-label="Dismiss Exact Book from' in page
    review_href = re.search(r'<a class="btn btn-primary" href="([^"]+)"[^>]*>Review match</a>', page).group(1)
    query = parse_qs(urlparse(html.unescape(review_href)).query)
    assert query["detected_id"] == ["11"]
    assert query["detected_source"] == ["abs"]
    assert query["detected_source_id"] == ["abs-exact"]
    assert query["candidate_source"] == [companion_source]
    assert query["candidate_source_id"] == [source_key]


def test_grimmory_audio_candidate_review_url_carries_exact_audio_identity(client, mock_container):
    match = {
        "source_family": "grimmory",
        "source_key": "grimmory:2:10:99",
        "title": "Exact Audio",
        "confidence": "high",
        "media_format": "audiobook",
    }
    detected = _detected(
        "hash-audio",
        "Exact Book",
        [match],
        source="kosync",
        media_format="ebook",
        detected_id=13,
    )
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = [detected]
    db.get_detected_book_count.return_value = 1

    page = client.get("/suggestions").get_data(as_text=True)

    review_href = re.search(r'<a class="btn btn-primary" href="([^"]+)"[^>]*>Review match</a>', page).group(1)
    query = parse_qs(urlparse(html.unescape(review_href)).query)
    assert query["audio_source"] == ["grimmory"]
    assert query["audio_source_id"] == ["2:10:99"]


def test_same_title_without_explicit_edge_stays_separate(client, mock_container):
    rows = [
        _detected("abs-same", "Same Title", [], source="abs", media_format="audiobook", detected_id=21),
        _detected("hash-same", "Same Title", [], source="kosync", media_format="ebook", detected_id=22),
    ]
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = rows
    db.get_detected_book_count.return_value = 2

    page = client.get("/suggestions").get_data(as_text=True)

    assert page.count('class="pairing-card"') == 2


def test_currently_reading_groups_are_ordered_by_most_recent_source_activity(client, mock_container):
    older = _detected(
        "older",
        "Older Book",
        [],
        source="kosync",
        source_updated_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    newer = _detected(
        "newer",
        "Newer Book",
        [],
        source="kosync",
        source_updated_at=datetime(2026, 7, 15, tzinfo=UTC),
    )
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = [older, newer]
    db.get_detected_book_count.return_value = 2

    page = client.get("/suggestions").get_data(as_text=True)

    assert page.index("Newer Book") < page.index("Older Book")


def test_non_high_confidence_edge_stays_separate(client, mock_container):
    match = {
        "source_family": "kosync",
        "source_key": "kosync:hash-medium",
        "title": "Maybe",
        "confidence": "medium",
        "media_format": "ebook",
    }
    rows = [
        _detected("abs-medium", "Maybe", [match], detected_id=31),
        _detected("hash-medium", "Maybe", [], source="kosync", media_format="ebook", detected_id=32),
    ]
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = rows
    db.get_detected_book_count.return_value = 2

    page = client.get("/suggestions").get_data(as_text=True)

    assert page.count('class="pairing-card"') == 2


def test_group_contract_preserves_each_exact_identity_progress_and_dedupes_companions(flask_app):
    companion = {
        "source_family": "kosync",
        "source_key": "kosync:hash-contract",
        "title": "Contract Book",
        "confidence": "high",
        "media_format": "ebook",
    }
    rows = [
        _detected(
            "abs-contract",
            "Contract Book",
            [companion, dict(companion)],
            detected_id=41,
            progress=0.25,
            source_updated_at=datetime(2026, 7, 14, tzinfo=UTC),
        ),
        _detected(
            "hash-contract",
            "Contract Book",
            [],
            source="kosync",
            media_format="ebook",
            detected_id=42,
            progress=0.73,
            source_updated_at=datetime(2026, 7, 15, tzinfo=UTC),
        ),
    ]

    with flask_app.test_request_context("/suggestions"):
        grouped = _group_detected_pairings(rows)

    assert len(grouped) == 1
    assert grouped[0]["identities"] == [
        {"source": "kosync", "source_id": "hash-contract"},
        {"source": "abs", "source_id": "abs-contract"},
    ]
    assert [(activity["source"], activity["progress"]) for activity in grouped[0]["activities"]] == [
        ("kosync", 73),
        ("abs", 25),
    ]
    assert [companion["identity"] for companion in grouped[0]["companions"]] == [
        {"source": "kosync", "source_id": "hash-contract"}
    ]


def test_legacy_catalog_only_renders_in_explicit_library_view(client, mock_container):
    db = mock_container.mock_database_service
    db.get_all_actionable_suggestions.return_value = []
    db.get_active_detected_book_count.return_value = 0

    response = client.get("/suggestions?view=library")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Library Suggestions" in page
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


def test_storyteller_only_detection_offers_generic_companion_search(client, mock_container):
    detected = _detected("story-1", "Storyteller Book", [])
    detected.source = "storyteller"
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = [detected]
    db.get_detected_book_count.return_value = 1

    page = client.get("/suggestions").get_data(as_text=True)

    assert "Find companion" in page
    assert "Review match" not in page


def test_stale_and_unknown_source_activity_stays_in_single_list(client, mock_container):
    stale = _detected("abs-old", "Older Book", [{"source_family": "grimmory", "title": "Older Book"}])
    stale.source_updated_at = datetime(2020, 1, 1, tzinfo=UTC)
    unknown = _detected("hash-unknown", "Unknown Activity", [])
    unknown.source_updated_at = None
    db = mock_container.mock_database_service
    db.get_active_detected_books.return_value = [stale, unknown]
    db.get_detected_book_count.return_value = 2

    page = client.get("/suggestions").get_data(as_text=True)

    assert "Older or unknown activity" not in page
    assert "Activity time unknown" in page
    assert page.count('class="pairing-card"') == 2


def test_source_scoped_detected_dismiss_endpoint(client, mock_container):
    db = mock_container.mock_database_service
    db.dismiss_detected_book.return_value = True

    response = client.post("/api/detected/kosync/shared-id/dismiss")

    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    db.dismiss_detected_book.assert_called_once_with("shared-id", source="kosync")


def test_group_dismiss_endpoint_validates_and_dispatches_exact_identities(client, mock_container):
    db = mock_container.mock_database_service
    db.dismiss_detected_books.return_value = True
    payload = {
        "identities": [
            {"source": "abs", "source_id": "audio:1"},
            {"source": "kosync", "source_id": "hash:2"},
        ]
    }

    response = client.post("/api/detected/dismiss-group", json=payload)

    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    db.dismiss_detected_books.assert_called_once_with([("abs", "audio:1"), ("kosync", "hash:2")])


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"identities": []},
        {"identities": [{"source": "unknown", "source_id": "id"}]},
        {"identities": [{"source": "abs", "source_id": ""}]},
    ],
)
def test_group_dismiss_endpoint_rejects_invalid_identities(client, mock_container, payload):
    response = client.post("/api/detected/dismiss-group", json=payload)

    assert response.status_code == 400
    mock_container.mock_database_service.dismiss_detected_books.assert_not_called()


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
