"""Tests for Storyteller wordTimeline alignment."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.storyteller_api import StorytellerAPIClient
from src.services.alignment_service import AlignmentService
from src.utils.polisher import Polisher


class TestWordTimelineChapterLoading(unittest.TestCase):
    """Test loading wordTimeline data from filesystem."""

    def setUp(self):
        self.client = StorytellerAPIClient()
        self.tmpdir = tempfile.mkdtemp()

    def _mock_book_details(self, title, suffix=''):
        """Patch get_book_details to return a fake book."""
        return patch.object(
            self.client, 'get_book_details',
            return_value={'title': title, 'suffix': suffix}
        )

    def test_no_assets_dir(self):
        os.environ.pop('STORYTELLER_ASSETS_DIR', None)
        result = self.client.get_word_timeline_chapters('test-uuid')
        self.assertIsNone(result)

    def test_missing_book_dir(self):
        os.environ['STORYTELLER_ASSETS_DIR'] = self.tmpdir
        with self._mock_book_details('Nonexistent Book'):
            result = self.client.get_word_timeline_chapters('nonexistent-uuid')
        self.assertIsNone(result)

    def test_loads_chapters_with_word_timeline(self):
        """Test loading wordTimeline data from title-based directory."""
        book_dir = os.path.join(self.tmpdir, 'assets', 'Meet Molly', 'transcriptions')
        os.makedirs(book_dir)

        timeline_data = {
            'wordTimeline': [
                {'type': 'word', 'text': 'Recorded', 'startTime': 0.361, 'endTime': 0.701},
                {'type': 'word', 'text': 'Books', 'startTime': 0.701, 'endTime': 0.961},
            ]
        }
        with open(os.path.join(book_dir, '00000-00001.json'), 'w') as f:
            json.dump(timeline_data, f)

        os.environ['STORYTELLER_ASSETS_DIR'] = self.tmpdir
        with self._mock_book_details('Meet Molly'):
            result = self.client.get_word_timeline_chapters('uuid-123')

        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]['words']), 2)

    def test_loads_chapters_with_suffix(self):
        """Test that books with suffix (duplicates) find the correct directory."""
        book_dir = os.path.join(self.tmpdir, 'assets', 'Mailman [MDDhkjo5]', 'transcriptions')
        os.makedirs(book_dir)

        timeline_data = {
            'wordTimeline': [
                {'type': 'word', 'text': 'Chapter', 'startTime': 1.0, 'endTime': 1.5},
            ]
        }
        with open(os.path.join(book_dir, '00001-00001.json'), 'w') as f:
            json.dump(timeline_data, f)

        os.environ['STORYTELLER_ASSETS_DIR'] = self.tmpdir
        with self._mock_book_details('Mailman', ' [MDDhkjo5]'):
            result = self.client.get_word_timeline_chapters('uuid-456')

        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)

    def test_timeline_fallback_key(self):
        """Test that 'timeline' key works as fallback for 'wordTimeline'."""
        book_dir = os.path.join(self.tmpdir, 'assets', 'Test Book', 'transcriptions')
        os.makedirs(book_dir)

        timeline_data = {
            'timeline': [
                {'startTime': 1.0, 'text': 'Chapter'},
                {'startTime': 1.5, 'text': 'two'},
            ]
        }
        with open(os.path.join(book_dir, '00002-00001.json'), 'w') as f:
            json.dump(timeline_data, f)

        os.environ['STORYTELLER_ASSETS_DIR'] = self.tmpdir
        with self._mock_book_details('Test Book'):
            result = self.client.get_word_timeline_chapters('uuid-789')

        self.assertIsNotNone(result)
        self.assertEqual(len(result[0]['words']), 2)

    def test_ignores_invalid_filenames(self):
        """Non-matching filenames should be skipped."""
        book_dir = os.path.join(self.tmpdir, 'assets', 'Test Book', 'transcriptions')
        os.makedirs(book_dir)

        with open(os.path.join(book_dir, 'metadata.json'), 'w') as f:
            json.dump({'info': 'not a chapter'}, f)

        os.environ['STORYTELLER_ASSETS_DIR'] = self.tmpdir
        with self._mock_book_details('Test Book'):
            result = self.client.get_word_timeline_chapters('uuid-999')
        self.assertIsNone(result)

    def tearDown(self):
        os.environ.pop('STORYTELLER_ASSETS_DIR', None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestStorytellerAlignment(unittest.TestCase):
    """Test the align_storyteller_and_store method."""

    def setUp(self):
        self.mock_db = MagicMock()
        self.polisher = Polisher()
        self.service = AlignmentService(self.mock_db, self.polisher)

    def test_empty_chapters_returns_false(self):
        result = self.service.align_storyteller_and_store('abs-1', [], 'Some ebook text here')
        self.assertFalse(result)

    def test_empty_words_returns_false(self):
        chapters = [{'words': []}]
        result = self.service.align_storyteller_and_store('abs-1', chapters, 'Some text')
        self.assertFalse(result)

    def test_builds_segments_and_aligns(self):
        """Basic test that word timeline data produces an alignment."""
        # Create words that match some ebook text
        words = []
        text = "The quick brown fox jumps over the lazy dog and then some more words to fill out the text"
        for i, word in enumerate(text.split()):
            words.append({'startTime': float(i) * 0.5, 'word': word})

        chapters = [{'words': words}]

        result = self.service.align_storyteller_and_store('abs-test', chapters, text)
        self.assertTrue(result)

        # Verify _save_alignment was called
        self.mock_db.get_session.assert_called()

    def test_linear_fallback_on_no_anchors(self):
        """When N-gram anchoring fails, a linear fallback map should be created."""
        words = [
            {'startTime': 0.0, 'word': 'xyz'},
            {'startTime': 1.0, 'word': 'abc'},
        ]
        chapters = [{'words': words}]
        # Ebook text has completely different content
        ebook_text = "Completely different content that shares no words with the transcript"

        result = self.service.align_storyteller_and_store('abs-fallback', chapters, ebook_text)
        self.assertTrue(result)


class TestAlignmentValidation(unittest.TestCase):
    """Test alignment map validation on load."""

    def setUp(self):
        self.mock_db = MagicMock()
        self.polisher = Polisher()
        self.service = AlignmentService(self.mock_db, self.polisher)

    def test_valid_points_pass(self):
        from src.db.models import BookAlignment
        mock_entry = MagicMock()
        mock_entry.alignment_map_json = json.dumps([
            {'char': 0, 'ts': 0.0},
            {'char': 100, 'ts': 10.5},
        ])
        session_mock = MagicMock()
        session_mock.query.return_value.filter_by.return_value.first.return_value = mock_entry
        self.mock_db.get_session.return_value.__enter__ = MagicMock(return_value=session_mock)
        self.mock_db.get_session.return_value.__exit__ = MagicMock(return_value=False)

        result = self.service._get_alignment('test-id')
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {'char': 0, 'ts': 0.0})

    def test_invalid_points_skipped(self):
        mock_entry = MagicMock()
        mock_entry.alignment_map_json = json.dumps([
            {'char': 0, 'ts': 0.0},
            {'bad': 'data'},
            {'char': 'not_a_number', 'ts': 5.0},
            {'char': 100, 'ts': 10.0},
        ])
        session_mock = MagicMock()
        session_mock.query.return_value.filter_by.return_value.first.return_value = mock_entry
        self.mock_db.get_session.return_value.__enter__ = MagicMock(return_value=session_mock)
        self.mock_db.get_session.return_value.__exit__ = MagicMock(return_value=False)

        result = self.service._get_alignment('test-id')
        # Only the valid points should remain
        self.assertEqual(len(result), 2)


if __name__ == '__main__':
    unittest.main()
