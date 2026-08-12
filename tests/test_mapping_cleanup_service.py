from types import SimpleNamespace
from unittest.mock import Mock

from src.api.grimmory_client import GrimmoryClientGroup
from src.services.mapping_cleanup_service import cleanup_mapping_resources


def test_cleanup_removes_same_filename_only_from_mapped_grimmory_server(tmp_path):
    primary = Mock(instance_id="default")
    primary.is_configured.return_value = True
    secondary = Mock(instance_id="2")
    secondary.is_configured.return_value = True
    secondary.remove_from_shelf.return_value = True
    group = GrimmoryClientGroup([primary, secondary])
    db = Mock()
    db.get_kosync_document.return_value = SimpleNamespace(source="grimmory", grimmory_id="2:22")
    container = Mock()
    container.data_dir.return_value = tmp_path
    container.epub_cache_dir.return_value = tmp_path / "cache"
    manager = Mock(epub_cache_dir=None)
    book = SimpleNamespace(
        abs_id="abs-2",
        ebook_filename="same.epub",
        original_ebook_filename=None,
        kosync_doc_id="hash-2",
        sync_mode="audiobook",
        transcript_file=None,
    )

    cleanup_mapping_resources(
        book,
        container=container,
        manager=manager,
        database_service=db,
        abs_service=Mock(),
        grimmory_client=group,
    )

    primary.remove_from_shelf.assert_not_called()
    secondary.remove_from_shelf.assert_called_once_with("same.epub", None)
