from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import pytest

from src.blueprints.helpers import EbookResult
from src.services.book_intake_service import IntakeResult


def _detected(*, source="abs", source_id="abs-1", ebook_filename=None, matches=None, detected_id=7, media_format=None):
    return SimpleNamespace(
        id=detected_id,
        source=source,
        source_id=source_id,
        title="Exact Book",
        author="Exact Author",
        status="detected",
        ebook_filename=ebook_filename,
        matches=matches or [],
        media_format=media_format or ("audiobook" if source == "abs" else "ebook"),
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
def review_setup(mock_container, monkeypatch):
    db = mock_container.mock_database_service
    db.get_book_by_kosync_id.return_value = None
    db.get_kosync_doc_by_grimmory_id.return_value = None
    db.get_book_by_ebook_filename.return_value = None
    db.get_all_books.return_value = []
    mock_container.mock_abs_service.get_audiobooks = lambda: [_abs_book()]
    monkeypatch.setattr(
        "src.blueprints.matching_bp.get_kosync_id_for_ebook",
        lambda filename, *args, **kwargs: "hash-exact" if filename in {"exact.epub", "same.epub"} else None,
    )
    return db


def test_abs_to_grimmory_review_preserves_and_preselects_exact_editions(client, mock_container, review_setup):
    detected = _detected(
        matches=[
            {
                "source_family": "grimmory",
                "source_key": "grimmory:2:44:441",
                "filename": "exact.epub",
                "id": "2:44:441",
                "title": "Exact Book",
                "media_format": "ebook",
            }
        ]
    )
    review_setup.get_detected_book.return_value = detected
    ebook = EbookResult("exact.epub", title="Exact Book", grimmory_id="2:44:441", source="Grimmory 2")
    query = _review_query(detected, "grimmory", "grimmory:2:44:441")

    with patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]):
        response = client.get(f"/match?{query}")

    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert page.count("<h1") == 1
    assert 'name="detected_source_id" value="abs-1"' in page
    assert 'name="candidate_source_id" value="grimmory:2:44:441"' in page
    assert 'name="audiobook_id" value="abs-1"' in page
    assert 'name="ebook_filename" value="exact.epub"' in page
    assert 'id="input_ebook_source_id" value="2:44:441"' in page
    assert "Review match" in page
    assert "Link formats" in page
    assert "Search books" not in page
    assert "Storyteller" not in page


def test_grimmory_audiobook_review_revalidates_exact_identity_before_render(client, mock_container, review_setup):
    detected = _detected(
        source="grimmory",
        source_id="2:10:99",
        media_format="audiobook",
        matches=[
            {
                "source_family": "kosync",
                "source_key": "kosync:hash-exact",
                "filename": "exact.epub",
                "title": "Exact Book",
                "media_format": "ebook",
            }
        ],
    )
    review_setup.get_detected_book.return_value = detected
    mock_container.mock_grimmory_client.find_audiobook_by_source_id.return_value = {
        "id": 10,
        "bookFileId": 99,
        "title": "Exact Book Audio",
    }
    ebook = EbookResult("exact.epub", title="Exact Book Ebook", source="Filesystem")

    with patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]):
        response = client.get(f"/match?{_review_query(detected, 'kosync', 'kosync:hash-exact')}")

    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Started audiobook" in page
    assert "Exact Book Audio" in page
    assert 'name="audio_source" value="grimmory"' in page
    assert 'name="audio_source_id" value="2:10:99"' in page
    mock_container.mock_grimmory_client.find_audiobook_by_source_id.assert_called_with("2:10:99")


def test_review_rejects_ebook_when_kosync_identity_changed(client, mock_container, review_setup):
    detected = _detected(
        source="grimmory",
        source_id="2:10:99",
        media_format="audiobook",
        matches=[
            {
                "source_family": "kosync",
                "source_key": "kosync:hash-exact",
                "filename": "exact.epub",
                "title": "Exact Book",
                "media_format": "ebook",
            }
        ],
    )
    review_setup.get_detected_book.return_value = detected
    mock_container.mock_grimmory_client.find_audiobook_by_source_id.return_value = {
        "id": 10,
        "bookFileId": 99,
        "title": "Exact Book Audio",
    }

    with (
        patch(
            "src.blueprints.matching_bp.get_searchable_ebooks",
            return_value=[EbookResult("exact.epub", title="Exact Book Ebook", source="Filesystem")],
        ),
        patch("src.blueprints.matching_bp.get_kosync_id_for_ebook", return_value="different-hash"),
    ):
        response = client.get(f"/match?{_review_query(detected, 'kosync', 'kosync:hash-exact')}")

    page = response.get_data(as_text=True)
    assert response.status_code == 409
    assert "no longer matches the detected edition" in page
    assert "Exact editions verified" not in page


