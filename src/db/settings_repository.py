"""Repository for application settings (key-value store).

All values are stored and returned as strings (or None).
Callers are responsible for type conversion.

Secret settings (see ``SECRET_SETTING_KEYS``) are encrypted at rest:
encrypted on save and decrypted on read. Non-secret settings are stored
and returned verbatim.
"""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert

from src.utils.secret_settings import is_secret_setting
from src.utils.settings_crypto import SettingsCryptoError, get_settings_crypto

from .base_repository import BaseRepository
from .models import Setting

logger = logging.getLogger(__name__)

BACKUP_SUFFIX = "pre-settings-encryption"


class SettingsRepository(BaseRepository):
    @staticmethod
    def _encrypt_for_storage(key, value):
        """Encrypt *value* if *key* is a secret setting; else return as-is."""
        if value is None or not is_secret_setting(key):
            return value
        return get_settings_crypto().encrypt_value(key, value)

    @staticmethod
    def _decrypt_from_storage(key, value):
        """Decrypt *value* if *key* is a secret setting; else return as-is."""
        if value is None or not is_secret_setting(key):
            return value
        return get_settings_crypto().decrypt_value(key, value)

    def get_setting(self, key, default=None):
        """Get a setting value by key. Returns a string or *default*."""
        with self.get_session() as session:
            query = session.query(Setting).filter(Setting.key == key)
            setting = self._query_and_expunge(session, query, one=True)
            if not setting:
                return default
            return self._decrypt_from_storage(key, setting.value)

    def set_setting(self, key, value):
        """Set a setting value. *value* is coerced to str (None stays None)."""
        with self.get_session() as session:
            str_value = str(value) if value is not None else None
            stored_value = self._encrypt_for_storage(key, str_value)
            stmt = (
                insert(Setting)
                .values(key=key, value=stored_value)
                .on_conflict_do_update(
                    index_elements=[Setting.key],
                    set_={"value": stored_value},
                )
            )
            session.execute(stmt)
            setting = session.query(Setting).filter(Setting.key == key).one()
            session.expunge(setting)
            return setting

    def get_all_settings(self):
        """Get all settings as a dictionary.

        Decryption is isolated per row: a single corrupt secret or wrong
        encryption key fails closed for that key only (it is logged and
        omitted) instead of aborting the whole read, so the remaining
        secret and non-secret settings still load.
        """
        with self.get_session() as session:
            query = session.query(Setting)
            settings = self._query_and_expunge(session, query, one=False)
            result = {}
            for s in settings:
                try:
                    result[s.key] = self._decrypt_from_storage(s.key, s.value)
                except SettingsCryptoError as e:
                    logger.error("Skipping secret setting '%s': %s", s.key, e)
            return result

    def delete_setting(self, key):
        """Delete a setting by key."""
        return self._delete_one(Setting, Setting.key == key)

    def get_undecryptable_secret_keys(self):
        """Return the names of secret settings that fail to decrypt.

        Used for safe diagnostics: it reports *which* secret rows cannot be
        read under the current key (e.g. wrong/lost master secret) without
        ever exposing a value. Non-secret settings and successfully
        decrypting secrets are excluded.
        """
        from src.utils.secret_settings import SECRET_SETTING_KEYS
        from src.utils.settings_crypto import SettingsCrypto

        undecryptable = []
        with self.get_session() as session:
            query = session.query(Setting).filter(Setting.key.in_(SECRET_SETTING_KEYS))
            rows = self._query_and_expunge(session, query, one=False)
            for row in rows:
                if not SettingsCrypto.is_encrypted(row.value):
                    continue
                try:
                    self._decrypt_from_storage(row.key, row.value)
                except SettingsCryptoError:
                    undecryptable.append(row.key)
        return undecryptable

    def encrypt_plaintext_secrets(self):
        """Encrypt any plaintext secret values stored in the table.

        Idempotent: values already in the encrypted envelope format and
        empty/None values are skipped, so this is safe to run on every
        startup and never double-encrypts. Returns the number of values
        newly encrypted.

        This is a one-way migration: once a plaintext secret becomes an
        encrypted row, the value can only be recovered with the master
        secret. To make an upgrade recoverable, a timestamped backup of
        the database is taken *before* the first row is mutated. If the
        backup cannot be created the migration fails closed — plaintext
        secrets are left untouched — rather than encrypting without a
        recovery path.
        """
        from src.utils.secret_settings import SECRET_SETTING_KEYS
        from src.utils.settings_crypto import SettingsCrypto

        crypto = get_settings_crypto()
        with self.get_session() as session:
            rows = session.query(Setting).filter(Setting.key.in_(SECRET_SETTING_KEYS)).all()
            pending = [row for row in rows if row.value and not SettingsCrypto.is_encrypted(row.value)]

            # Nothing to migrate: skip the backup entirely so already-encrypted
            # installs never accumulate a backup on every startup.
            if not pending:
                return 0

            # Fail closed: create the backup before mutating any row. A failure
            # here aborts the transaction with plaintext secrets intact.
            backup_path = self._backup_database_before_encryption()
            logger.info(
                "Created pre-encryption database backup before encrypting %d secret(s): %s",
                len(pending),
                backup_path,
            )

            for row in pending:
                row.value = crypto.encrypt_value(row.key, row.value)
        return len(pending)

    def _backup_database_before_encryption(self) -> str:
        """Back up the SQLite database before the one-way encryption migration.

        Returns the backup path on success. Raises on failure so the caller
        can fail closed and leave plaintext secrets untouched.

        Uses the SQLite online backup API (``sqlite3.Connection.backup``),
        which produces a single, internally consistent snapshot file. Unlike a
        naive file copy it accounts for the WAL: committed transactions still
        sitting in the ``-wal`` file are folded into the backup, so the result
        is a standalone ``database.db`` with no companion ``-wal``/``-shm``
        files required to restore it.

        The backup holds plaintext secrets, so the file is pre-created with
        owner-only (0600) permissions before SQLite writes a single byte to
        it: SQLite adopts an existing empty file as the database, so the
        snapshot is never readable by group/other, even transiently.
        """
        db_path = Path(self.db_manager.db_path)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}.{BACKUP_SUFFIX}-{timestamp}")

        fd = os.open(str(backup_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            # os.open's mode is masked by umask; fchmod pins it to exactly 0600.
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

        source = sqlite3.connect(str(db_path))
        try:
            destination = sqlite3.connect(str(backup_path))
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        return str(backup_path)
