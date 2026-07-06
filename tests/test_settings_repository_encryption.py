"""Tests for encrypt-on-save / decrypt-on-read in SettingsRepository and
the startup plaintext-to-encrypted migration."""

import os
import tempfile
from pathlib import Path

import pytest

from src.db.database_service import DatabaseService
from src.db.models import Setting
from src.utils.settings_crypto import (
    ENVELOPE_PREFIX,
    SettingsCrypto,
    SettingsDecryptionError,
    reset_settings_crypto,
)

DUMMY_KEY = "dummy-master-secret-aaaaaaaaaaaaaaaaaaaa"


@pytest.fixture()
def encryption_key(monkeypatch):
    monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", DUMMY_KEY)
    monkeypatch.delenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY_PREVIOUS", raising=False)
    reset_settings_crypto()
    yield DUMMY_KEY
    reset_settings_crypto()


@pytest.fixture()
def db_service():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield DatabaseService(str(Path(temp_dir) / "settings_enc.db"))


def _raw_value(db_service, key):
    """Read the on-disk (possibly encrypted) value, bypassing decryption."""
    with db_service.get_session() as session:
        row = session.query(Setting).filter(Setting.key == key).first()
        return row.value if row else None


class TestEncryptOnSaveDecryptOnRead:
    def test_secret_is_encrypted_at_rest(self, encryption_key, db_service):
        db_service.set_setting("HARDCOVER_TOKEN", "dummy-hardcover-token")

        stored = _raw_value(db_service, "HARDCOVER_TOKEN")
        assert SettingsCrypto.is_encrypted(stored)
        assert "dummy-hardcover-token" not in stored

        assert db_service.get_setting("HARDCOVER_TOKEN") == "dummy-hardcover-token"

    def test_get_all_settings_decrypts_secrets(self, encryption_key, db_service):
        db_service.set_setting("ABS_KEY", "dummy-abs-key")
        db_service.set_setting("ABS_SERVER", "https://abs.example")

        all_settings = db_service.get_all_settings()
        assert all_settings["ABS_KEY"] == "dummy-abs-key"
        assert all_settings["ABS_SERVER"] == "https://abs.example"

    def test_non_secret_stored_plaintext(self, encryption_key, db_service):
        db_service.set_setting("ABS_SERVER", "https://abs.example")
        assert _raw_value(db_service, "ABS_SERVER") == "https://abs.example"

    def test_none_value_not_encrypted(self, encryption_key, db_service):
        db_service.set_setting("HARDCOVER_TOKEN", None)
        assert _raw_value(db_service, "HARDCOVER_TOKEN") is None
        assert db_service.get_setting("HARDCOVER_TOKEN") is None

    def test_missing_key_returns_default(self, encryption_key, db_service):
        sentinel = object()
        assert db_service.get_setting("NEVER_STORED") is None
        assert db_service.get_setting("NEVER_STORED", sentinel) is sentinel

    def test_get_all_empty_db_returns_empty_dict(self, encryption_key, db_service):
        assert db_service.get_all_settings() == {}

    def test_resave_does_not_double_encrypt(self, encryption_key, db_service):
        db_service.set_setting("KOSYNC_KEY", "dummy-kosync-key")
        # Round-trip the decrypted value back through set_setting.
        db_service.set_setting("KOSYNC_KEY", db_service.get_setting("KOSYNC_KEY"))
        second = _raw_value(db_service, "KOSYNC_KEY")
        # A single envelope prefix (not nested) and the value still decrypts.
        assert second.count(ENVELOPE_PREFIX) == 1
        assert db_service.get_setting("KOSYNC_KEY") == "dummy-kosync-key"


