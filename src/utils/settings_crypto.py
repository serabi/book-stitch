"""Encryption of sensitive settings at rest.

Secrets stored in the SQLite ``settings`` table are encrypted with
Fernet (authenticated symmetric encryption from the ``cryptography``
library). The encryption key is derived from a master secret via
HKDF-SHA256 and is never stored in the database.

Envelope format::

    pkenc:v1:fernet:<urlsafe-base64-fernet-token>

The plaintext sealed inside the token binds the setting name, so a
ciphertext copied from one setting key cannot be decrypted under a
different key name (defends against copy-paste/confused-deputy attacks).

Design guarantees:

- Idempotent: already-encrypted values pass through ``encrypt_value``
  unchanged; plaintext values pass through ``decrypt_value`` unchanged
  so the system keeps working during the plaintext-to-encrypted
  transition.
- Fail closed: a wrong key, tampered ciphertext, or name mismatch
  raises :class:`SettingsDecryptionError` rather than returning
  corrupted or partial data.
- Rotation: a previous key is accepted for decryption via
  :class:`~cryptography.fernet.MultiFernet`, while new writes always use
  the current (primary) key.
"""

import base64
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

ENVELOPE_PREFIX = "pkenc:v1:fernet:"

_HKDF_INFO = b"pagekeeper-settings-encryption-v1"
_HKDF_LENGTH = 32
_HKDF_SALT = b"pagekeeper-settings-encryption-salt-v1"

# Separates the bound setting name from its value inside the sealed
# plaintext. A NUL byte cannot appear in a setting key, so the split is
# unambiguous.
_NAME_SEPARATOR = b"\x00"


class SettingsCryptoError(Exception):
    """Base class for settings-encryption errors."""


class SettingsDecryptionError(SettingsCryptoError):
    """Raised when a value cannot be decrypted (wrong key, tamper, mismatch)."""


def _derive_fernet_key(master_secret: str) -> bytes:
    """Derive a urlsafe-base64 Fernet key from *master_secret* via HKDF."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_HKDF_LENGTH,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    )
    derived = hkdf.derive(master_secret.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


class SettingsCrypto:
    """Encrypt and decrypt secret setting values.

    *master_secret* is the primary key material. *previous_secret* is an
    optional retired key kept available for decryption during rotation.
    """

    def __init__(self, master_secret: str, previous_secret: str | None = None):
        if not master_secret:
            raise SettingsCryptoError("master_secret must be a non-empty string")

        primary = Fernet(_derive_fernet_key(master_secret))
        self._primary = primary

        fernets = [primary]
        if previous_secret:
            fernets.append(Fernet(_derive_fernet_key(previous_secret)))
        self._multi = MultiFernet(fernets)

    @staticmethod
    def is_encrypted(value: str | None) -> bool:
        """Return True if *value* is in the encrypted envelope format."""
        return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)

    def encrypt_value(self, name: str, value: str | None) -> str | None:
        """Encrypt *value* for setting *name*, binding the name into the payload.

        ``None`` passes through unchanged. Already-encrypted values pass
        through unchanged so encryption is idempotent.
        """
        if value is None:
            return None
        if self.is_encrypted(value):
            return value

        payload = name.encode("utf-8") + _NAME_SEPARATOR + value.encode("utf-8")
        token = self._primary.encrypt(payload).decode("ascii")
        return ENVELOPE_PREFIX + token

    def decrypt_value(self, name: str, value: str | None) -> str | None:
        """Decrypt *value* for setting *name*.

        ``None`` and plaintext (non-enveloped) values pass through
        unchanged to support the migration transition. A wrong key,
        tampered token, or name mismatch raises
        :class:`SettingsDecryptionError`.
        """
        if value is None:
            return None
        if not self.is_encrypted(value):
            return value

        token = value[len(ENVELOPE_PREFIX) :].encode("ascii")
        try:
            payload = self._multi.decrypt(token)
        except InvalidToken as exc:
            raise SettingsDecryptionError(
                f"Could not decrypt setting '{name}': wrong encryption key or tampered value. "
                "Verify PAGEKEEPER_SETTINGS_ENCRYPTION_KEY (and *_PREVIOUS for rotation)."
            ) from exc

        bound_name, separator, plaintext = payload.partition(_NAME_SEPARATOR)
        if separator != _NAME_SEPARATOR or bound_name.decode("utf-8") != name:
            raise SettingsDecryptionError(
                f"Decrypted setting '{name}' is bound to a different name; refusing to return it."
            )
        return plaintext.decode("utf-8")


_crypto_cache: SettingsCrypto | None = None


def get_settings_crypto() -> SettingsCrypto:
    """Return a process-wide :class:`SettingsCrypto` built from discovered keys.

    The instance is cached after first construction. Tests and key
    rotation can call :func:`reset_settings_crypto` to force rebuilding.
    """
    global _crypto_cache
    if _crypto_cache is None:
        from src.app_runtime import get_settings_master_secret, get_settings_previous_secret

        _crypto_cache = SettingsCrypto(
            get_settings_master_secret(),
            get_settings_previous_secret() or None,
        )
    return _crypto_cache


def reset_settings_crypto() -> None:
    """Clear the cached :class:`SettingsCrypto` (key rotation / tests)."""
    global _crypto_cache
    _crypto_cache = None
