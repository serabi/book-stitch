"""ABSService — thin wrapper around ABSClient with is_available() guards.

Every public method checks is_available() first and returns a safe default
(empty list, None, False) when ABS is disabled. This eliminates scattered
guard checks in every consumer.
"""

import logging
import os

from src.api.api_clients import ABSClient

logger = logging.getLogger(__name__)


class ABSService:
    def __init__(self, abs_client: ABSClient):
        self.abs_client = abs_client

    def is_available(self) -> bool:
        return self.abs_client.is_configured()

    # --- Audiobook listing ---

    def get_audiobooks(self) -> list[dict]:
        """Get audiobooks from configured libraries or all libraries."""
        if not self.is_available():
            return []
        lib_ids_str = os.environ.get("ABS_LIBRARY_IDS", "").strip()
        if lib_ids_str:
            lib_ids = [lid.strip() for lid in lib_ids_str.split(",") if lid.strip()]
            return self.abs_client.get_audiobooks_for_libs(lib_ids)
        return self.abs_client.get_all_audiobooks()

    # --- Item details ---

    def get_item_details(self, abs_id: str) -> dict | None:
        if not self.is_available():
            return None
        return self.abs_client.get_item_details(abs_id)

    # --- Collection management ---

    def add_to_collection(self, abs_id: str, collection_name: str) -> bool:
        if not self.is_available() or not collection_name:
            return False
        return self.abs_client.add_to_collection(abs_id, collection_name)

    def remove_from_collection(self, abs_id: str, collection_name: str) -> bool:
        if not self.is_available() or not collection_name:
            return False
        return self.abs_client.remove_from_collection(abs_id, collection_name)

    # --- Ebook operations ---

    def search_ebooks(self, query: str) -> list[dict]:
        if not self.is_available():
            return []
        return self.abs_client.search_ebooks(query)

    def get_ebook_files(self, item_id: str) -> list[dict]:
        if not self.is_available():
            return []
        return self.abs_client.get_ebook_files(item_id)

    # --- URL construction ---

    def get_cover_proxy_url(self, abs_id: str) -> str | None:
        if not self.is_available() or not abs_id:
            return None
        return f"/api/cover-proxy/{abs_id}"

    def get_abs_item_url(self, abs_id: str) -> str | None:
        if not self.is_available() or not abs_id:
            return None
        return f"{self.abs_client.base_url}/item/{abs_id}"

    # --- Progress ---

    def mark_finished(self, abs_id: str) -> bool:
        if not self.is_available():
            return False
        return self.abs_client.mark_finished(abs_id)

    def get_libraries(self) -> list[dict]:
        if not self.is_available():
            return []
        return self.abs_client.get_libraries()