class TestMigration:
    def _seed_plaintext(self, db_service, key, value):
        """Write a plaintext value directly, simulating a pre-encryption DB."""
        with db_service.get_session() as session:
            session.add(Setting(key=key, value=value))

    def test_plaintext_secret_gets_encrypted(self, encryption_key, db_service):
        self._seed_plaintext(db_service, "HARDCOVER_TOKEN", "dummy-hardcover-token")

        migrated = db_service.encrypt_plaintext_secrets()

        assert migrated == 1
        assert SettingsCrypto.is_encrypted(_raw_value(db_service, "HARDCOVER_TOKEN"))
        assert db_service.get_setting("HARDCOVER_TOKEN") == "dummy-hardcover-token"

    def test_migration_is_idempotent(self, encryption_key, db_service):
        self._seed_plaintext(db_service, "ABS_KEY", "dummy-abs-key")

        assert db_service.encrypt_plaintext_secrets() == 1
        first = _raw_value(db_service, "ABS_KEY")
        assert db_service.encrypt_plaintext_secrets() == 0
        assert _raw_value(db_service, "ABS_KEY") == first

    def test_mixed_state_only_migrates_plaintext(self, encryption_key, db_service):
        self._seed_plaintext(db_service, "ABS_KEY", "dummy-abs-key")
        db_service.set_setting("KOSYNC_KEY", "dummy-kosync-key")  # already encrypted

        migrated = db_service.encrypt_plaintext_secrets()

        assert migrated == 1
        assert db_service.get_setting("ABS_KEY") == "dummy-abs-key"
        assert db_service.get_setting("KOSYNC_KEY") == "dummy-kosync-key"

    def test_empty_secret_not_migrated(self, encryption_key, db_service):
        self._seed_plaintext(db_service, "ABS_KEY", "")
        assert db_service.encrypt_plaintext_secrets() == 0
        assert not SettingsCrypto.is_encrypted(_raw_value(db_service, "ABS_KEY") or "")

    def test_non_secret_not_migrated(self, encryption_key, db_service):
        self._seed_plaintext(db_service, "ABS_SERVER", "https://abs.example")
        assert db_service.encrypt_plaintext_secrets() == 0
        assert _raw_value(db_service, "ABS_SERVER") == "https://abs.example"


class TestPreEncryptionBackup:
    """The one-way encryption migration must back up the DB before mutating."""

    def _backups(self, db_service):
        db_path = Path(db_service.db_manager.db_path)
        return sorted(db_path.parent.glob(f"{db_path.name}.pre-settings-encryption-*"))

    def _seed_plaintext(self, db_service, key, value):
        with db_service.get_session() as session:
            session.add(Setting(key=key, value=value))

    def test_backup_created_before_encrypting(self, encryption_key, db_service):
        self._seed_plaintext(db_service, "ABS_KEY", "dummy-abs-key")

        assert self._backups(db_service) == []
        migrated = db_service.encrypt_plaintext_secrets()

        assert migrated == 1
        backups = self._backups(db_service)
        assert len(backups) == 1

        # The backup is a standalone SQLite DB still holding the PLAINTEXT secret,
        # proving it was taken before the row was encrypted.
        backup_service = DatabaseService(str(backups[0]))
        assert _raw_value(backup_service, "ABS_KEY") == "dummy-abs-key"
        # Live DB is now encrypted.
        assert SettingsCrypto.is_encrypted(_raw_value(db_service, "ABS_KEY"))

    def test_backup_is_owner_read_write_only(self, encryption_key, db_service):
        # The backup contains plaintext secrets: it must be 0600 (owner rw only).
        self._seed_plaintext(db_service, "ABS_KEY", "dummy-abs-key")

        assert db_service.encrypt_plaintext_secrets() == 1

        (backup,) = self._backups(db_service)
        assert os.stat(backup).st_mode & 0o777 == 0o600

    def test_no_plaintext_means_no_backup(self, encryption_key, db_service):
        # Already-encrypted secret only — nothing pending.
        db_service.set_setting("ABS_KEY", "dummy-abs-key")
        assert SettingsCrypto.is_encrypted(_raw_value(db_service, "ABS_KEY"))

        assert db_service.encrypt_plaintext_secrets() == 0
        assert self._backups(db_service) == []

    def test_empty_db_means_no_backup(self, encryption_key, db_service):
        assert db_service.encrypt_plaintext_secrets() == 0
        assert self._backups(db_service) == []

    def test_backup_failure_fails_closed(self, encryption_key, db_service, monkeypatch):
        self._seed_plaintext(db_service, "ABS_KEY", "dummy-abs-key")

        def boom(self):
            raise OSError("disk full")

        monkeypatch.setattr(
            "src.db.settings_repository.SettingsRepository._backup_database_before_encryption",
            boom,
        )

        with pytest.raises(OSError):
            db_service.encrypt_plaintext_secrets()

        # Plaintext is untouched (not partially encrypted) and no backup remains.
        assert _raw_value(db_service, "ABS_KEY") == "dummy-abs-key"
        assert self._backups(db_service) == []


