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
# Client version declared when acquiring a chip (mirrors dewey's
# acquireChip: c=d:<version>, s=0).
DEWEY_VERSION = "22.1.0"
# Chips are bound to the User-Agent they were created with and reader
# endpoints are gated to tokens minted by real dewey sessions — clone-code
# chips never get reader access no matter what they present (verified live
# Aug 2026). We pair by importing the user's browser-session token.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


def _looks_like_identity_token(token: str) -> bool:
    """Structural check for a dewey identity JWT (header.payload.signature,
    payload decodable, audience readiverse)."""
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return False
    try:
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(claims, dict) and claims.get("aud") == "readiverse"


def _clamp_poll_mins(raw_value, floor=10, default=60):
    """Return LIBBY_POLL_MINS as an int, floored and defaulted."""
    try:
        return max(floor, int(str(raw_value).strip()))
    except (TypeError, ValueError):
        return default


class LibbyClient:
    """Read-only client for Libby's private sentry/passport APIs.

    Two credential tiers (either or both may be present):

    - **Sync token** (`LIBBY_SYNC_TOKEN`): a chip paired via 8-digit setup
      code. Long-lived, zero-maintenance. Can read /chip/sync — loans,
      holds, cards — but is permanently denied reader access, so no
      positions.
    - **Identity token** (`LIBBY_IDENTITY_TOKEN`): a JWT copied from the
      user's logged-in libbyapp.com browser session
      (localStorage ``dewey:sentry.identity``). Full access including
      positions; expires (~7 days) and must be re-pasted.

    Sentry calls use Bearer auth; passport-session hosts authenticate via
    the per-session hash in the URL plus a one-time message handshake, and
    must NOT receive an Authorization header. See docs/LIBBY.md.
    """

    def __init__(self, database_service=None, env_prefix="LIBBY"):
        self.db = database_service
        self.env_prefix = env_prefix
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Referer": "https://libbyapp.com/",
            }
        )
        self._last_sync_state = None

    # ── Configuration ──────────────────────────────────────────────

    @property
    def identity_token(self) -> str | None:
        """Browser-session token: full access incl. positions."""
        return os.environ.get(f"{self.env_prefix}_IDENTITY_TOKEN") or None

    @property
    def sync_token(self) -> str | None:
        """Setup-code-paired chip token: /chip/sync only."""
        return os.environ.get(f"{self.env_prefix}_SYNC_TOKEN") or None

    @property
    def active_sync_token(self) -> str | None:
        """Token used for /chip/sync calls — prefer the full-access one."""
        return self.identity_token or self.sync_token

    @property
    def can_read_positions(self) -> bool:
        return bool(self.identity_token)

    @property
    def device_id(self) -> str | None:
        return os.environ.get(f"{self.env_prefix}_DEVICE_ID") or None

    @property
    def poll_mins(self) -> int:
        return _clamp_poll_mins(os.environ.get(f"{self.env_prefix}_POLL_MINS"))

    def is_configured(self) -> bool:
        if os.environ.get(f"{self.env_prefix}_ENABLED", "").lower() == "false":
            return False
        return bool(self.active_sync_token)

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

    def _sentry_request(self, method, path, json_body=None, form_body=None, bearer=True):
        headers = {}
        if bearer:
            token = self.active_sync_token
            if not token:
                logger.warning("Libby: no identity token for %s request", path)
                return None
            headers["Authorization"] = f"Bearer {token}"
        url = f"{SENTRY_BASE}{path}"
        request_kwargs = {"headers": headers, "timeout": REQUEST_TIMEOUT}
        if json_body is not None:
            request_kwargs["json"] = json_body
        elif form_body is not None:
            request_kwargs["data"] = form_body
        try:
            return self.session.request(method, url, **request_kwargs)
        except requests.RequestException as e:
            logger.error("Libby: request to %s failed: %s", path, sanitize_exception(e))
            return None

    # ── Pairing / lifecycle ────────────────────────────────────────

    def pair_with_setup_code(self, code: str) -> dict:
        """Pair a sync-only chip using an 8-digit setup code.

        Creates a fresh chip and submits the code (Sonos-style flow), giving
        PageKeeper a long-lived credential for /chip/sync — loans, holds,
        cards. Clone-code chips are permanently denied reader access
        (verified live Aug 2026), so this tier never yields positions;
        pair an identity token for those.

        Returns {"success": True, "cards": [...]} or {"success": False,
        "error", "detail"} with error ∈ {invalid_code_format,
        expired_code, revoked_token, http_error, network_error}.
        """
        clean_code = str(code or "").strip()
        if not (len(clean_code) == 8 and clean_code.isdigit()):
            return {"success": False, "error": "invalid_code_format", "detail": "Setup code must be 8 digits."}

        chip = self._create_sync_chip()
        if not chip:
            return {
                "success": False,
                "error": "http_error",
                "detail": "Could not create a Libby session — try again.",
            }
        chip_token, chip_id = chip

        clone_response = self.session.post(
            f"{SENTRY_BASE}/chip/clone/code",
            data={"code": clean_code},
            headers={"Authorization": f"Bearer {chip_token}", "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if clone_response is None:
            self._clear_setting(f"{self.env_prefix}_SYNC_TOKEN")
            return {"success": False, "error": "network_error", "detail": "Could not reach Libby."}

        if clone_response.status_code in (401, 403):
            self._clear_setting(f"{self.env_prefix}_SYNC_TOKEN")
            return {
                "success": False,
                "error": "revoked_token",
                "detail": "Libby rejected the pairing session — try again.",
            }
        if clone_response.status_code in (400, 404, 410):
            return {
                "success": False,
                "error": "expired_code",
                "detail": "That setup code was invalid or has expired — generate a fresh one and retry.",
            }
        if clone_response.status_code != 200:
            logger.error("Libby: clone-by-code returned HTTP %s", clone_response.status_code)
            return {
                "success": False,
                "error": "http_error",
                "detail": f"Pairing failed (HTTP {clone_response.status_code}).",
            }

        # Verify the cloned chip actually serves /chip/sync before keeping it.
        try:
            verify = self.session.get(
                f"{SENTRY_BASE}/chip/sync",
                headers={"Authorization": f"Bearer {chip_token}"},
                timeout=REQUEST_TIMEOUT,
            )
            state = verify.json() if verify.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            state = {}

        cards = self._extract_cards(state or {})
        if not cards:
            return {
                "success": False,
                "error": "expired_code",
                "detail": "Pairing succeeded but no cards came across — regenerate the code and retry.",
            }

        self._persist_setting(f"{self.env_prefix}_SYNC_TOKEN", chip_token)
        self.ensure_device_id()
        logger.info(
            "Libby: setup-code pairing complete; %d card(s) linked (%s)",
            len(cards),
            ", ".join(sanitize_log_data(c.get("name") or "") for c in cards) or "none named",
        )
        return {"success": True, "cards": cards}

    def _create_sync_chip(self) -> tuple[str, str] | None:
        """POST /chip for a fresh blank chip; returns (identity, chip_uuid)."""
        try:
            response = self.session.post(
                f"{SENTRY_BASE}/chip?client=dewey&c=d:{DEWEY_VERSION}&s=0",
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.error("Libby: chip creation failed: %s", sanitize_exception(e))
            return None
        if response.status_code != 200:
            logger.error("Libby: chip creation returned HTTP %s", response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        token = data.get("identity")
        chip_id = data.get("chip")
        if not token or not chip_id:
            return None
        return token, chip_id

    def pair_with_identity_token(self, token: str) -> dict:
        """Pair full reader access using an identity token copied from the
        user's logged-in libbyapp.com browser session.

        Returns {"success": True, "cards": [...]} on success, or
        {"success": False, "error": ..., "detail": ...} on failure where
        error is one of: invalid_token_format, revoked_token, network_error.
        """
        clean_token = str(token or "").strip().strip('"')
        if not _looks_like_identity_token(clean_token):
            return {
                "success": False,
                "error": "invalid_token_format",
                "detail": "That doesn't look like a Libby identity token — copy the full value of "
                "dewey:sentry.identity from your browser console.",
            }

        # Verify against /chip/sync with THIS token before accepting it.
        try:
            response = self.session.get(
                f"{SENTRY_BASE}/chip/sync",
                headers={"Authorization": f"Bearer {clean_token}"},
                timeout=REQUEST_TIMEOUT,
            )
            state = response.json() if response.status_code == 200 else None
        except (requests.RequestException, ValueError):
            state = None

        if state is None:
            return {"success": False, "error": "network_error", "detail": "Could not reach Libby."}

        if state.get("result") in ("missing_chip", "revoked") or not self._extract_cards(state):
            return {
                "success": False,
                "error": "revoked_token",
                "detail": "Libby rejected that token — it may have expired. Refresh libbyapp.com and copy a fresh one.",
            }

        self._persist_setting(f"{self.env_prefix}_IDENTITY_TOKEN", clean_token)
        cards = self._extract_cards(state)
        self.ensure_device_id()
        logger.info(
            "Libby: identity token paired; %d card(s) linked (%s)",
            len(cards),
            ", ".join(sanitize_log_data(c.get("name") or "") for c in cards) or "none named",
        )
        return {"success": True, "cards": cards}

    def disconnect(self) -> bool:
        """Revoke every paired chip server-side.

        Returns True when each present token was revoked or already invalid
        (401/403) — both mean safe-to-disconnect. Returns False only when a
        revocation could not be confirmed (network/5xx), in which case the
        caller should keep local settings intact.
        """
        ok = True
        for label, token in (
            ("identity", self.identity_token),
            ("sync", self.sync_token),
        ):
            if not token:
                continue
            try:
                response = self.session.post(
                    f"{SENTRY_BASE}/chip/revoke",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                logger.error("Libby: %s chip revoke failed: %s", label, sanitize_exception(e))
                ok = False
                continue
            if response.status_code == 200:
                logger.info("Libby: %s chip revoked", label)
            elif response.status_code in (401, 403):
                logger.info("Libby: %s chip already invalid; treating as disconnected", label)
            else:
                logger.error("Libby: %s chip revoke returned HTTP %s", label, response.status_code)
                ok = False
        return ok

    def clear_credentials(self):
        """Remove all Libby tokens + device id from settings and env."""
        self._clear_setting(f"{self.env_prefix}_IDENTITY_TOKEN")
        self._clear_setting(f"{self.env_prefix}_SYNC_TOKEN")
        self._clear_setting(f"{self.env_prefix}_DEVICE_ID")
        self._last_sync_state = None

    @staticmethod
    def _extract_cards(sync_payload: dict) -> list[dict]:
        """Normalize linked cards from a /chip payload.

        Real card shape: {cardId, cardName, advantageKey, library: {name,
        websiteId, ...}, ...}.
        """
        raw_cards = sync_payload.get("cards") if isinstance(sync_payload, dict) else None
        if not isinstance(raw_cards, list):
            return []
        cards = []
        for card in raw_cards:
            if not isinstance(card, dict):
                continue
            card_id = card.get("cardId") or card.get("id")
            if card_id is None:
                continue
            library = card.get("library") if isinstance(card.get("library"), dict) else {}
            cards.append(
                {
                    "id": str(card_id),
                    "name": card.get("cardName") or "",
                    "library": library.get("name") or "",
                    "website_id": library.get("websiteId") or "",
                    "library_key": card.get("advantageKey") or "",
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

        Real loan shape is FLAT: {id, cardId, title (string),
        firstCreatorName, type: {id}, expireDate, websiteId, ...}.

        Each entry: {psn_key, card_id, title_id, title, authors, format,
        isbn, expires, library_key, media_type}.
        """
        state = self.get_sync_state()
        if not state:
            return []
        loans = []
        for loan in state.get("loans") or []:
            if not isinstance(loan, dict):
                continue
            card_id = loan.get("cardId")
            title_id = loan.get("id")
            if card_id is None or title_id is None:
                continue
            media_type = (loan.get("type") or {}).get("id") if isinstance(loan.get("type"), dict) else None
            loans.append(
                {
                    "psn_key": f"{card_id}-{title_id}",
                    "card_id": str(card_id),
                    "title_id": str(title_id),
                    "title": loan.get("title"),
                    "authors": loan.get("firstCreatorName") or "",
                    "format": media_type,
                    # Loans carry no ISBN directly; resolved in the matching
                    # layer via metadata/spine lookups.
                    "isbn": None,
                    "expires": loan.get("expireDate") or loan.get("expires"),
                    "library_key": loan.get("websiteId") or "",
                    "media_type": self._media_type_path(media_type),
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
    def _media_type_path(media_type: str | None) -> str:
        """Map a Libby type id onto the /open/{segment}/ URL segment."""
        if media_type == "audiobook":
            return "audiobook"
        if media_type == "magazine":
            return "magazine"
        return "book"

    def get_passport(
        self,
        card_id,
        title_id,
        media_type: str = "book",
        website_id: str | None = None,
        library_name: str | None = None,
        library_key: str | None = None,
    ) -> dict | None:
        """Open a loan's reader session (a "dervish passport").

        Mirrors dewey's fetchDervishPassport: GET /open/{type}/card/
        {cardId}/title/{titleId}?t={base64 tData}&website_id={card library
        websiteId}. The tData blob carries codex context (loan psnKey,
        library), the shell root URI, and the current client spec — the
        server rejects requests whose spec/version look stale.
        """
        if not website_id or not library_key:
            for card in self._extract_cards(self.get_sync_state() or {}):
                if card["id"] == str(card_id):
                    website_id = website_id or card.get("website_id")
                    library_name = library_name or card.get("library")
                    library_key = library_key or card.get("library_key")
                    break

        segment = self._media_type_path(media_type)
        psn_key = f"{card_id}-{title_id}"
        if not self.identity_token:
            logger.warning("Libby: no identity token — reader endpoints unavailable for %s", sanitize_log_data(psn_key))
            return None
        tdata = {
            "codex": {
                "title": {"titleId": str(title_id), "format": media_type},
                "loan": {"psnKey": psn_key, "slug": psn_key},
                "library": {"key": library_key or "", "name": library_name or ""},
            },
            "dewey-url": "https://libbyapp.com",
            # Spec value seen in real browser passport traffic (LIBBY.md HARs).
            "spec": "V22",
        }
        # dewey's btoa(unescape(encodeURIComponent(json))) nets out to plain
        # UTF-8 base64 of the JSON string.
        encoded = base64.b64encode(json.dumps(tdata).encode()).decode()
        path = f"/open/{segment}/card/{card_id}/title/{title_id}?t={encoded}&website_id={website_id or ''}"
        try:
            response = self.session.get(
                f"{SENTRY_BASE}{path}",
                headers={"Authorization": f"Bearer {self.identity_token}"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.error("Libby: passport request failed: %s", sanitize_exception(e))
            return None
        if response is None:
            return None
        if response.status_code != 200:
            logger.error(
                "Libby: passport request for %s returned HTTP %s",
                sanitize_log_data(psn_key),
                response.status_code,
            )
            return None
        try:
            passport = response.json()
        except ValueError as e:
            logger.error("Libby: passport response was invalid JSON: %s", e)
            return None

        web_url = (passport.get("urls") or {}).get("web")
        message = passport.get("message")
        if web_url and message:
            try:
                self.session.head(f"{web_url}?{message}", headers={"Accept": "*/*"}, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                logger.warning("Libby: reader-session cookie handshake failed: %s", sanitize_exception(e))
        else:
            logger.warning("Libby: passport missing web url or message for cookie handshake")
        return passport

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
