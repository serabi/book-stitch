import base64
import json
import logging
import os
import uuid

import requests

from src.utils.logging_utils import sanitize_exception, sanitize_log_data

logger = logging.getLogger(__name__)

SENTRY_BASE = "https://sentry.libbyapp.com"
REQUEST_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_1) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/14.0.2 Safari/605.1.15"
)


def _clamp_poll_mins(raw_value, floor=10, default=60):
    """Return LIBBY_POLL_MINS as an int, floored and defaulted."""
    try:
        return max(floor, int(str(raw_value).strip()))
    except (TypeError, ValueError):
        return default


class LibbyClient:
    """Read-only client for Libby's private sentry/passport APIs.

    Auth model: PageKeeper creates an identity chip ("chip") and shows the
    user an 8-digit code; the user enters that code in their Libby app
    (Copy To Another Device) and Libby clones their library into our chip.
    Sentry calls use Bearer auth; passport-session hosts authenticate via
    the session hash in the URL itself and must NOT receive an Authorization
    header. See docs/LIBBY.md.
    """

    def __init__(self, database_service=None, env_prefix="LIBBY"):
        self.db = database_service
        self.env_prefix = env_prefix
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._last_sync_state = None

    # ── Configuration ──────────────────────────────────────────────

    @property
    def identity_token(self) -> str | None:
        return os.environ.get(f"{self.env_prefix}_IDENTITY_TOKEN") or None

    @property
    def device_id(self) -> str | None:
        return os.environ.get(f"{self.env_prefix}_DEVICE_ID") or None

    @property
    def poll_mins(self) -> int:
        return _clamp_poll_mins(os.environ.get(f"{self.env_prefix}_POLL_MINS"))

    def is_configured(self) -> bool:
        if os.environ.get(f"{self.env_prefix}_ENABLED", "").lower() == "false":
            return False
        return bool(self.identity_token)

    # ── Settings persistence ───────────────────────────────────────

    def _persist_setting(self, key, value):
        os.environ[key] = str(value)
        if self.db is not None:
            try:
                self.db.set_setting(key, str(value))
            except Exception as e:
                logger.error("Libby: failed to persist setting %s: %s", key, e)

    def _clear_setting(self, key):
        os.environ[key] = ""
        if self.db is not None:
            try:
                self.db.set_setting(key, "")
            except Exception as e:
                logger.error("Libby: failed to clear setting %s: %s", key, e)

    def ensure_device_id(self) -> str:
        existing = self.device_id
        if existing:
            return existing
        generated = str(uuid.uuid4())
        self._persist_setting(f"{self.env_prefix}_DEVICE_ID", generated)
        logger.info("Libby: generated device id %s", sanitize_log_data(generated))
        return generated

    # ── HTTP helpers ───────────────────────────────────────────────

    def _sentry_request(self, method, path, json_body=None, bearer=True):
        headers = {}
        if bearer:
            token = self.identity_token
            if not token:
                logger.warning("Libby: no identity token for %s request", path)
                return None
            headers["Authorization"] = f"Bearer {token}"
        url = f"{SENTRY_BASE}{path}"
        try:
            return self.session.request(method, url, json=json_body, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.error("Libby: request to %s failed: %s", path, sanitize_exception(e))
            return None

    # ── Pairing / lifecycle ────────────────────────────────────────

    def begin_pairing(self) -> dict:
        """Start the pairing flow: create a fresh identity chip and generate
        an 8-digit code for the user to enter in their Libby app.

        The chip stays unpaired (no cards) until the user completes code
        entry; poll ``check_pairing()`` to detect completion.

        Returns {"success": True, "code": "12345678"} or {"success": False,
        "error": ..., "detail": ...} where error is http_error or
        network_error.
        """
        try:
            response = self.session.post(f"{SENTRY_BASE}/chip?client=dewey", timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.error("Libby: chip creation failed: %s", sanitize_exception(e))
            return {"success": False, "error": "network_error", "detail": "Could not reach Libby."}

        if response.status_code != 200:
            logger.error("Libby: chip creation returned HTTP %s", response.status_code)
            return {
                "success": False,
                "error": "http_error",
                "detail": f"Chip creation failed (HTTP {response.status_code}).",
            }

        try:
            new_token = response.json().get("identity")
        except ValueError:
            new_token = None
        if not new_token:
            return {
                "success": False,
                "error": "http_error",
                "detail": "Chip creation returned no identity.",
            }
        self._persist_setting(f"{self.env_prefix}_IDENTITY_TOKEN", new_token)

        # Code generation is a GET against the same endpoint the submit
        # direction uses; a POST returns 404 (verified live + matches
        # libby-calibre-plugin).
        generate_response = self._sentry_request("GET", "/chip/clone/code")
        if generate_response is None:
            self._clear_setting(f"{self.env_prefix}_IDENTITY_TOKEN")
            return {"success": False, "error": "network_error", "detail": "Could not reach Libby."}

        if generate_response.status_code != 200:
            self._clear_setting(f"{self.env_prefix}_IDENTITY_TOKEN")
            logger.error("Libby: clone-code generation returned HTTP %s", generate_response.status_code)
            return {
                "success": False,
                "error": "http_error",
                "detail": f"Pairing-code generation failed (HTTP {generate_response.status_code}).",
            }

        try:
            payload = generate_response.json()
        except ValueError:
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        if not code:
            self._clear_setting(f"{self.env_prefix}_IDENTITY_TOKEN")
            return {"success": False, "error": "http_error", "detail": "Libby returned no pairing code."}

        logger.info("Libby: pairing started; awaiting code entry in the Libby app")
        return {"success": True, "code": str(code)}

    def check_pairing(self) -> dict:
        """Check whether the user completed code entry in the Libby app.

        Returns {"complete": bool, "cards": [...]} — complete once the chip
        reports at least one linked card.
        """
        state = self.get_sync_state(force=True)
        cards = self._extract_cards(state or {})
        complete = bool(cards)
        if complete:
            self.ensure_device_id()
            logger.info(
                "Libby: pairing completed; %d card(s) linked (%s)",
                len(cards),
                ", ".join(sanitize_log_data(c.get("name") or "") for c in cards) or "none named",
            )
        return {"complete": complete, "cards": cards}

    def disconnect(self) -> bool:
        """Revoke the identity chip server-side.

        Returns True when revoked or when the chip was already invalid
        (401/403) — both mean safe-to-disconnect. Returns False only when
        revocation could not be confirmed (network/5xx).
        """
        if not self.identity_token:
            return True
        response = self._sentry_request("POST", "/chip/revoke")
        if response is None:
            return False
        if response.status_code == 200:
            logger.info("Libby: identity chip revoked")
            return True
        if response.status_code in (401, 403):
            logger.info("Libby: chip already invalid; treating as disconnected")
            return True
        logger.error("Libby: chip revoke returned HTTP %s", response.status_code)
        return False

    def clear_credentials(self):
        """Remove the identity token + device id from settings and env."""
        self._clear_setting(f"{self.env_prefix}_IDENTITY_TOKEN")
        self._clear_setting(f"{self.env_prefix}_DEVICE_ID")
        self._last_sync_state = None

    @staticmethod
    def _extract_cards(sync_payload: dict) -> list[dict]:
        """Normalize linked cards from any /chip payload shape."""
        raw_cards = sync_payload.get("cards") if isinstance(sync_payload, dict) else None
        if not isinstance(raw_cards, list):
            return []
        cards = []
        for card in raw_cards:
            if not isinstance(card, dict):
                continue
            card_id = card.get("id") or card.get("cardId")
            if card_id is None:
                continue
            library = card.get("library") if isinstance(card.get("library"), dict) else {}
            cards.append(
                {
                    "id": str(card_id),
                    "name": card.get("name") or card.get("niceName") or "",
                    "library": library.get("name") or "",
                    "library_key": library.get("key") or "",
                }
            )
        return cards

    # ── Data reads ─────────────────────────────────────────────────

    def get_sync_state(self, force: bool = False) -> dict | None:
        """GET /chip/sync → loans, holds, cards. Returns None on failure."""
        if not force and self._last_sync_state is not None:
            return self._last_sync_state
        response = self._sentry_request("GET", "/chip/sync")
        if response is None:
            return None
        if response.status_code != 200:
            logger.error("Libby: /chip/sync returned HTTP %s", response.status_code)
            return None
        try:
            state = response.json()
        except ValueError as e:
            logger.error("Libby: /chip/sync returned invalid JSON: %s", e)
            return None
        self._last_sync_state = state
        return state

    def get_active_loans(self) -> list[dict]:
        """Return normalized active loans from the last sync state.

        Each entry: {psn_key, card_id, title_id, title, authors, format,
        isbn, expires, library_key}.
        """
        state = self.get_sync_state()
        if not state:
            return []
        loans = []
        for loan in state.get("loans") or []:
            if not isinstance(loan, dict):
                continue
            card = loan.get("card") if isinstance(loan.get("card"), dict) else {}
            card_id = loan.get("cardId") or card.get("id")
            title_info = loan.get("title") if isinstance(loan.get("title"), dict) else {}
            title_id = loan.get("titleId") or title_info.get("id")
            if card_id is None or title_id is None:
                continue
            library = card.get("library") if isinstance(card.get("library"), dict) else {}
            loans.append(
                {
                    "psn_key": f"{card_id}-{title_id}",
                    "card_id": str(card_id),
                    "title_id": str(title_id),
                    "title": title_info.get("name") or title_info.get("title"),
                    "authors": ", ".join(
                        a.get("name", "") for a in title_info.get("authors") or [] if isinstance(a, dict)
                    ),
                    "format": title_info.get("type", {}).get("id")
                    if isinstance(title_info.get("type"), dict)
                    else None,
                    "isbn": self._extract_isbn(title_info),
                    "expires": loan.get("expires"),
                    "library_key": library.get("key") or "",
                }
            )
        return loans

    def get_holds(self) -> list[dict]:
        """Return holds from the last sync state (TBR review queue feed)."""
        state = self.get_sync_state()
        if not state:
            return []
        return [h for h in state.get("holds") or [] if isinstance(h, dict)]

    @staticmethod
    def _extract_isbn(title_info: dict) -> str | None:
        for key in ("isbn", "ISBN"):
            value = title_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for sub in ("value", "isbn"):
                    inner = value.get(sub)
                    if isinstance(inner, str) and inner.strip():
                        return inner.strip()
        return None

    def get_passport(self, card_id, title_id, library_key: str | None = None) -> dict | None:
        """Issue a reader passport for a loan; returns {urls, expires, leeway, ...}."""
        if not library_key:
            for loan in self.get_active_loans():
                if loan["card_id"] == str(card_id) and loan["title_id"] == str(title_id):
                    library_key = loan.get("library_key")
                    break
        tdata = {
            "codex": {
                "title": {"titleId": str(title_id)},
                "loan": {"psnKey": f"{card_id}-{title_id}"},
                "library": {"key": library_key or ""},
            },
            "spec": "V22",
            "locale": "en",
        }
        encoded = base64.b64encode(json.dumps(tdata).encode()).decode()
        path = f"/open/book/card/{card_id}/title/{title_id}?t={encoded}&website_id={library_key or ''}"
        response = self._sentry_request("GET", path, bearer=False)
        if response is None:
            return None
        if response.status_code != 200:
            logger.error(
                "Libby: passport request for %s returned HTTP %s",
                sanitize_log_data(f"{card_id}-{title_id}"),
                response.status_code,
            )
            return None
        try:
            return response.json()
        except ValueError as e:
            logger.error("Libby: passport response was invalid JSON: %s", e)
            return None

    def get_possession(self, possession_url: str) -> dict | None:
        """GET a passport's possession URL → position/marks/statistics.

        No Authorization header: possession hosts authenticate via the
        per-session hash embedded in the hostname.
        """
        try:
            response = self.session.get(possession_url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.error("Libby: possession request failed: %s", sanitize_exception(e))
            return None
        if response.status_code != 200:
            logger.debug("Libby: possession returned HTTP %s", response.status_code)
            return None
        try:
            return response.json()
        except ValueError as e:
            logger.error("Libby: possession returned invalid JSON: %s", e)
            return None

    def check_connection(self) -> bool:
        """Cheap authenticated ping used by settings + connection checks."""
        state = self.get_sync_state(force=True)
        connected = bool(state)
        if connected:
            cards = self._extract_cards(state)
            logger.info("Libby: connected (%d card(s))", len(cards))
        else:
            logger.warning("Libby: connection check failed")
        return connected
