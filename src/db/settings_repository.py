"""Repository for application settings (key-value store).

All values are stored and returned as strings (or None).
Callers are responsible for type conversion.
"""

from sqlalchemy.dialects.sqlite import insert

from .base_repository import BaseRepository
from .models import Setting


class SettingsRepository(BaseRepository):

    @staticmethod
    def _persist_and_detach(session, obj):
        """Flush, refresh, and detach an object so it's usable outside the session."""
        session.flush()
        session.refresh(obj)
        session.expunge(obj)

    def get_setting(self, key, default=None):
        """Get a setting value by key. Returns a string or *default*."""
        with self.get_session() as session:
            setting = session.query(Setting).filter(Setting.key == key).first()
            return setting.value if setting else default

    def set_setting(self, key, value):
        """Set a setting value. *value* is coerced to str (None stays None)."""
        with self.get_session() as session:
            str_value = str(value) if value is not None else None
            stmt = (
                insert(Setting)
                .values(key=key, value=str_value)
                .on_conflict_do_update(
                    index_elements=[Setting.key],
                    set_={"value": str_value},
                )
            )
            session.execute(stmt)
            setting = session.query(Setting).filter(Setting.key == key).one()
            session.expunge(setting)
            return setting

    def get_all_settings(self):
        """Get all settings as a dictionary."""
        with self.get_session() as session:
            settings = session.query(Setting).all()
            return {s.key: s.value for s in settings}

    def delete_setting(self, key):
        """Delete a setting by key."""
        return self._delete_one(Setting, Setting.key == key)
