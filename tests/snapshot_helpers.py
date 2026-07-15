"""Reusable JSON snapshot helpers for behavior-freeze tests.

Two freezing strategies are provided, because the UI endpoints differ in how
deterministic their responses are against the test ``MockContainer``:

* ``snapshot_value`` — freeze the *exact* JSON. Use for endpoints whose full
  response is controllable from mocks (e.g. an empty-database ``/api/status``).
* ``snapshot_shape`` — freeze the *structure* only: every leaf scalar is
  replaced by its JSON type name (``"int"``, ``"str"``, ``"null"`` …) and lists
  are collapsed to the shape of their first element. Use for endpoints whose
  content varies at runtime (e.g. ``/api/logs`` reads a live log file) but whose
  shape is the actual frontend contract.

Both compare against checked-in files under ``tests/snapshots/`` and regenerate
when ``PAGEKEEPER_UPDATE_SNAPSHOTS=1`` is set. See ``tests/snapshots/README.md``.
"""

import json
import os
from pathlib import Path

import pytest

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

_UPDATE = os.environ.get("PAGEKEEPER_UPDATE_SNAPSHOTS") == "1"


def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    raise TypeError(f"Unexpected scalar type: {type(value)!r}")


def shape_of(value):
    """Return a structure mirroring ``value`` with scalars replaced by type names.

    Dicts keep their keys (sorted on serialization by ``json.dumps``). Lists are
    collapsed to ``[shape_of(first_element)]`` or ``[]`` so element count does
    not cause spurious drift; the element *shape* is still frozen.
    """
    if isinstance(value, dict):
        return {key: shape_of(value[key]) for key in value}
    if isinstance(value, list):
        return [shape_of(value[0])] if value else []
    return _type_name(value)


def _snapshot_path(name):
    return SNAPSHOT_DIR / f"{name}.json"


def _compare_or_update(name, payload):
    path = _snapshot_path(name)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    if _UPDATE:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized)
        pytest.skip(f"Snapshot {name!r} regenerated")

    assert path.exists(), (
        f"Missing snapshot {path}. Regenerate with PAGEKEEPER_UPDATE_SNAPSHOTS=1."
    )

    expected = json.loads(path.read_text())
    assert payload == expected, (
        f"Snapshot drift for {name!r}.\n"
        f"Expected: {json.dumps(expected, indent=2, sort_keys=True)}\n"
        f"Actual:   {json.dumps(payload, indent=2, sort_keys=True)}\n"
        "If this change is intentional and the consuming JS was updated in "
        "lockstep, regenerate with PAGEKEEPER_UPDATE_SNAPSHOTS=1."
    )


def snapshot_value(name, value):
    """Freeze the exact JSON value against ``tests/snapshots/<name>.json``."""
    _compare_or_update(name, value)


def snapshot_shape(name, value):
    """Freeze only the structure (types/keys) of ``value``."""
    _compare_or_update(name, shape_of(value))
