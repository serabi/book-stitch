from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import pytest

from src.blueprints.helpers import EbookResult
from src.services.book_intake_service import IntakeResult


def _detected(*, source="abs", source_id="abs-1", ebook_filename=None, matches=None, detected_id=7):
    return SimpleNamespace(
        id=detected_id,
        source=source,
        source_id=source_id,
        title="Exact Book",
        author="Exact Author",
        status="detected",
        ebook_filename=ebook_filename,
        matches=matches or [],
    )


def _abs_book(abs_id="abs-1"):
    return {
        "id": abs_id,
        "media": {"metadata": {"title": "Exact Book", "authorName": "Exact Author"}, "duration": 100},
    }


def _review_query(detected, candidate_source="", candidate_source_id=""):
    return urlencode(
        {
            "search": detected.title,
            "detected_id": detected.id,
            "detected_source": detected.source,
            "detected_source_id": detected.source_id,
            "candidate_source": candidate_source,
            "candidate_source_id": candidate_source_id,
        }
    )


@pytest.fixture
def review_setup(mock_container):
    db = mock_container.mock_database_service
    db.get_book_by_kosync_id.return_value = None
    db.get_kosync_doc_by_grimmory_id.return_value = None
    db.get_book_by_ebook_filename.return_value = None
    db.get_all_books.return_value = []
    mock_container.mock_abs_service.get_audiobooks = lambda: [_abs_book()]
    return db


def test_abs_to_grimmory_review_preserves_and_preselects_exact_editions(client, mock_container, review_setup):
    detected = _detected(
        matches=[
            {
                "source_family": "grimmory",
                "source_key": "grimmory:2:exact.epub",
                "filename": "exact.epub",
                "id": "2:44",
                "title": "Exact Book",
            }
        ]
    )
    review_setup.get_detected_book.return_value = detected
    ebook = EbookResult("exact.epub", title="Exact Book", grimmory_id="2:44", source="Grimmory 2")
    query = _review_query(detected, "grimmory", "grimmory:2:exact.epub")

    with patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]):
        response = client.get(f"/match?{query}")

    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert page.count("<h1") == 1
    assert 'name="detected_source_id" value="abs-1"' in page
    assert 'name="candidate_source_id" value="grimmory:2:exact.epub"' in page
    assert 'name="audiobook_id" value="abs-1" checked' in page
    assert 'name="ebook_filename" value="exact.epub" checked' in page
    assert 'id="input_ebook_source_id" value="2:44"' in page
    assert "Pair formats" in page


@pytest.mark.parametrize(
    ("source", "source_id", "ebook_source_id"),
    [("grimmory", "default:exact.epub", "default:44"), ("kosync", "hash-exact", "")],
)
def test_ebook_to_abs_review_commits_through_intake_and_redirects_next(
    client, mock_container, review_setup, source, source_id, ebook_source_id
):
    detected = _detected(
        source=source,
        source_id=source_id,
        ebook_filename="exact.epub",
        matches=[{"source": "abs_audiobook", "abs_id": "abs-1", "title": "Exact Book"}],
    )
    review_setup.get_detected_book.return_value = detected
    ebook = EbookResult(
        "exact.epub",
        title="Exact Book",
        grimmory_id=ebook_source_id or None,
        source="Grimmory" if ebook_source_id else "Filesystem",
    )
    intake = Mock()
    intake.map_audiobook_ebook.return_value = IntakeResult(book=SimpleNamespace(id=1))
    data = {
        "search": "Exact Book",
        "detected_id": "7",
        "detected_source": source,
        "detected_source_id": source_id,
        "candidate_source": "abs_audiobook",
        "candidate_source_id": "abs-1",
        "audiobook_id": "abs-1",
        "ebook_filename": "exact.epub",
        "ebook_source_id": ebook_source_id,
    }

    with (
        patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]),
        patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake),
    ):
        response = client.post("/match", data=data)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/suggestions")
    intake.map_audiobook_ebook.assert_called_once()
    kwargs = intake.map_audiobook_ebook.call_args.kwargs
    assert kwargs["detected_source"] == source
    assert kwargs["detected_source_id"] == source_id
    assert kwargs["expected_ebook_kosync_id"] == ("hash-exact" if source == "kosync" else None)


