"""
Tests for ebook-only cross-format normalization in SyncManager.

When multiple ebook clients sync the same book without ABS, positions
are normalized to character offsets in the shared EPUB text.
"""

import os
import sys
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('DATA_DIR', 'test_data')
os.environ.setdefault('BOOKS_DIR', 'test_data')

from src.sync_clients.sync_client_interface import LocatorResult, ServiceState


def _make_state(pct, **extra):
    """Build a minimal ServiceState for testing."""
    current = {'pct': pct, **extra}
    return ServiceState(
        current=current,
        previous_pct=0.0,
        delta=pct,
        threshold=0.001,
        is_configured=True,
        display=('test', 'test'),
        value_formatter=lambda p: f"{p:.2%}",
    )


def _make_book(ebook_filename='book.epub', abs_id='test-123', transcript_file=None):
    book = MagicMock()
    book.ebook_filename = ebook_filename
    book.abs_id = abs_id
    book.transcript_file = transcript_file
    return book


def _make_manager(sync_clients=None, ebook_parser=None):
    """Create a SyncManager with mocked dependencies."""
    from src.sync_manager import SyncManager

    mgr = SyncManager.__new__(SyncManager)
    mgr.sync_clients = sync_clients or {}
    mgr.ebook_parser = ebook_parser or MagicMock()
    mgr.alignment_service = None
    return mgr


# ── Normalization returns char offsets with 2+ ebook clients ───────────

class TestEbookOnlyNormalization:

    def test_returns_char_offsets_via_text_matching(self):
        """When clients provide text snippets that can be located in the EPUB,
        normalization should return match_index character offsets."""
        full_text = "A" * 100_000
        parser = MagicMock()
        parser.resolve_book_path.return_value = '/books/book.epub'
        parser.extract_text_and_map.return_value = (full_text, [])
        parser.find_text_location.side_effect = [
            LocatorResult(percentage=0.30, match_index=30_000),
            LocatorResult(percentage=0.50, match_index=50_000),
        ]

        client_a = MagicMock()
        client_a.get_text_from_current_state.return_value = "some text near 30%"
        client_b = MagicMock()
        client_b.get_text_from_current_state.return_value = "some text near 50%"

        mgr = _make_manager(
            sync_clients={'KoSync': client_a, 'Booklore': client_b},
            ebook_parser=parser,
        )

        config = {
            'KoSync': _make_state(0.30),
            'Booklore': _make_state(0.50),
        }
        result = mgr._normalize_for_cross_format_comparison(_make_book(), config)

        assert result == {'KoSync': 30_000, 'Booklore': 50_000}

    def test_returns_none_with_single_ebook_client(self):
        """With only one ebook client, there's nothing to compare — return None."""
        mgr = _make_manager(sync_clients={'KoSync': MagicMock()})
        config = {'KoSync': _make_state(0.50)}
        result = mgr._normalize_for_cross_format_comparison(_make_book(), config)
        assert result is None

    def test_returns_none_without_ebook_filename(self):
        """Without an ebook file, normalization is impossible."""
        mgr = _make_manager(sync_clients={'KoSync': MagicMock(), 'Booklore': MagicMock()})
        config = {
            'KoSync': _make_state(0.30),
            'Booklore': _make_state(0.50),
        }
        result = mgr._normalize_for_cross_format_comparison(
            _make_book(ebook_filename=None), config
        )
        assert result is None

    def test_falls_back_to_pct_when_text_extraction_fails(self):
        """When get_text_from_current_state returns None, fall back to pct * total_len."""
        full_text = "B" * 80_000
        parser = MagicMock()
        parser.resolve_book_path.return_value = '/books/book.epub'
        parser.extract_text_and_map.return_value = (full_text, [])

        client_a = MagicMock()
        client_a.get_text_from_current_state.return_value = None
        client_b = MagicMock()
        client_b.get_text_from_current_state.return_value = None

        mgr = _make_manager(
            sync_clients={'KoSync': client_a, 'Booklore': client_b},
            ebook_parser=parser,
        )

        config = {
            'KoSync': _make_state(0.25),
            'Booklore': _make_state(0.75),
        }
        result = mgr._normalize_for_cross_format_comparison(_make_book(), config)

        assert result == {'KoSync': 20_000, 'Booklore': 60_000}

    def test_falls_back_to_pct_when_find_text_location_returns_none(self):
        """When text is found but can't be located in the EPUB, fall back to pct."""
        full_text = "C" * 50_000
        parser = MagicMock()
        parser.resolve_book_path.return_value = '/books/book.epub'
        parser.extract_text_and_map.return_value = (full_text, [])
        parser.find_text_location.return_value = None

        client_a = MagicMock()
        client_a.get_text_from_current_state.return_value = "snippet"
        client_b = MagicMock()
        client_b.get_text_from_current_state.return_value = "another snippet"

        mgr = _make_manager(
            sync_clients={'KoSync': client_a, 'Booklore': client_b},
            ebook_parser=parser,
        )

        config = {
            'KoSync': _make_state(0.40),
            'Booklore': _make_state(0.60),
        }
        result = mgr._normalize_for_cross_format_comparison(_make_book(), config)

        assert result == {'KoSync': 20_000, 'Booklore': 30_000}


# ── Integration: _determine_leader picks correct leader via char offsets ──

class TestDetermineLeaderEbookOnly:

    def test_leader_chosen_by_char_offset_not_raw_pct(self):
        """When two ebook clients disagree, the leader should be chosen by
        normalized char offset, not raw percentage (which may differ across readers)."""
        full_text = "D" * 100_000
        parser = MagicMock()
        parser.resolve_book_path.return_value = '/books/book.epub'
        parser.extract_text_and_map.return_value = (full_text, [])
        # KoSync's text resolves to char 60000 (ahead), Booklore to char 40000
        parser.find_text_location.side_effect = [
            LocatorResult(percentage=0.45, match_index=60_000),
            LocatorResult(percentage=0.50, match_index=40_000),
        ]

        client_ko = MagicMock()
        client_ko.can_be_leader.return_value = True
        client_ko.get_text_from_current_state.return_value = "text at 60k"

        client_bl = MagicMock()
        client_bl.can_be_leader.return_value = True
        client_bl.get_text_from_current_state.return_value = "text at 40k"

        mgr = _make_manager(
            sync_clients={'KoSync': client_ko, 'Booklore': client_bl},
            ebook_parser=parser,
        )

        # Booklore has higher raw pct (0.50 vs 0.45), but KoSync is
        # actually further ahead by char offset (60000 vs 40000)
        config = {
            'KoSync': _make_state(0.45),
            'Booklore': _make_state(0.50),
        }

        # Mock _has_significant_delta so both clients appear changed
        with patch.object(mgr, '_has_significant_delta', return_value=True):
            leader, leader_pct = mgr._determine_leader(
                config, _make_book(), 'test-123', 'Test Book'
            )

        assert leader == 'KoSync'
        assert leader_pct == 0.45
