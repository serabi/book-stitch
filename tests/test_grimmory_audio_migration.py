import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command


def _config(db_path):
    root = Path(__file__).parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def test_grimmory_audio_identity_migration_round_trip(tmp_path):
    db_path = tmp_path / "migration.db"
    config = _config(db_path)
    command.upgrade(config, "z7a8b9c0d1e2")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO detected_books (source, source_id, title, progress_percentage) VALUES (?, ?, ?, ?)",
            ("abs", "abs-1", "Audio", 0.2),
        )
        connection.execute(
            "INSERT INTO detected_books (source, source_id, title, progress_percentage) VALUES (?, ?, ?, ?)",
            ("grimmory", "default:book.epub", "Ebook", 0.2),
        )

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        assert dict(connection.execute("SELECT source, media_format FROM detected_books")) == {
            "abs": "audiobook",
            "grimmory": "ebook",
        }
        book_columns = {row[1] for row in connection.execute("PRAGMA table_info(books)")}
        assert "grimmory_audio_source_id" in book_columns

    command.downgrade(config, "z7a8b9c0d1e2")
    with sqlite3.connect(db_path) as connection:
        detected_columns = {row[1] for row in connection.execute("PRAGMA table_info(detected_books)")}
        book_columns = {row[1] for row in connection.execute("PRAGMA table_info(books)")}
        assert "media_format" not in detected_columns
        assert "grimmory_audio_source_id" not in book_columns