class TestUndecryptableSecretDiagnostics:
    def test_lists_only_undecryptable_secret_keys(self, encryption_key, db_service, monkeypatch):
        db_service.set_setting("ABS_KEY", "dummy-abs-key")  # good
        db_service.set_setting("HARDCOVER_TOKEN", "dummy-hardcover-token")  # will break
        db_service.set_setting("ABS_SERVER", "https://abs.example")  # non-secret

        with db_service.get_session() as session:
            row = session.query(Setting).filter(Setting.key == "HARDCOVER_TOKEN").one()
            row.value = row.value + "tampered"

        undecryptable = db_service.get_undecryptable_secret_keys()
        assert undecryptable == ["HARDCOVER_TOKEN"]

    def test_no_undecryptable_returns_empty(self, encryption_key, db_service):
        db_service.set_setting("ABS_KEY", "dummy-abs-key")
        assert db_service.get_undecryptable_secret_keys() == []


class TestKeySourceDiagnostics:
    def test_explicit_env_var_source(self, monkeypatch):
        from src import app_runtime

        monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", "dummy-explicit")
        assert app_runtime.get_settings_key_source() == app_runtime.KEY_SOURCE_EXPLICIT

    def test_flask_env_source(self, monkeypatch):
        from src import app_runtime

        monkeypatch.delenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("FLASK_SECRET_KEY", "dummy-flask-secret")
        assert app_runtime.get_settings_key_source() == app_runtime.KEY_SOURCE_FLASK_ENV

    def test_flask_file_source(self, monkeypatch, tmp_path):
        from src import app_runtime

        monkeypatch.delenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert app_runtime.get_settings_key_source() == app_runtime.KEY_SOURCE_FLASK_FILE

    def test_source_label_never_contains_secret_value(self, monkeypatch):
        from src import app_runtime

        monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", "super-secret-value-xyz")
        label = app_runtime.get_settings_key_source()
        assert "super-secret-value-xyz" not in label

    def test_log_emitted_once(self, monkeypatch, caplog):
        import logging

        from src import app_runtime

        monkeypatch.setattr(app_runtime, "_key_source_logged", False)
        monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", "dummy-explicit")

        with caplog.at_level(logging.INFO):
            app_runtime.log_settings_key_source()
            app_runtime.log_settings_key_source()

        source_logs = [r for r in caplog.records if "Settings encryption key source" in r.message]
        assert len(source_logs) == 1


