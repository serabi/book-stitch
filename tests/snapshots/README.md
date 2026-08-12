# Behavior-freeze snapshots

These JSON files are **golden snapshots** that freeze the backend's externally
observable contract before the backend cleanup begins. They let aggressive
internal refactors proceed safely: if a refactor changes a route, an HTTP method
set, a UI JSON response shape, or the set of endpoints the frontend calls, the
matching test fails loudly instead of silently breaking the UI or a KOReader
device.

They were introduced in **Cleanup Stage 01** (`Cleanup 01: freeze routes and
protocol behavior`). Nothing here changes runtime behavior — it is a safety
harness only.

## What is frozen

| Snapshot file | Frozen by | What it captures |
|---|---|---|
| `route_inventory.json` | `tests/test_route_inventory.py` | Every Flask rule: pattern, declared HTTP methods (Flask's auto HEAD/OPTIONS removed), endpoint name, and a `kind` label (`page` / `api` / `protocol` / `asset`). |
| `fetch_url_inventory.json` | `tests/test_fetch_url_inventory.py` | The set of same-origin `fetch()` path prefixes the vanilla-JS frontend calls. |
| `api_status_empty.json` | `tests/test_ui_api_snapshots.py` | Exact JSON of `GET /api/status` with an empty database. |
| `api_status_populated.json` | `tests/test_ui_api_snapshots.py` | Structure (types/keys) of `GET /api/status` with one book and four client states. |
| `api_processing_status_empty.json` | `tests/test_ui_api_snapshots.py` | Exact JSON of `GET /api/processing-status` with no processing books. |
| `api_processing_status_populated.json` | `tests/test_ui_api_snapshots.py` | Structure of `GET /api/processing-status` with one pending book + job. |
| `api_suggestions_empty.json` | `tests/test_ui_api_snapshots.py` | Exact JSON of `GET /api/suggestions` with no actionable suggestions (a bare array). |
| `api_logs_keys.json` | `tests/test_ui_api_snapshots.py` | The sorted top-level keys of `GET /api/logs` (content is runtime-variable, so only the key shape is frozen). |

## Two freezing strategies

`tests/snapshot_helpers.py` provides:

* **`snapshot_value(name, value)`** — freezes the *exact* JSON. Used where the
  full response is controllable from the test `MockContainer`.
* **`snapshot_shape(name, value)`** — freezes the *structure* only: every leaf
  scalar becomes its JSON type name (`"int"`, `"str"`, `"null"`, …) and lists
  collapse to the shape of their first element. Used where content varies but
  the key/type shape is the real contract.

There is intentionally **no uniform `{success, error}` envelope** in the UI API.
Some endpoints return a bare array, some a keyed map, some `{success: ...}`.
Each snapshot captures that endpoint's *real* shape — do not "normalize" shapes
as part of cleanup; that would be a frontend-coupled change and is out of scope.

## How to update a snapshot (after a reviewed, intentional change)

If you intentionally change a route, method, or response shape **and update the
consuming JS/templates in lockstep**, regenerate the affected snapshot:

```bash
# Local (Docker test container — matches CI):
docker compose -f docker-compose.test.yml run --rm \
  -e PAGEKEEPER_UPDATE_SNAPSHOTS=1 test tests/test_route_inventory.py

# Or regenerate everything at once:
docker compose -f docker-compose.test.yml run --rm \
  -e PAGEKEEPER_UPDATE_SNAPSHOTS=1 test \
  tests/test_route_inventory.py tests/test_ui_api_snapshots.py tests/test_fetch_url_inventory.py
```

When `PAGEKEEPER_UPDATE_SNAPSHOTS=1` is set, the tests rewrite their snapshot
files and `skip` instead of asserting. Review the resulting diff carefully — a
snapshot change is a contract change. Commit the regenerated JSON alongside the
code/JS change that justifies it.

## Determinism notes

* All snapshot tests run against mocks/fixtures (no live network, no live DB
  state), so they are deterministic in CI.
* `route_inventory.json` includes both the bare and `/koreader/...` prefixed
  KOSync forms; the protocol parity itself is exercised by
  `tests/test_kosync_protocol_freeze.py`.
* `api_logs_keys.json` deliberately freezes only the top-level keys because the
  log *content* depends on the runtime log file.
