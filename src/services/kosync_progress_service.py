import calendar
import logging
import os
import threading
import time
from datetime import UTC, datetime

from src.db.models import KosyncDocument
from src.utils.constants import INTERNAL_DEVICE_NAMES

logger = logging.getLogger(__name__)


class KosyncProgressService:
    """KoSync GET/PUT progress flow extracted from KosyncService."""

    def __init__(self, service, db, container, manager):
        self.service = service
        self.db = db
        self.container = container
        self.manager = manager

    def handle_put_progress(self, data, remote_addr, debounce_manager=None):
        if not data:
            logger.warning("KOSync: PUT progress with no JSON data from %s", remote_addr)
            return {"error": "No data"}, 400

        doc_hash = data.get("document")
        if not doc_hash or not isinstance(doc_hash, str):
            logger.warning("KOSync: PUT progress with no document ID from %s", remote_addr)
            return {"error": "Missing document ID"}, 400
        if len(doc_hash) > 64:
            return {"error": "Document hash too long"}, 400

        percentage = data.get("percentage", 0)
        try:
            percentage = float(percentage)
        except (TypeError, ValueError):
            return {"error": "Invalid percentage value"}, 400
        if percentage < 0.0 or percentage > 1.0:
            return {"error": "Percentage must be between 0.0 and 1.0"}, 400

        logger.info(
            "KOSync: PUT progress request for doc %s... from %s (device: %s)",
            doc_hash[:8],
            remote_addr,
            data.get("device", "unknown"),
        )

        progress = str(data.get("progress", ""))[:512]
        device = str(data.get("device", ""))[:128]
        device_id = str(data.get("device_id", ""))[:64]
        now = datetime.now(UTC)

        kosync_doc = self.db.get_kosync_document(doc_hash)
        furthest_wins = os.environ.get("KOSYNC_FURTHEST_WINS", "true").lower() == "true"
        force_update = data.get("force", False)
        same_device = kosync_doc and kosync_doc.device_id == device_id

        if furthest_wins and kosync_doc and kosync_doc.percentage and not force_update and not same_device:
            existing_pct = float(kosync_doc.percentage)
            new_pct = float(percentage)
            if new_pct < existing_pct - 0.0001:
                logger.info(
                    "KOSync: Ignored progress from '%s' for doc %s... (server has higher: %.2f%% vs new %.2f%%)",
                    device,
                    doc_hash[:8],
                    existing_pct,
                    new_pct,
                )
                ts_source = kosync_doc.timestamp or now
                if device and device.lower() == "booknexus":
                    ignored_timestamp = int(calendar.timegm(ts_source.timetuple()))
                else:
                    ignored_timestamp = ts_source.isoformat()
                return {"document": doc_hash, "timestamp": ignored_timestamp}, 200

        if kosync_doc is None:
            kosync_doc = KosyncDocument(
                document_hash=doc_hash,
                progress=progress,
                percentage=percentage,
                device=device,
                device_id=device_id,
                timestamp=now,
            )
            logger.info("KOSync: New document tracked: %s... from device '%s'", doc_hash[:8], device)
        else:
            logger.info(
                "KOSync: Received progress from '%s' for doc %s... -> %.2f%% (Updated from %.2f%%)",
                device,
                doc_hash[:8],
                float(percentage),
                float(kosync_doc.percentage) if kosync_doc.percentage else 0,
            )
            kosync_doc.progress = progress
            kosync_doc.percentage = percentage
            kosync_doc.device = device
            kosync_doc.device_id = device_id
            kosync_doc.timestamp = now

        self.db.save_kosync_document(kosync_doc)
        linked_book = self._resolve_linked_book(doc_hash, kosync_doc)

        if not linked_book:
            self._handle_unlinked_document(doc_hash, kosync_doc, device)

        if linked_book:
            if linked_book.status in ("paused", "dnf", "not_started") and not linked_book.activity_flag:
                linked_book.activity_flag = True
                self.db.save_book(linked_book)
                logger.info("KOSync PUT: Activity detected on %s book '%s'", linked_book.status, linked_book.title)

            logger.debug("KOSync: Updated linked book '%s' to %.2f%%", linked_book.title, percentage)
            is_internal = device and device.lower() in INTERNAL_DEVICE_NAMES
            instant_sync_enabled = os.environ.get("INSTANT_SYNC_ENABLED", "true").lower() != "false"
            if linked_book.status == "active" and self.manager and not is_internal and instant_sync_enabled and debounce_manager:
                logger.debug("KOSync PUT: Progress event recorded for '%s'", linked_book.title)
                debounce_manager.record_event(linked_book.id, linked_book.title)

        response_timestamp = now.isoformat()
        if device and device.lower() == "booknexus":
            response_timestamp = int(calendar.timegm(now.timetuple()))

        return {"document": doc_hash, "timestamp": response_timestamp}, 200

    def handle_get_progress(self, doc_id, remote_addr):
        if len(doc_id) > 64:
            return {"error": "Document ID too long"}, 400

        logger.info("KOSync: GET progress for doc %s... from %s", doc_id[:8], remote_addr)

        kosync_doc = self.db.get_kosync_document(doc_id)
        if kosync_doc:
            if kosync_doc.linked_book_id:
                book = self.db.get_book_by_id(kosync_doc.linked_book_id)
                if book:
                    return self.resolve_best_progress(doc_id, book)
            elif kosync_doc.linked_abs_id:
                book = self.db.get_book_by_abs_id(kosync_doc.linked_abs_id)
                if book:
                    return self.resolve_best_progress(doc_id, book)

            has_progress = kosync_doc.percentage and float(kosync_doc.percentage) > 0
            if has_progress:
                return self.service.serialize_progress(kosync_doc, device_default=""), 200

        book = self.db.get_book_by_kosync_id(doc_id)
        if book:
            return self.resolve_best_progress(doc_id, book)

        resolved_book = self.service.resolve_book_by_sibling_hash(doc_id, existing_doc=kosync_doc)
        if resolved_book:
            self.service.register_hash_for_book(doc_id, resolved_book)
            return self.resolve_best_progress(doc_id, resolved_book)

        auto_create = os.environ.get("AUTO_CREATE_EBOOK_MAPPING", "true").lower() == "true"
        if auto_create and self.service.start_discovery_if_available(doc_id):
            stub = KosyncDocument(document_hash=doc_id)
            self.db.save_kosync_document(stub)
            logger.info("KOSync: Created stub for unknown hash %s..., starting background discovery", doc_id[:8])
            threading.Thread(target=self.service.run_get_auto_discovery, args=(doc_id,), daemon=True).start()

        logger.warning("KOSync: Document not found: %s... (GET from %s)", doc_id[:8], remote_addr)
        return {"message": "Document not found on server"}, 502

    def resolve_best_progress(self, doc_id, book):
        states = self.db.get_states_for_book(book.id)
        sibling_docs = self.db.get_kosync_documents_for_book_by_book_id(book.id)
        now_ts = time.time()
        docs_with_progress = [
            d
            for d in sibling_docs
            if d.percentage and float(d.percentage) > 0 and d.timestamp and (now_ts - d.timestamp.timestamp()) < 30 * 86400
        ]
        if not docs_with_progress:
            docs_with_progress = [d for d in sibling_docs if d.percentage and float(d.percentage) > 0 and d.timestamp]
        if docs_with_progress:
            best_doc = max(docs_with_progress, key=lambda d: float(d.percentage))
            logger.info(
                "KOSync: Resolved %s... to '%s' via sibling hash %s... (%.2f%%)",
                doc_id[:8],
                book.title,
                best_doc.document_hash[:8],
                float(best_doc.percentage),
            )
            return self.service.serialize_progress(best_doc, doc_id), 200

        if not states:
            return {"message": "Document not found on server"}, 502

        kosync_state = next((s for s in states if s.client_name.lower() == "kosync"), None)
        latest_state = kosync_state or max(states, key=lambda s: s.last_updated or float("-inf"))
        return {
            "device": "pagekeeper",
            "device_id": "pagekeeper",
            "document": doc_id,
            "percentage": float(latest_state.percentage) if latest_state.percentage else 0,
            "progress": (latest_state.xpath or latest_state.cfi) if hasattr(latest_state, "xpath") else "",
            "timestamp": int(latest_state.last_updated) if latest_state.last_updated else 0,
        }, 200

    def _resolve_linked_book(self, doc_hash, kosync_doc):
        if kosync_doc.linked_book_id:
            return self.db.get_book_by_id(kosync_doc.linked_book_id)
        if kosync_doc.linked_abs_id:
            return self.db.get_book_by_abs_id(kosync_doc.linked_abs_id)

        linked_book = self.db.get_book_by_kosync_id(doc_hash)
        if linked_book:
            self.db.link_kosync_document(doc_hash, linked_book.id, linked_book.abs_id)
        return linked_book

    def _handle_unlinked_document(self, doc_hash, kosync_doc, device):
        auto_create = os.environ.get("AUTO_CREATE_EBOOK_MAPPING", "true").lower() == "true"
        discovery_started = auto_create and self.service.start_discovery_if_available(doc_hash)
        if discovery_started:
            threading.Thread(target=self.service.run_put_auto_discovery, args=(doc_hash,), daemon=True).start()
            return

        try:
            suggestion_svc = self.container.suggestion_service()
            suggestion_svc.queue_kosync_suggestion(
                doc_hash,
                filename=kosync_doc.filename,
                device=device,
                progress_percentage=float(kosync_doc.percentage or 0),
                source_updated_at=kosync_doc.timestamp,
            )
        except Exception as exc:
            logger.debug("KoSync suggestion attempt failed for %s...: %s", doc_hash[:8], exc)
