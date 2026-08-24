from unittest.mock import Mock, patch

from src.sync_manager import SyncManager


def _make_sync_manager(sync_clients):
    db = Mock()
    db.get_all_settings.return_value = {}
    db.get_books_by_status.return_value = []
    db.get_all_books.return_value = []
    db.get_book_by_id.return_value = None

    with patch.object(SyncManager, "startup_checks"):
        manager = SyncManager(
            abs_client=Mock(),
            grimmory_client=Mock(),
            hardcover_client=Mock(),
            transcriber=Mock(),
            ebook_parser=Mock(),
            database_service=db,
            storyteller_client=Mock(),
            sync_clients=sync_clients,
            alignment_service=Mock(),
            library_service=Mock(cwa_client=None),
            migration_service=Mock(),
            suggestion_service=Mock(),
            background_job_service=Mock(),
            data_dir=None,
            books_dir=None,
            epub_cache_dir="/tmp/test_epub_cache",
        )

    manager.sync_clients = sync_clients
    return manager


def test_startup_checks_treats_false_return_as_failed_connection():
    client = Mock()
    client.is_configured.return_value = True
    client.check_connection.side_effect = [False, False]

    manager = _make_sync_manager({"Storyteller": client})

    with (
        patch("src.services.sync_manager_startup.time.sleep"),
        patch("src.services.sync_manager_startup.logger") as mock_logger,
    ):
        manager.startup_checks()

    assert client.check_connection.call_count == 2
    assert not any("connection verified" in call.args[0] for call in mock_logger.info.call_args_list)
    assert any("connection failed after retry" in call.args[0] for call in mock_logger.warning.call_args_list)


def test_startup_checks_logs_retry_success_only_when_second_attempt_succeeds():
    client = Mock()
    client.is_configured.return_value = True
    client.check_connection.side_effect = [False, True]

    manager = _make_sync_manager({"Storyteller": client})

    with (
        patch("src.services.sync_manager_startup.time.sleep"),
        patch("src.services.sync_manager_startup.logger") as mock_logger,
    ):
        manager.startup_checks()

    assert client.check_connection.call_count == 2
    assert any("connection verified (retry)" in call.args[0] for call in mock_logger.info.call_args_list)
    assert not any("connection failed after retry" in call.args[0] for call in mock_logger.warning.call_args_list)


def test_startup_checks_treats_legacy_migration_failure_as_non_fatal():
    hardcover_service = Mock()
    hardcover_client = Mock(hardcover_service=hardcover_service)
    migration_service = Mock()
    migration_service.migrate_legacy_data.side_effect = RuntimeError("legacy boom")

    manager = _make_sync_manager({"Hardcover": hardcover_client})
    manager._startup.migration_service = migration_service

    with (
        patch("src.services.sync_manager_startup.time.sleep"),
        patch("src.services.sync_manager_startup.logger") as mock_logger,
    ):
        manager.startup_checks()

    migration_service.migrate_legacy_data.assert_called_once()
    hardcover_service.backfill_hardcover_states.assert_called_once()
    assert any("Legacy migration failed (non-fatal)" in call.args[0] for call in mock_logger.warning.call_args_list)