def test_stale_detected_id_is_inline_and_preserves_identifiers(client, review_setup):
    detected = _detected(detected_id=8)
    review_setup.get_detected_book.return_value = detected

    response = client.get(f"/match?{_review_query(_detected())}")
    page = response.get_data(as_text=True)

    assert response.status_code == 409
    assert 'role="alert"' in page
    assert "no longer available" in page
    assert 'name="detected_source_id"' not in page
    assert 'href="/suggestions"' in page


def test_stale_recommended_edition_clears_candidate_and_recovers_manual_review(client, review_setup):
    detected = _detected(
        matches=[
            {
                "source_family": "grimmory",
                "source_key": "grimmory:default:gone.epub",
                "filename": "gone.epub",
                "id": "default:99",
            }
        ]
    )
    review_setup.get_detected_book.return_value = detected

    with patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[]):
        response = client.get(
            f"/match?{_review_query(detected, 'grimmory', 'grimmory:default:gone.epub')}"
        )

    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "recommended ebook was removed" in page
    assert 'name="candidate_source_id" value="grimmory:default:gone.epub"' not in page
    assert 'name="detected_source_id" value="abs-1"' in page
    assert "Pair formats" in page


def test_post_error_keeps_exact_selections_and_inline_error(client, mock_container, review_setup):
    detected = _detected(
        matches=[
            {
                "source_family": "grimmory",
                "source_key": "grimmory:default:exact.epub",
                "filename": "exact.epub",
                "id": "default:44",
            }
        ]
    )
    review_setup.get_detected_book.return_value = detected
    ebook = EbookResult("exact.epub", grimmory_id="default:44", source="Grimmory")
    intake = Mock()
    intake.map_audiobook_ebook.return_value = IntakeResult(error="Edition changed", status_code=409)
    data = {
        "search": "Exact Book",
        "detected_id": "7",
        "detected_source": "abs",
        "detected_source_id": "abs-1",
        "candidate_source": "grimmory",
        "candidate_source_id": "grimmory:default:exact.epub",
        "audiobook_id": "abs-1",
        "ebook_filename": "exact.epub",
        "ebook_source_id": "default:44",
    }

    with (
        patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]),
        patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake),
    ):
        response = client.post("/match", data=data)

    page = response.get_data(as_text=True)
    assert response.status_code == 409
    assert 'role="alert">Edition changed' in page
    assert 'name="audiobook_id" value="abs-1" checked' in page
    assert 'name="ebook_filename" value="exact.epub" checked' in page
    assert 'name="candidate_source_id" value="grimmory:default:exact.epub"' in page


def test_existing_entry_collision_requires_distinct_confirmation(client, review_setup):
    detected = _detected(
        source="kosync",
        source_id="hash-exact",
        ebook_filename="exact.epub",
        matches=[{"source": "abs_audiobook", "abs_id": "abs-1"}],
    )
    review_setup.get_detected_book.return_value = detected
    ebook = EbookResult("exact.epub", source="Filesystem")
    intake = Mock()
    intake.inspect_audiobook_ebook.return_value = IntakeResult(
        error="This ebook already belongs to another PageKeeper entry",
        status_code=409,
        conflict_code="combine_required",
        conflict_book_id=22,
        conflict_book_title="Existing Ebook",
    )

    with (
        patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]),
        patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake),
    ):
        response = client.get(f"/match?{_review_query(detected, 'abs_audiobook', 'abs-1')}")

    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "one canonical PageKeeper entry" in page
    assert "Combine existing entries and pair" in page
    assert 'name="confirm_combine" value="1"' in page
    assert 'name="combine_book_id" value="22"' in page


