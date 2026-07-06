"""Regression tests for fail-closed startup readiness.

The release blocker was that startup/health could look green after migration or
schema failures. In the integrated fix, ``DatabaseService`` is the single
migration authority and migration/schema failures are fatal during service
construction; Docker health checks the app-level ``/readyz`` readiness endpoint
instead of the unconditional KOSync ``/healthcheck`` liveness endpoint.
"""

import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
START_SH = REPO_ROOT / "start.sh"


# ── start.sh script-level regression ───────────────────────────────


def test_start_sh_delegates_migrations_to_app():
    """start.sh must not run Alembic directly.

    Direct ``alembic upgrade head`` against a pre-Alembic DB can fail with
    ``table books already exists`` and leave an empty alembic_version table.
    DatabaseService owns the verified legacy/unsafe-schema handling instead.
    """
    command_lines = [line for line in START_SH.read_text().splitlines() if not line.lstrip().startswith("#")]
    assert "alembic" not in "\n".join(command_lines)


# ── DatabaseService startup gate ───────────────────────────────────


def test_startup_ready_ok_on_fresh_database(tmp_path):
    from src.db.database_service import DatabaseService

    service = DatabaseService(str(tmp_path / "database.db"))
    try:
        assert service.startup_ready() == (True, "ok")
    finally:
        service.db_manager.close()


def test_database_service_raises_when_alembic_revision_is_unknown(tmp_path):
    """Unknown Alembic revisions fail closed during construction."""
    db_path = tmp_path / "database.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);"
        "INSERT INTO alembic_version VALUES ('deadbeefdead');"
    )
    conn.commit()
    conn.close()

    from src.db.database_service import DatabaseSchemaError, DatabaseService

    with pytest.raises(DatabaseSchemaError):
        DatabaseService(str(db_path))


# ── /readyz endpoint ───────────────────────────────────────────────


def test_readyz_returns_200_when_database_ready(client, flask_app):
    flask_app.config["database_service"].startup_ready.return_value = (True, "ok")
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.get_json() == {"ready": True, "reason": "ok"}


def test_readyz_returns_503_when_database_reports_not_ready(client, flask_app):
    flask_app.config["database_service"].startup_ready.return_value = (
        False,
        "database migrations failed",
    )
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["ready"] is False
    assert body["reason"] == "database migrations failed"


def test_readyz_returns_503_when_database_service_missing(client, flask_app):
    flask_app.config["database_service"] = None
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.get_json()["ready"] is False
