import json
from unittest.mock import MagicMock

import pytest

from src.api.thunder_client import ThunderClient
from src.db.models import Book, PendingSuggestion, TbrItem
from src.services.libby_service import LibbyService


@pytest.fixture
def db():
    db = MagicMock()
    db.get_all_books.return_value = []
    db.get_all_pending_suggestions.return_value = []
    return db


@pytest.fixture
def libby():
    libby = MagicMock()
    libby.is_configured.return_value = True
    return libby


@pytest.fixture
def thunder():
    thunder = MagicMock(spec=ThunderClient)
    thunder.extract_isbns = ThunderClient.extract_isbns
    thunder.extract_cover = ThunderClient.extract_cover
    return thunder


@pytest.fixture
def service(db, libby, thunder):
    return LibbyService(database_service=db, libby_client=libby, thunder_client=thunder)


def make_loan(psn="55881092-12503377", title="The Lost Metal", authors="Brandon Sanderson"):
    card_id, title_id = psn.split("-")
    return {
        "psn_key": psn,
        "card_id": card_id,
        "title_id": title_id,
        "title": title,
        "authors": authors,
        "format": "audiobook",
        "isbn": None,
        "expires": None,
        "library_key": "fairfax",
        "media_type": "audiobook",
        "isbns": ["9781250767286"],
        "cover_url": None,
        "reading_time": None,
    }


class TestThunder:
    def test_extract_isbns_from_formats(self):
        media = {"formats": [{"identifiers": [{"type": "ISBN", "value": "9781"}, {"type": "ASIN", "value": "x"}]}]}
        assert ThunderClient.extract_isbns(media) == ["9781"]
        assert ThunderClient.extract_isbns(None) == []


class TestMatching:
    def test_isbn_match_links_psn_key(self, service, db):
        details = MagicMock()
        details.isbn = "9781250767286"
        book = Book(title="The Lost Metal")
        book.hardcover_details = details
        book.id = 7
        db.get_all_books.return_value = [book]

        service.match_loans([make_loan()])

        assert book.libby_psn_key == "55881092-12503377"
        db.save_book.assert_called_once_with(book)

    def test_fuzzy_title_author_match(self, service, db):
        book = Book(title="The Lost Metal", author="Brandon Sanderson")
        book.id = 3
        db.get_all_books.return_value = [book]

        loan = make_loan()
        loan["isbns"] = []
        service.match_loans([loan])

        assert book.libby_psn_key == "55881092-12503377"

    def test_no_match_queues_suggestion(self, service, db):
        db.get_all_books.return_value = [Book(title="Something Else Entirely")]
        saved = []
        db.save_pending_suggestion.side_effect = lambda s: saved.append(s)

        loan = make_loan()
        loan["isbns"] = []
        service.match_loans([loan])

        assert len(saved) == 1
        assert saved[0].source == "libby_loan"
        assert saved[0].source_id == "55881092-12503377"
        assert isinstance(saved[0], PendingSuggestion)

    def test_already_linked_skipped(self, service, db):
        book = Book(title="The Lost Metal", libby_psn_key="55881092-12503377")
        book.id = 1
        db.get_all_books.return_value = [book]

        service.match_loans([make_loan()])

        db.save_book.assert_not_called()


class TestLifecycle:
    def test_missing_loan_unlinks_and_journals(self, service, db):
        book = Book(title="Returned Book", libby_psn_key="gone-card-gone")
        book.id = 9
        db.get_all_books.return_value = [book]

        service.handle_lifecycle([make_loan()])

        assert book.libby_psn_key is None
        db.save_book.assert_called_once_with(book)
        journal_call = db.add_reading_journal.call_args
        assert journal_call.kwargs["book_id"] == 9
        assert journal_call.kwargs["event"] == "note"
        assert "returned" in journal_call.kwargs["entry"].lower()

    def test_active_loan_untouched(self, service, db):
        book = Book(title="Still Out", libby_psn_key="55881092-12503377")
        book.id = 2
        db.get_all_books.return_value = [book]

        service.handle_lifecycle([make_loan()])

        assert book.libby_psn_key == "55881092-12503377"
        db.save_book.assert_not_called()
        db.add_reading_journal.assert_not_called()


