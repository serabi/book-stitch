"""Behavior-freeze: Flask route inventory snapshot.

This test captures the full set of Flask URL rules (rule pattern, HTTP
methods, and endpoint name) and compares it against a checked-in snapshot at
``tests/snapshots/route_inventory.json``.

Purpose: during the backend cleanup, refactors must not silently add, remove,
rename, or change the method set of any route. The frontend (Jinja shells +
vanilla JS) and external KOReader devices depend on the exact URL surface; a
disappearing route or a changed method set is a contract break.

To intentionally update the snapshot after a *reviewed* route change, run::

    PAGEKEEPER_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_route_inventory.py

See ``tests/snapshots/README.md`` for the full workflow.
"""

import json
import os
from pathlib import Path

import pytest

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "route_inventory.json"

# Flask adds HEAD and OPTIONS automatically. Including them makes the snapshot
# noisier without protecting any real contract, so we drop them and freeze only
# the methods a handler explicitly declares.
_AUTO_METHODS = {"HEAD", "OPTIONS"}

# The static-file route is provided by Flask itself and is not part of the app
# contract under test.
_IGNORED_ENDPOINTS = {"static"}


def _build_inventory(app):
    """Return a deterministic, JSON-serializable route inventory.

    Each entry is ``{"rule", "methods", "endpoint", "kind"}`` where ``methods``
    is a sorted list with Flask's automatic HEAD/OPTIONS removed and ``kind``
    annotates the route as a UI page, a protocol (KOSync) route, or a JSON/API
    route to make review easier.
    """
    inventory = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint in _IGNORED_ENDPOINTS:
            continue
        methods = sorted((rule.methods or set()) - _AUTO_METHODS)
        inventory.append(
            {
                "rule": str(rule),
                "methods": methods,
                "endpoint": rule.endpoint,
                "kind": _classify(str(rule)),
            }
        )
    inventory.sort(key=lambda entry: (entry["rule"], entry["endpoint"]))
    return inventory


def _classify(rule):
    """Annotate a route as protocol / page / api for reviewer convenience.

    This is a labeling aid only; the assertion compares the whole structure, so
    a misclassification still fails loudly rather than hiding drift.
    """
    # The KOSync admin dashboard routes live under /api/ and are UI JSON, not
    # the device-facing protocol — classify by path first so they read as "api".
    # /readyz is the Docker HEALTHCHECK readiness probe, also JSON.
    if rule.startswith("/api/") or rule == "/readyz":
        return "api"
    # Device-facing KOSync protocol routes: the /koreader/* forms plus the bare
    # forms KOReader speaks. Matched by path so UI shells like /kosync-documents
    # (a Jinja page) are not mislabeled.
    _protocol_bare = {
        "/healthcheck",
        "/users/auth",
        "/users/create",
        "/users/login",
        "/syncs/progress",
        "/syncs/progress/<doc_id>",
    }
    if rule.startswith("/koreader/") or rule in _protocol_bare:
        return "protocol"
    if rule.startswith("/static") or rule.startswith("/covers"):
        return "asset"
    # Routes that render a Jinja shell or perform a form POST/redirect.
    return "page"


@pytest.fixture()
def route_inventory(flask_app):
    return _build_inventory(flask_app)


def test_route_inventory_matches_snapshot(route_inventory):
    """Fail if a route disappears, is added, is renamed, or changes methods."""
    if os.environ.get("PAGEKEEPER_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(route_inventory, indent=2) + "\n")
        pytest.skip("Route inventory snapshot regenerated")

    assert SNAPSHOT_PATH.exists(), (
        f"Missing route inventory snapshot at {SNAPSHOT_PATH}. Regenerate with PAGEKEEPER_UPDATE_SNAPSHOTS=1."
    )

    expected = json.loads(SNAPSHOT_PATH.read_text())

    expected_by_endpoint = {(e["rule"], e["endpoint"]): e for e in expected}
    actual_by_endpoint = {(e["rule"], e["endpoint"]): e for e in route_inventory}

    missing = sorted(set(expected_by_endpoint) - set(actual_by_endpoint))
    added = sorted(set(actual_by_endpoint) - set(expected_by_endpoint))

    assert not missing, (
        "Routes disappeared from the app (contract break). "
        f"Missing (rule, endpoint): {missing}. "
        "If intentional, update the consuming JS/templates and regenerate the "
        "snapshot with PAGEKEEPER_UPDATE_SNAPSHOTS=1."
    )
    assert not added, (
        "New routes appeared that are not in the frozen snapshot. "
        f"Added (rule, endpoint): {added}. "
        "If intentional, regenerate the snapshot with PAGEKEEPER_UPDATE_SNAPSHOTS=1."
    )

    method_drift = []
    for key, expected_entry in expected_by_endpoint.items():
        actual_entry = actual_by_endpoint[key]
        if expected_entry["methods"] != actual_entry["methods"]:
            method_drift.append(
                {
                    "rule": key[0],
                    "endpoint": key[1],
                    "expected_methods": expected_entry["methods"],
                    "actual_methods": actual_entry["methods"],
                }
            )

    assert not method_drift, (
        "HTTP method set changed for existing routes (contract break): "
        f"{method_drift}. If intentional, regenerate the snapshot."
    )


def test_known_contract_routes_present(route_inventory):
    """Guard a hand-picked set of high-value routes by hard-coded expectation.

    This is a second, independent layer that does not depend on the snapshot
    file: even if someone regenerates the snapshot to bless a deletion, this
    test still fails for the routes the frontend and KOReader devices cannot
    live without.
    """
    rules = {entry["rule"] for entry in route_inventory}

    # UI JSON endpoints the vanilla JS polls/calls.
    ui_api = {
        "/api/status",
        "/api/processing-status",
        "/api/suggestions",
        "/api/logs",
    }
    # KOSync protocol endpoints external KOReader devices speak (both forms).
    protocol = {
        "/healthcheck",
        "/koreader/healthcheck",
        "/users/auth",
        "/koreader/users/auth",
        "/syncs/progress",
        "/koreader/syncs/progress",
        "/syncs/progress/<doc_id>",
        "/koreader/syncs/progress/<doc_id>",
    }
    # Jinja page shells.
    pages = {"/", "/reading", "/suggestions", "/settings", "/logs"}

    for expected in ui_api | protocol | pages:
        assert expected in rules, f"Required route {expected!r} is missing from the app"