class TestWrongKeyFailsClosed:
    def test_read_with_wrong_key_raises(self, encryption_key, db_service, monkeypatch):
        db_service.set_setting("HARDCOVER_TOKEN", "dummy-hardcover-token")

        monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", "dummy-wrong-secret-cccccccccccccc")
        reset_settings_crypto()

        with pytest.raises(SettingsDecryptionError):
            db_service.get_setting("HARDCOVER_TOKEN")

    def test_get_all_isolates_undecryptable_secret(self, encryption_key, db_service, monkeypatch):
        # An undecryptable secret must not drop the other settings.
        db_service.set_setting("HARDCOVER_TOKEN", "dummy-hardcover-token")
        db_service.set_setting("ABS_KEY", "dummy-abs-key")
        db_service.set_setting("ABS_SERVER", "https://abs.example")  # non-secret

        # Corrupt only one secret's ciphertext, leaving the rest valid.
        with db_service.get_session() as session:
            row = session.query(Setting).filter(Setting.key == "HARDCOVER_TOKEN").one()
            row.value = row.value + "tampered"

        all_settings = db_service.get_all_settings()

        assert "HARDCOVER_TOKEN" not in all_settings  # failed closed, omitted
        assert all_settings["ABS_KEY"] == "dummy-abs-key"  # healthy secret still loads
        assert all_settings["ABS_SERVER"] == "https://abs.example"  # non-secret unaffected

        # Targeted reads still fail closed for the bad key.
        with pytest.raises(SettingsDecryptionError):
            db_service.get_setting("HARDCOVER_TOKEN")


class TestUndecryptableSecretNotOverwritten:
    """A blank secret submission must not clobber an undecryptable secret row.

    Mirrors the settings-route save logic: secret keys submitted blank are
    skipped so the stored ciphertext is preserved. This guards against a
    wrong-key startup leading to silent data loss when the user saves the
    Settings form (whose secret fields render blank) without re-entering
    every credential.
    """

    def _apply_settings_save(self, db_service, submitted):
        """Replicate the settings route's persist loop for *submitted* values."""
        from src.utils.secret_settings import SECRET_SETTING_KEYS

        current_settings = {}
        try:
            current_settings = db_service.get_all_settings()
        except Exception:
            current_settings = {}

        for key, value in submitted.items():
            clean_value = value.strip()
            if not clean_value and key in SECRET_SETTING_KEYS:
                continue  # preserve existing secret
            if clean_value:
                db_service.set_setting(key, clean_value)
            elif key in current_settings:
                db_service.set_setting(key, "")

    def test_blank_secret_post_preserves_undecryptable_row(self, encryption_key, db_service):
        db_service.set_setting("HARDCOVER_TOKEN", "dummy-hardcover-token")

        # Simulate a wrong/lost key: corrupt the stored ciphertext so it cannot decrypt.
        with db_service.get_session() as session:
            row = session.query(Setting).filter(Setting.key == "HARDCOVER_TOKEN").one()
            row.value = row.value + "tampered"
        corrupted_raw = _raw_value(db_service, "HARDCOVER_TOKEN")

        # User saves the settings form with the secret field left blank.
        self._apply_settings_save(db_service, {"HARDCOVER_TOKEN": "", "ABS_SERVER": "https://abs.example"})

        # The undecryptable secret row is untouched (not blanked, not re-encrypted).
        assert _raw_value(db_service, "HARDCOVER_TOKEN") == corrupted_raw
        # A non-secret submitted alongside it still persists normally.
        assert _raw_value(db_service, "ABS_SERVER") == "https://abs.example"

    def test_explicit_new_secret_overwrites(self, encryption_key, db_service):
        db_service.set_setting("HARDCOVER_TOKEN", "dummy-old-token")

        self._apply_settings_save(db_service, {"HARDCOVER_TOKEN": "dummy-new-token"})

        assert db_service.get_setting("HARDCOVER_TOKEN") == "dummy-new-token"


class TestKeyRotationThroughRepository:
    def test_previous_key_decrypts_after_rotation(self, db_service, monkeypatch):
        old_key = "dummy-old-secret-dddddddddddddddddddd"
        new_key = "dummy-new-secret-eeeeeeeeeeeeeeeeeeee"

        monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", old_key)
        monkeypatch.delenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY_PREVIOUS", raising=False)
        reset_settings_crypto()
        db_service.set_setting("HARDCOVER_TOKEN", "dummy-hardcover-token")

        monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", new_key)
        monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY_PREVIOUS", old_key)
        reset_settings_crypto()

        assert db_service.get_setting("HARDCOVER_TOKEN") == "dummy-hardcover-token"
        reset_settings_crypto()