def test_manual_companion_picker_keeps_started_grimmory_audiobook(client, mock_container, review_setup):
    detected = _detected(
        source="grimmory",
        source_id="2:10:99",
        media_format="audiobook",
        matches=[],
    )
    review_setup.get_detected_book.return_value = detected
    mock_container.mock_grimmory_client.find_audiobook_by_source_id.return_value = {
        "id": 10,
        "bookFileId": 99,
        "title": "Exact Book Audio",
    }
    suggestion_service = Mock()
    suggestion_service.find_companion_candidates.return_value = [
        {
            "source_family": "grimmory",
            "source_key": "grimmory:2:44:441",
            "id": "2:44:441",
            "filename": "exact.epub",
            "title": "Exact Book Ebook",
            "author": "Exact Author",
            "media_format": "ebook",
        }
    ]
    mock_container.suggestion_service = lambda: suggestion_service

    chooser = client.get(f"/match?{_review_query(detected)}")
    chooser_page = chooser.get_data(as_text=True)

    assert chooser.status_code == 200
    assert "Started audiobook" in chooser_page
    assert "Exact Book Ebook" in chooser_page
    assert "candidate_source_id=grimmory:2:44:441" in chooser_page
    assert 'name="detected_source_id" value="2:10:99"' in chooser_page
    assert "Search books" not in chooser_page

    ebook = EbookResult(
        "exact.epub",
        title="Exact Book Ebook",
        grimmory_id="2:44:441",
        source="Grimmory 2",
    )
    with patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]):
        selected = client.get(f"/match?{_review_query(detected, 'grimmory', 'grimmory:2:44:441')}")

    selected_page = selected.get_data(as_text=True)
    assert selected.status_code == 200
    assert 'name="audio_source_id" value="2:10:99"' in selected_page
    assert 'value="2:44:441"' in selected_page
    assert "Exact editions verified" in selected_page


@pytest.mark.parametrize(
    ("detected_source", "detected_source_id", "ebook_source_id"),
    [("kosync", "hash-exact", None), ("grimmory", "default:44:441", "default:44:441")],
)
def test_ebook_to_grimmory_audio_review_ignores_hidden_identity_and_uses_exact_stored_match(
    client, mock_container, review_setup, detected_source, detected_source_id, ebook_source_id
):
    detected = _detected(
        source=detected_source,
        source_id=detected_source_id,
        ebook_filename="exact.epub",
        media_format="ebook",
        matches=[
            {
                "source_family": "grimmory",
                "source_key": "grimmory:2:10:99",
                "title": "Exact Book Audio",
                "media_format": "audiobook",
            }
        ],
    )
    review_setup.get_detected_book.return_value = detected
    mock_container.mock_grimmory_client.find_audiobook_by_source_id.return_value = {
        "id": 10,
        "bookFileId": 99,
        "title": "Exact Book Audio",
    }
    ebook = EbookResult(
        "exact.epub",
        title="Exact Book Ebook",
        grimmory_id=ebook_source_id,
        source="Grimmory" if ebook_source_id else "Filesystem",
    )
    intake = Mock()
    intake.link_grimmory_audiobook_ebook.return_value = IntakeResult(book=SimpleNamespace(id=1))
    data = {
        "detected_id": "7",
        "detected_source": detected_source,
        "detected_source_id": detected_source_id,
        "candidate_source": "grimmory",
        "candidate_source_id": "grimmory:2:10:99",
        "audio_source": "grimmory",
        "audio_source_id": "evil:changed:identity",
        "audiobook_id": "evil:changed:identity",
        "ebook_filename": "wrong.epub",
        "ebook_source_id": "evil:44",
    }

    with (
        patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]),
        patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake),
    ):
        response = client.post("/match", data=data)

    assert response.status_code == 302
    intake.map_audiobook_ebook.assert_not_called()
    intake.link_grimmory_audiobook_ebook.assert_called_once_with(
        audio_source_id="2:10:99",
        ebook_filename="exact.epub",
        ebook_source_id=ebook_source_id,
        expected_ebook_kosync_id="hash-exact" if detected_source == "kosync" else None,
        detected_source=detected_source,
        detected_source_id=detected_source_id,
    )


