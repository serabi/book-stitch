"""Canonical list of setting keys whose values are sensitive secrets.

This is the single source of truth for which settings hold API keys,
tokens, and passwords. Encryption at rest, log redaction, and the
settings UI all reference this list so they never drift apart.
"""

SECRET_SETTING_KEYS = frozenset(
    {
        "ABS_KEY",
        "STORYTELLER_PASSWORD",
        "GRIMMORY_PASSWORD",
        "GRIMMORY_2_PASSWORD",
        "CWA_PASSWORD",
        "KOSYNC_KEY",
        "KOSYNC_SERVER_KEY",
        "TELEGRAM_BOT_TOKEN",
        "HARDCOVER_TOKEN",
        "DEEPGRAM_API_KEY",
        "BOOKFUSION_API_KEY",
        "BOOKFUSION_UPLOAD_API_KEY",
        "LIBBY_IDENTITY_TOKEN",
        "LIBBY_SYNC_TOKEN",
    }
)


def is_secret_setting(key: str) -> bool:
    """Return True if *key* names a setting whose value is a secret."""
    return key in SECRET_SETTING_KEYS
