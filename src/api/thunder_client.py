"""Public OverDrive Thunder metadata API — unauthenticated catalog lookups."""

import logging

import requests

logger = logging.getLogger(__name__)

THUNDER_BASE = "https://thunder.api.overdrive.com/v2"
REQUEST_TIMEOUT = 10


class ThunderClient:
    def get_media(self, title_id) -> dict | None:
        """Catalog metadata for a title: title, authors, ISBN identifiers,
        covers. Public endpoint, no auth."""
        try:
            response = requests.get(f"{THUNDER_BASE}/media/{title_id}", timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.error("Thunder: media fetch failed for %s: %s", title_id, e)
            return None
        if response.status_code != 200:
            logger.debug("Thunder: media %s returned HTTP %s", title_id, response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def extract_isbns(media: dict | None) -> list[str]:
        if not media:
            return []
        isbns = []
        for fmt in media.get("formats") or []:
            for identifier in fmt.get("identifiers") or []:
                if identifier.get("type") == "ISBN" and identifier.get("value"):
                    value = str(identifier["value"]).strip()
                    if value and value not in isbns:
                        isbns.append(value)
        return isbns

    @staticmethod
    def extract_cover(media: dict | None) -> str | None:
        if not media:
            return None
        covers = media.get("covers") or {}
        best = covers.get("cover510Wide") or covers.get("cover300Wide")
        return best.get("href") if isinstance(best, dict) else None