class TestReadingTimeBackfill:
    def test_delta_above_threshold_journals(self, service, db):
        book = Book(title="Lost Metal", libby_psn_key="65497484-8209392")
        book.id = 5
        db.get_all_books.return_value = [book]
        loan = make_loan("65497484-8209392")

        loan["reading_time"] = 43491
        service._maybe_journal_reading_time(loan)  # baseline established
        loan["reading_time"] = 44200  # +709s
        service._maybe_journal_reading_time(loan)

        assert db.add_reading_journal.called
        entry = db.add_reading_journal.call_args.kwargs["entry"]
        assert "11 min" in entry or "12 min" in entry

    def test_small_delta_ignored(self, service, db):
        book = Book(title="X", libby_psn_key="a-b")
        book.id = 1
        db.get_all_books.return_value = [book]
        loan = make_loan("a-b")
        loan["reading_time"] = 10000
        service._maybe_journal_reading_time(loan)
        loan["reading_time"] = 10100  # +100s < threshold
        service._maybe_journal_reading_time(loan)

        db.add_reading_journal.assert_not_called()


class TestTbrCuration:
    def test_holds_become_review_suggestions(self, service, db, libby):
        libby.get_sync_state.return_value = {
            "cards": [{"id": "42"}],
            "holds": [{"cardId": "42", "id": "tid1", "title": "Hold Book", "firstCreatorName": "Auth"}],
        }
        db.get_all_pending_suggestions.return_value = []
        saved = []
        db.save_pending_suggestion.side_effect = lambda s: saved.append(s)

        service.sync_hold_suggestions()

        assert len(saved) == 1
        assert saved[0].source == "libby_hold"
        assert saved[0].title == "Hold Book"

    def test_merge_creates_tbr_items_only_on_explicit_action(self, service, db):
        sugg = PendingSuggestion(source="libby_hold", source_id="hold:42:tid1", title="Hold Book", author="Auth")
        sugg.id = 11
        sugg.status = "pending"
        db.get_all_pending_suggestions.return_value = [sugg]
        added = []
        db.add_tbr_item.side_effect = lambda title, **kw: added.append((title, kw.get("source")))

        merged = service.merge_tbr_items([11])

        assert merged == 1
        assert added == [("Hold Book", "libby")]
        assert sugg.status == "merged"

    def test_dismiss_marks_without_tbr(self, service, db):
        sugg = PendingSuggestion(source="libby_hold", source_id="hold:42:x", title="Nope")
        sugg.id = 12
        sugg.status = "pending"
        db.get_all_pending_suggestions.return_value = [sugg]

        dismissed = service.dismiss_tbr_items([12])

        assert dismissed == 1
        assert sugg.status == "dismissed"
        db.add_tbr_item.assert_not_called()

    def test_suggestions_json_serializable(self, service, db, libby):
        libby.get_sync_state.return_value = {
            "cards": [{"id": "42"}],
            "holds": [{"cardId": "42", "id": "t", "title": "T"}],
        }
        captured = {}
        db.save_pending_suggestion.side_effect = lambda s: captured.setdefault("s", s)
        db.get_all_pending_suggestions.return_value = []

        service.sync_hold_suggestions()

        json.loads(captured["s"].matches_json)  # raises if malformed


class TestRefreshWiring:
    def test_refresh_passes_positions_and_runs_pipeline(self, service, db, libby, thunder):
        loan = make_loan()
        loan["reading_time"] = None
        libby.get_active_loans.return_value = [loan]
        thunder.get_media.return_value = {"formats": [{"identifiers": [{"type": "ISBN", "value": "9781"}]}]}

        service.refresh(positions={})

        assert loan["isbns"] == ["9781"]
        db.save_book.assert_not_called()  # nothing matched in an empty library

    def test_refresh_noop_when_sync_fails(self, service, db, libby):
        libby.get_active_loans.return_value = None
        service.refresh({})
        db.save_book.assert_not_called()


class TestTbrModelCompat:
    def test_tbr_item_accepts_libby_source(self):
        item = TbrItem(title="T", source="libby")
        assert item.source == "libby"