class TestMasterSecretDiscovery:
    def test_explicit_env_var_preferred(self, monkeypatch):
        from src.app_runtime import get_settings_master_secret

        monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", "dummy-explicit-key")
        assert get_settings_master_secret() == "dummy-explicit-key"

    def test_falls_back_to_flask_secret(self, monkeypatch, tmp_path):
        from src import app_runtime

        monkeypatch.delenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        secret = app_runtime.get_settings_master_secret()
        assert secret
        # Stable across calls — persisted to the data dir.
        assert app_runtime.get_settings_master_secret() == secret

    def test_previous_secret_read_from_env(self, monkeypatch):
        from src.app_runtime import get_settings_previous_secret

        monkeypatch.setenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY_PREVIOUS", "dummy-prev-key")
        assert get_settings_previous_secret() == "dummy-prev-key"

        monkeypatch.delenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY_PREVIOUS", raising=False)
        assert get_settings_previous_secret() == ""

    def test_raises_when_no_key_and_data_dir_unwritable(self, monkeypatch, tmp_path):
        """An unwritable data dir with no key env vars must fail closed.

        Returning an ephemeral key here would let the startup migration
        encrypt secrets with a key that vanishes on the next restart,
        permanently stranding them.
        """
        if os.geteuid() == 0:
            pytest.skip("root bypasses directory permissions")

        from src.app_runtime import EphemeralSecretKeyError, get_settings_master_secret

        monkeypatch.delenv("PAGEKEEPER_SETTINGS_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)

        unwritable = tmp_path / "readonly"
        unwritable.mkdir()
        unwritable.chmod(0o500)
        monkeypatch.setenv("DATA_DIR", str(unwritable / "data"))

        try:
            with pytest.raises(EphemeralSecretKeyError):
                get_settings_master_secret()
        finally:
            unwritable.chmod(0o700)

    def test_flask_session_secret_still_falls_back_to_ephemeral(self, monkeypatch, tmp_path):
        """The Flask session secret keeps the lenient ephemeral fallback."""
        from src.app_runtime import get_or_create_secret_key

        unwritable = tmp_path / "readonly"
        unwritable.mkdir()
        unwritable.chmod(0o500)
        monkeypatch.setenv("DATA_DIR", str(unwritable / "data"))

        try:
            assert get_or_create_secret_key()  # no raise: ephemeral is acceptable here
        finally:
            unwritable.chmod(0o700)


class TestEnvSyncAfterEncryption:
    """The settings save/load cycle must expose decrypted secrets to env."""

    def test_load_settings_syncs_decrypted_secret_to_env(self, encryption_key, db_service, monkeypatch):
        from src.utils.config_loader import load_settings

        monkeypatch.delenv("HARDCOVER_TOKEN", raising=False)

        # Mimic the settings route: save then sync to os.environ.
        db_service.set_setting("HARDCOVER_TOKEN", "dummy-hardcover-token")
        assert SettingsCrypto.is_encrypted(_raw_value(db_service, "HARDCOVER_TOKEN"))

        load_settings(db_service)
        assert os.environ["HARDCOVER_TOKEN"] == "dummy-hardcover-token"

    def test_startup_migration_then_env_sync(self, encryption_key, db_service, monkeypatch):
        from src.utils.config_loader import ConfigLoader

        monkeypatch.delenv("ABS_KEY", raising=False)

        # Seed a legacy plaintext secret directly.
        with db_service.get_session() as session:
            session.add(Setting(key="ABS_KEY", value="dummy-abs-key"))

        ConfigLoader.migrate_secrets_encryption(db_service)
        assert SettingsCrypto.is_encrypted(_raw_value(db_service, "ABS_KEY"))

        ConfigLoader.load_settings(db_service)
        assert os.environ["ABS_KEY"] == "dummy-abs-key"
