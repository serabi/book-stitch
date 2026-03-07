"""
Write-suppression tracker — prevents self-triggered feedback loops.

Call record_write(client_name, abs_id) after Stitch successfully pushes
progress to any client. Call is_own_write(client_name, abs_id) before acting
on a progress change from that client to suppress round-trip echoes.

Supported client_name values: 'ABS', 'Storyteller', 'BookLore', 'KoSync'
"""

import threading
import time

_recent_writes: dict[str, dict] = {}
_writes_lock = threading.Lock()

_DEFAULT_SUPPRESSION_WINDOW = 60  # seconds


def _normalize_state(state: dict | None) -> dict | None:
    if not isinstance(state, dict):
        return None
    return {
        'pct': state.get('pct'),
        'ts': state.get('ts'),
        'xpath': state.get('xpath'),
        'cfi': state.get('cfi'),
    }


def _states_match(recorded: dict | None, incoming: dict | None) -> bool:
    if not recorded or not incoming:
        return True

    recorded_pct = recorded.get('pct')
    incoming_pct = incoming.get('pct')
    if recorded_pct is not None and incoming_pct is not None and abs(recorded_pct - incoming_pct) > 0.001:
        return False

    for key in ('xpath', 'cfi'):
        recorded_val = recorded.get(key)
        incoming_val = incoming.get(key)
        if recorded_val and incoming_val and recorded_val != incoming_val:
            return False

    recorded_ts = recorded.get('ts')
    incoming_ts = incoming.get('ts')
    if recorded_ts is not None and incoming_ts is not None and abs(recorded_ts - incoming_ts) > 5:
        return False

    return True


def record_write(client_name: str, abs_id: str, state: dict | None = None) -> None:
    """Call after Stitch successfully pushes progress to a client."""
    key = f"{client_name}:{abs_id}"
    with _writes_lock:
        _recent_writes[key] = {
            'timestamp': time.time(),
            'state': _normalize_state(state),
        }


def is_own_write(client_name: str, abs_id: str, suppression_window: int = _DEFAULT_SUPPRESSION_WINDOW, state: dict | None = None) -> bool:
    """Return True if a recent progress event for this client/book was caused by our own write."""
    key = f"{client_name}:{abs_id}"
    with _writes_lock:
        last_write = _recent_writes.get(key)
        if last_write and time.time() - last_write['timestamp'] < suppression_window:
            return _states_match(last_write.get('state'), _normalize_state(state))
        # Clean up stale entries while holding the lock
        stale = [k for k, v in _recent_writes.items() if time.time() - v['timestamp'] > suppression_window]
        for k in stale:
            del _recent_writes[k]
        return False
