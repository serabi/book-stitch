"""Behavior-freeze: inventory of ``fetch()`` URLs the frontend JS calls.

The vanilla-JS files in ``static/js`` are the canonical list of backend
endpoints the UI depends on. This test extracts every ``fetch(...)`` URL,
normalizes it to a stable path prefix (dropping query strings and JS template
interpolations), and freezes the resulting set against
``tests/snapshots/fetch_url_inventory.json``.

Why this matters for cleanup: the route inventory test freezes the *server*
side; this freezes the *client* side. Together they catch a refactor that drops
a route the JS still calls, or a JS edit that silently abandons an endpoint —
either of which is a contract drift worth a deliberate review.

To regenerate after a *reviewed* frontend change::

    PAGEKEEPER_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_fetch_url_inventory.py

See ``tests/snapshots/README.md``.
"""

import json
import os
import re
from pathlib import Path

import pytest

JS_DIR = Path(__file__).parent.parent / "static" / "js"
SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "fetch_url_inventory.json"

# Matches the first string-literal argument of a fetch( call, across the three
# JS quote styles. Only the leading literal segment is captured; template
# interpolations (${...}) terminate the literal and are normalized away below.
_FETCH_RE = re.compile(r"""fetch\(\s*[`'"]([^`'"$]*)""")


def _normalize(url):
    """Reduce a raw fetch literal to a stable, comparable path prefix.

    * strip any query string (``?...``)
    * keep the leading path only (interpolations already truncated by the regex)
    * collapse a trailing ``/`` that precedes a now-removed ``${id}`` segment
    """
    url = url.split("?", 1)[0]
    return url.rstrip()


def _extract_fetch_urls():
    urls = set()
    for js_file in sorted(JS_DIR.glob("*.js")):
        text = js_file.read_text(encoding="utf-8")
        for match in _FETCH_RE.finditer(text):
            literal = match.group(1)
            if not literal.startswith("/"):
                # Skip non-path fetches (e.g. full URLs handled elsewhere); the
                # contract we freeze is same-origin path prefixes.
                continue
            urls.add(_normalize(literal))
    return sorted(u for u in urls if u)


def test_fetch_url_inventory_matches_snapshot():
    """Fail if the set of frontend fetch() path prefixes changes."""
    actual = _extract_fetch_urls()

    if os.environ.get("PAGEKEEPER_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(actual, indent=2) + "\n")
        pytest.skip("Fetch URL inventory snapshot regenerated")

    assert SNAPSHOT_PATH.exists(), (
        f"Missing fetch URL inventory snapshot at {SNAPSHOT_PATH}. "
        "Regenerate with PAGEKEEPER_UPDATE_SNAPSHOTS=1."
    )

    expected = json.loads(SNAPSHOT_PATH.read_text())
    missing = sorted(set(expected) - set(actual))
    added = sorted(set(actual) - set(expected))

    assert not missing, (
        f"Frontend stopped calling these endpoint prefixes: {missing}. "
        "If intentional, regenerate the snapshot with PAGEKEEPER_UPDATE_SNAPSHOTS=1."
    )
    assert not added, (
        f"Frontend started calling new endpoint prefixes: {added}. "
        "If intentional, regenerate the snapshot with PAGEKEEPER_UPDATE_SNAPSHOTS=1."
    )
