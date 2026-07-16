from pathlib import Path

import pytest
from flask import render_template


def render_navigation(flask_app, path):
    flask_app.template_folder = str(Path(__file__).parent.parent / "templates")
    with flask_app.test_request_context(path):
        return render_template("partials/navbar.html")


def test_navigation_shell_exposes_primary_and_utility_destinations(flask_app):
    html = render_navigation(flask_app, "/")

    assert '<aside class="app-sidebar"' in html
    assert '<nav class="mobile-bottom-nav"' in html
    for destination in ("/", "/reading", "/suggestions", "/settings", "/match", "/logs"):
        assert f'href="{destination}"' in html
    assert '<span class="sidebar-label">Currently Reading</span>' in html
    assert '<span>Currently</span>' in html
    assert '/static/icon.png' in html
    assert '/static/grimmory-app.png' not in html


@pytest.mark.parametrize("path", ["/suggestions", "/match", "/batch-match"])
def test_pairing_routes_share_one_active_navigation_group(flask_app, path):
    html = render_navigation(flask_app, path)

    assert 'href="/suggestions" class="sidebar-nav-link active" aria-current="page"' in html
    assert 'href="/suggestions" class="mobile-nav-link active" aria-current="page"' in html


def test_only_current_primary_destination_is_marked_active(flask_app):
    html = render_navigation(flask_app, "/reading")

    assert 'href="/reading" class="sidebar-nav-link active" aria-current="page"' in html
    assert 'href="/" class="sidebar-nav-link active"' not in html


def test_pairings_badge_counts_only_unresolved_detected_books(flask_app, mock_container):
    from src import app_template_context

    app_template_context._PAIRING_COUNT_CACHE["expires"] = 0
    mock_container.mock_database_service.get_active_detected_book_count.return_value = 3

    html = render_navigation(flask_app, "/suggestions")

    assert html.count('class="nav-badge" aria-hidden="true"') == 2
    assert html.count('aria-label="Currently Reading, 3 source activities"') == 2
    mock_container.mock_database_service.get_pending_suggestion_count.assert_not_called()


def test_mobile_pairings_badge_does_not_grow_bottom_navigation():
    css = (Path(__file__).parent.parent / "static/css/layout.css").read_text()

    assert ".mobile-nav-link {\n    position: relative;" in css
    assert ".mobile-nav-link .nav-badge {\n    position: absolute;" in css
    assert "margin: 0;" in css


def test_navigation_icons_share_one_outline_style(flask_app):
    html = render_navigation(flask_app, "/")
    css = (Path(__file__).parent.parent / "static/css/layout.css").read_text()

    for name in ("dashboard", "reading", "pairings", "settings", "add", "activity", "more"):
        assert f'id="nav-icon-{name}"' in html
    assert "stroke-width: 2;" in css
    assert 'use[href="#nav-icon-dashboard"]' not in css


def test_confirm_modal_has_dialog_semantics_and_focus_management():
    root = Path(__file__).parent.parent
    markup = (root / "templates/partials/confirm_modal.html").read_text()
    script = (root / "static/js/confirm-modal.js").read_text()

    assert 'role="dialog"' in markup
    assert 'aria-modal="true"' in markup
    assert 'aria-labelledby="pk-modal-title"' in markup
    assert "_focusableElements" in script
    assert "_opener.focus()" in script
