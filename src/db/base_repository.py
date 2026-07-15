# pyright: reportMissingImports=false

"""Base repository with shared query helpers to reduce boilerplate."""

import logging
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


class BaseRepository:
    """Provides common query patterns for domain repositories."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    @contextmanager
    def get_session(self):
        """Context manager for database sessions with automatic commit/rollback."""
        session = self.db_manager.get_session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error("Database error: %s", sanitize_log_data(e))
            raise
        finally:
            session.close()

    def _expunge_items(self, session, items):
        for item in items:
            session.expunge(item)
        return items

    def _query_and_expunge(self, session, query, *, one=False):
        if one:
            obj = query.first()
            if obj:
                session.expunge(obj)
            return obj
        items = query.all()
        return self._expunge_items(session, items)

    def _paginate(self, query, page=1, per_page=50):
        safe_page = max(1, int(page))
        safe_per_page = max(1, int(per_page))
        total = query.count()
        items = query.offset((safe_page - 1) * safe_per_page).limit(safe_per_page).all()
        return items, total

    def _get_one(self, model, *filters):
        """Query a single row, expunge and return it (or None)."""
        with self.get_session() as session:
            query = session.query(model).filter(*filters)
            return self._query_and_expunge(session, query, one=True)

    def _get_all(self, model, *filters, order_by=None):
        """Query multiple rows, expunge and return them."""
        with self.get_session() as session:
            query = session.query(model)
            if filters:
                query = query.filter(*filters)
            if order_by is not None:
                query = query.order_by(order_by)
            return self._query_and_expunge(session, query, one=False)

    def _exists(self, model, *filters):
        """Return True if any row matches the filters, without exposing it."""
        with self.get_session() as session:
            return session.query(model).filter(*filters).first() is not None

    def _count(self, model, *filters):
        """Return the number of rows matching the filters (0 for none)."""
        with self.get_session() as session:
            query = session.query(model)
            if filters:
                query = query.filter(*filters)
            return query.count()

    def _delete_one(self, model, *filters):
        """Find and delete a single row. Returns True if deleted."""
        with self.get_session() as session:
            obj = session.query(model).filter(*filters).first()
            if obj:
                session.delete(obj)
                return True
            return False

    def _save_new(self, obj):
        """Insert a new object, flush/refresh/expunge and return it."""
        with self.get_session() as session:
            session.add(obj)
            session.flush()
            session.refresh(obj)
            session.expunge(obj)
            return obj

    def _merge_save(self, obj):
        """Merge an object by primary key, flush/refresh/expunge and return it.

        Uses ``session.merge`` so a new primary key inserts and an existing one
        copies every column attribute from ``obj`` onto the loaded row. This
        differs from ``_upsert``, which only copies a chosen subset of
        attributes; callers needing merge's copy-all semantics use this helper.
        """
        with self.get_session() as session:
            merged = session.merge(obj)
            session.flush()
            session.refresh(merged)
            session.expunge(merged)
            return merged

    def _upsert(self, model, lookup_filters, obj, update_attrs, normalize=None):
        """Find existing by filters and update attrs, or insert new. Returns the saved object.

        When ``normalize`` is provided, it is invoked as ``normalize(obj, existing)``
        against the row found inside this transaction before update attributes are
        copied. This lets callers reconcile incoming values against the actual
        existing row atomically, closing the race where a concurrent insert lands
        between a caller's pre-read and this transaction's own lookup.
        """
        with self.get_session() as session:
            existing = session.query(model).filter(*lookup_filters).first()
            if existing:
                self._apply_update(existing, obj, update_attrs, normalize)
                session.flush()
                session.refresh(existing)
                session.expunge(existing)
                return existing
            else:
                try:
                    session.add(obj)
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    existing = session.query(model).filter(*lookup_filters).first()
                    if existing:
                        self._apply_update(existing, obj, update_attrs, normalize)
                        session.flush()
                        session.refresh(existing)
                        session.expunge(existing)
                        return existing
                    raise
                session.refresh(obj)
                session.expunge(obj)
                return obj

    @staticmethod
    def _apply_update(existing, obj, update_attrs, normalize=None):
        if normalize is not None:
            normalize(obj, existing)
        for attr in update_attrs:
            if hasattr(obj, attr):
                setattr(existing, attr, getattr(obj, attr))