def test_abs_to_kosync_recommendation_passes_exact_hash_to_service(client, review_setup):
    detected = _detected(
        matches=[
            {
                "source_family": "kosync",
                "source_key": "kosync:hash-exact",
                "filename": "same.epub",
            }
        ]
    )
    review_setup.get_detected_book.return_value = detected
    intake = Mock()
    intake.map_audiobook_ebook.return_value = IntakeResult(book=SimpleNamespace(id=1))
    data = {
        "detected_id": "7",
        "detected_source": "abs",
        "detected_source_id": "abs-1",
        "candidate_source": "kosync",
        "candidate_source_id": "kosync:hash-exact",
        "audiobook_id": "abs-1",
        "ebook_filename": "same.epub",
    }

    with patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake):
        response = client.post("/match", data=data)

    assert response.status_code == 302
    assert intake.map_audiobook_ebook.call_args.kwargs["expected_ebook_kosync_id"] == "hash-exact"


def test_removed_candidate_post_recovers_with_manual_companion(client, review_setup):
    detected = _detected(matches=[])
    review_setup.get_detected_book.return_value = detected
    intake = Mock()
    intake.map_audiobook_ebook.return_value = IntakeResult(book=SimpleNamespace(id=1))
    data = {
        "detected_id": "7",
        "detected_source": "abs",
        "detected_source_id": "abs-1",
        "candidate_source": "grimmory",
        "candidate_source_id": "grimmory:default:removed.epub",
        "audiobook_id": "abs-1",
        "ebook_filename": "manual.epub",
    }

    with patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake):
        response = client.post("/match", data=data)

    assert response.status_code == 302
    assert intake.map_audiobook_ebook.call_args.kwargs["expected_ebook_kosync_id"] is None


def test_storyteller_detection_is_terminal_and_left_active(client, review_setup):
    detected = _detected(source="storyteller", source_id="story-1")
    review_setup.get_detected_book.return_value = detected

    response = client.get(f"/match?{_review_query(detected)}")
    page = response.get_data(as_text=True)

    assert response.status_code == 409
    assert "cannot yet be paired" in page
    assert 'name="detected_source_id"' not in page
    review_setup.resolve_detected_book.assert_not_called()


def test_resolved_detection_get_is_terminal_but_post_verifies_mapping_retry(client, review_setup):
    detected = _detected()
    detected.status = "resolved"
    review_setup.get_detected_book.return_value = detected

    terminal = client.get(f"/match?{_review_query(detected)}")
    assert terminal.status_code == 409
    assert 'name="detected_source_id"' not in terminal.get_data(as_text=True)

    intake = Mock()
    intake.map_audiobook_ebook.return_value = IntakeResult(book=SimpleNamespace(id=1))
    with patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake):
        retry = client.post(
            "/match",
            data={
                "detected_id": "7",
                "detected_source": "abs",
                "detected_source_id": "abs-1",
                "audiobook_id": "abs-1",
                "ebook_filename": "exact.epub",
            },
        )

    assert retry.status_code == 302
    assert retry.headers["Location"].endswith("/suggestions")


def test_legacy_match_renders_service_combine_conflict(client, mock_container, review_setup):
    intake = Mock()
    intake.map_audiobook_ebook.return_value = IntakeResult(
        error="This ebook already belongs to another PageKeeper entry",
        status_code=409,
        conflict_code="combine_required",
        conflict_book_id=22,
        conflict_book_title="Existing Ebook",
    )
    mock_container.mock_abs_service.get_audiobooks = lambda: [_abs_book()]

    with patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake):
        response = client.post(
            "/match",
            data={"audiobook_id": "abs-1", "ebook_filename": "exact.epub"},
        )

    page = response.get_data(as_text=True)
    assert response.status_code == 409
    assert "Combine existing entries and pair" in page
    assert 'name="combine_book_id" value="22"' in page


def test_match_accessibility_and_mobile_guards_are_scoped_to_touched_flow():
    root = Path(__file__).parent.parent
    template = (root / "templates/match.html").read_text()
    css = (root / "static/css/match.css").read_text()
    javascript = (root / "static/js/match.js").read_text()

    assert "<main" in template and "<fieldset" in template and "<legend" in template
    assert '<label for="match-search"' in template
    assert "<details" in template and "<summary" in template
    assert 'aria-live="polite"' in template and 'role="alert"' in template
    assert ".batch-select-card:focus-within" in css
    assert ".match-action-footer {\n    position: static;" in css
    assert "actionBtn.disabled = true" in javascript
