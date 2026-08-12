from unittest.mock import Mock

from src.blueprints.helpers import find_ebook_file
from src.utils.epub_resolver import get_local_epub


def test_find_ebook_file_rejects_parent_traversal(tmp_path):
    outside = tmp_path / "outside.epub"
    outside.write_bytes(b"outside")
    books = tmp_path / "books"
    books.mkdir()

    assert find_ebook_file("../outside.epub", ebook_dir=books) is None


def test_find_ebook_file_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside.epub"
    outside.write_bytes(b"outside")
    books = tmp_path / "books"
    books.mkdir()
    (books / "linked.epub").symlink_to(outside)

    assert find_ebook_file("linked.epub", ebook_dir=books) is None


def test_epub_resolver_rejects_remote_traversal_filename(tmp_path):
    grimmory = Mock()
    grimmory.is_configured.return_value = True

    result = get_local_epub("../outside.epub", tmp_path / "books", tmp_path / "cache", grimmory)

    assert result is None
    grimmory.find_book_by_filename.assert_not_called()


def test_epub_resolver_rejects_cache_symlink_escape(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "outside.epub"
    outside.write_bytes(b"outside")
    (cache / "book.epub").symlink_to(outside)
    grimmory = Mock()
    grimmory.is_configured.return_value = False

    assert get_local_epub("book.epub", books, cache, grimmory) is None