@pytest.mark.parametrize(
    ("source", "source_id", "ebook_source_id"),
    [("grimmory", "default:44:441", "default:44:441"), ("kosync", "hash-exact", "")],
)
def test_ebook_to_abs_review_commits_through_intake_and_redirects_next(
    client, mock_container, review_setup, source, source_id, ebook_source_id
):
    detected = _detected(
        source=source,
        source_id=source_id,
        ebook_filename="exact.epub",
        matches=[
            {
                "source": "abs_audiobook",
                "source_key": "abs:abs-1",
                "abs_id": "abs-1",
                "title": "Exact Book",
                "media_format": "audiobook",
            }
        ],
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
        "candidate_source_id": "abs:abs-1",
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
                "source_key": "grimmory:default:99:999",
                "filename": "gone.epub",
                "id": "default:99:999",
                "media_format": "ebook",
            }
        ]
    )
    review_setup.get_detected_book.return_value = detected

    with patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[]):
        response = client.get(f"/match?{_review_query(detected, 'grimmory', 'grimmory:default:99:999')}")

    page = response.get_data(as_text=True)
    assert response.status_code == 409
    assert "selected ebook edition is no longer available" in page
    assert 'name="candidate_source_id" value="grimmory:default:99:999"' not in page
    assert 'name="detected_source_id" value="abs-1"' in page
    assert "Choose its companion" in page


def test_post_error_keeps_exact_selections_and_inline_error(client, mock_container, review_setup):
    detected = _detected(
        matches=[
            {
                "source_family": "grimmory",
                "source_key": "grimmory:default:44:441",
                "filename": "exact.epub",
                "id": "default:44:441",
                "media_format": "ebook",
            }
        ]
    )
    review_setup.get_detected_book.return_value = detected
    ebook = EbookResult("exact.epub", grimmory_id="default:44:441", source="Grimmory")
    intake = Mock()
    intake.map_audiobook_ebook.return_value = IntakeResult(error="Edition changed", status_code=409)
    data = {
        "search": "Exact Book",
        "detected_id": "7",
        "detected_source": "abs",
        "detected_source_id": "abs-1",
        "candidate_source": "grimmory",
        "candidate_source_id": "grimmory:default:44:441",
        "audiobook_id": "abs-1",
        "ebook_filename": "exact.epub",
        "ebook_source_id": "default:44:441",
    }

    with (
        patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]),
        patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake),
    ):
        response = client.post("/match", data=data)

    page = response.get_data(as_text=True)
    assert response.status_code == 409
    assert 'role="alert">Edition changed' in page
    assert 'name="audiobook_id" value="abs-1"' in page
    assert 'name="ebook_filename" value="exact.epub"' in page
    assert 'name="candidate_source_id" value="grimmory:default:44:441"' in page


def test_existing_entry_collision_requires_distinct_confirmation(client, review_setup):
    detected = _detected(
        source="kosync",
        source_id="hash-exact",
        ebook_filename="exact.epub",
        matches=[
            {
                "source": "abs_audiobook",
                "source_key": "abs:abs-1",
                "abs_id": "abs-1",
                "media_format": "audiobook",
            }
        ],
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
        response = client.get(f"/match?{_review_query(detected, 'abs_audiobook', 'abs:abs-1')}")

    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "one canonical PageKeeper entry" in page
    assert "Combine existing entries and link" in page
    assert 'name="confirm_combine" value="1"' in page
    assert 'name="combine_book_id" value="22"' in page


def test_abs_to_kosync_recommendation_passes_exact_hash_to_service(client, review_setup):
    detected = _detected(
        matches=[
            {
                "source_family": "kosync",
                "source_key": "kosync:hash-exact",
                "filename": "same.epub",
                "media_format": "ebook",
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

    ebook = EbookResult("same.epub", source="Filesystem")
    with (
        patch("src.blueprints.matching_bp.get_searchable_ebooks", return_value=[ebook]),
        patch("src.blueprints.matching_bp._get_book_intake_service", return_value=intake),
    ):
        response = client.post("/match", data=data)

    assert response.status_code == 302
    assert intake.map_audiobook_ebook.call_args.kwargs["expected_ebook_kosync_id"] == "hash-exact"


def test_removed_candidate_post_does_not_trust_hidden_companion(client, review_setup):
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

    assert response.status_code == 409
    intake.map_audiobook_ebook.assert_not_called()


def test_storyteller_detection_is_terminal_and_left_active(client, review_setup):
    detected = _detected(source="storyteller", source_id="story-1")
    review_setup.get_detected_book.return_value = detected

    response = client.get(f"/match?{_review_query(detected)}")
    page = response.get_data(as_text=True)

    assert response.status_code == 409
    assert "cannot yet be paired" in page
    assert 'name="detected_source_id"' not in page
    review_setup.resolve_detected_book.assert_not_called()


def test_resolved_detection_get_is_terminal_and_post_requires_stored_companion(client, review_setup):
    detected = _detected()
    detected.status = "resolved"
    review_setup.get_detected_book.return_value = detected

    terminal = client.get(f"/match?{_review_query(detected)}")
    assert terminal.status_code == 409
    assert 'name="detected_source_id"' not in terminal.get_data(as_text=True)

    intake = Mock()
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

    assert retry.status_code == 409
    intake.map_audiobook_ebook.assert_not_called()


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
    assert "radio.addEventListener('change'" in javascript
    assert "Linking selected formats…" in javascript
    assert 'onclick="selectItem' not in template
