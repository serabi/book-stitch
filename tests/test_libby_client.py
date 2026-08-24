import base64
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from src.api.libby_client import LibbyClient


@pytest.fixture
def mock_db():
    return MagicMock()


def make_response(status_code=200, json_body=None):
    response = MagicMock()
    response.status_code = status_code
    if json_body is not None:
        response.json.return_value = json_body
    else:
        response.json.side_effect = ValueError("no json")
    return response


@pytest.fixture
def client(mock_db):
    with patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "", "LIBBY_DEVICE_ID": "", "LIBBY_ENABLED": "true"}):
        yield LibbyClient(database_service=mock_db)


def make_jwt(payload: dict) -> str:
    import base64 as b64

    def enc(obj):
        return b64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'RS256'})}.{enc(payload)}.sig"


VALID_TOKEN = make_jwt({"aud": "readiverse", "iss": "sentry", "chip": {"id": "abc"}})


class TestPairing:
    def test_malformed_token_rejected(self, client):
        for bad in ["", "not-a-jwt", "a.b.c", "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ3cm9uZyJ9.sig"]:
            result = client.pair_with_identity_token(bad)
            assert result["success"] is False
            assert result["error"] == "invalid_token_format"
        # Nothing was persisted
        assert os.environ.get("LIBBY_IDENTITY_TOKEN") == ""

    def test_pairing_success_stores_token_and_cards(self, client, mock_db):
        cards_payload = {
            "result": "synchronized",
            "cards": [
                {
                    "cardId": "card-1",
                    "cardName": "My Card",
                    "advantageKey": "libkey",
                    "library": {"name": "Town Library", "websiteId": "visit_town"},
                },
            ],
        }
        sync_response = make_response(200, cards_payload)

        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": ""}),
            patch.object(client.session, "request", return_value=sync_response),
        ):
            result = client.pair_with_identity_token(VALID_TOKEN)
            # Env check must run inside the patch.dict scope — exiting it
            # discards keys written while active.
            assert client.identity_token == VALID_TOKEN

        assert result["success"] is True
        assert result["cards"][0]["library_key"] == "libkey"
        assert result["cards"][0]["name"] == "My Card"
        # Token persisted to DB
        mock_db.set_setting.assert_any_call("LIBBY_IDENTITY_TOKEN", VALID_TOKEN)
        # Device id was generated and persisted
        device_calls = [c for c in mock_db.set_setting.call_args_list if c.args[0] == "LIBBY_DEVICE_ID"]
        assert device_calls and device_calls[0].args[1]

    def test_revoked_or_empty_token_reported_and_cleared(self, client):
        for body in [{"result": "missing_chip"}, {"result": "synchronized", "cards": []}]:
            with (
                patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": ""}),
                patch.object(client.session, "request", return_value=make_response(200, body)),
            ):
                result = client.pair_with_identity_token(VALID_TOKEN)
            assert result["success"] is False
            assert result["error"] == "revoked_token"
            assert os.environ.get("LIBBY_IDENTITY_TOKEN") == ""

    def test_sync_failure_during_pairing(self, client):
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": ""}),
            patch.object(client.session, "request", return_value=None),
        ):
            result = client.pair_with_identity_token(VALID_TOKEN)
        assert result["success"] is False
        assert result["error"] == "network_error"
        assert os.environ.get("LIBBY_IDENTITY_TOKEN") == ""


class TestSyncState:
    def test_get_sync_state_parses_loans_holds_cards(self, client):
        state_body = {
            "loans": [{"cardId": "c1", "titleId": "t1"}],
            "holds": [{"cardId": "c1", "titleId": "t2"}],
            "cards": [{"id": "c1"}],
        }
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}),
            patch.object(client.session, "request", return_value=make_response(200, state_body)) as mock_request,
        ):
            state = client.get_sync_state(force=True)

        assert state == state_body
        call_kwargs = mock_request.call_args.kwargs
        assert call_kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_get_sync_state_failure_returns_none(self, client):
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}),
            patch.object(client.session, "request", return_value=make_response(500)),
        ):
            assert client.get_sync_state(force=True) is None

    def test_get_sync_state_without_token_returns_none(self, client):
        with patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": ""}):
            assert client.get_sync_state(force=True) is None

    def test_active_loans_normalization_flat_payload(self, client):
        state_body = {
            "loans": [
                {
                    "id": "99",
                    "cardId": "42",
                    "title": "A Book",
                    "firstCreatorName": "Author One",
                    "type": {"id": "audiobook"},
                    "expireDate": "2026-09-12",
                    "websiteId": "visit_lib",
                }
            ],
        }
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}),
            patch.object(client.session, "request", return_value=make_response(200, state_body)),
        ):
            loans = client.get_active_loans()

        assert len(loans) == 1
        loan = loans[0]
        assert loan["psn_key"] == "42-99"
        assert loan["media_type"] == "audiobook"
        assert loan["format"] == "audiobook"
        assert loan["title"] == "A Book"
        assert loan["authors"] == "Author One"
        assert loan["expires"] == "2026-09-12"
        assert loan["library_key"] == "visit_lib"
        # Loans carry no ISBN directly
        assert loan["isbn"] is None


class TestPassport:
    def test_passport_request_construction_and_cookie_handshake(self, client):
        passport_body = {
            "urls": {
                "web": "https://dewey-abc.read.libbyapp.com/",
                "possession": "https://dewey-abc.read.libbyapp.com/_d/possession",
            },
            "message": "m=signedblob",
            "expires": 1789395929,
            "leeway": 3600,
        }
        with (
            patch.object(client.session, "request", return_value=make_response(200, passport_body)) as mock_request,
            patch.object(client.session, "head") as mock_head,
        ):
            with patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}):
                passport = client.get_passport("42", "99", media_type="audiobook")

        assert passport["urls"]["possession"].endswith("/_d/possession")
        open_call = mock_request.call_args
        assert open_call.args[0] == "GET"
        url_arg = open_call.args[1]
        assert "/open/audiobook/card/42/title/99?" in url_arg
        assert "website_id=" in url_arg
        # Bearer auth on the sentry open call
        assert open_call.kwargs["headers"]["Authorization"] == "Bearer tok"
        # tData decodes to codex context with dewey-url and spec
        t_param = url_arg.split("?t=")[1].split("&")[0]
        tdata = json.loads(base64.b64decode(t_param))
        assert tdata["codex"]["title"]["titleId"] == "99"
        assert tdata["codex"]["loan"]["psnKey"] == "42-99"
        assert tdata["dewey-url"] == "https://libbyapp.com"
        assert tdata["spec"] == "V22"
        # Cookie handshake: unauthenticated HEAD to web url with message
        mock_head.assert_called_once_with(
            "https://dewey-abc.read.libbyapp.com/?m=signedblob",
            headers={"Accept": "*/*"},
            timeout=10,
        )

    def test_media_type_path_mapping(self):
        assert LibbyClient._media_type_path("audiobook") == "audiobook"
        assert LibbyClient._media_type_path("magazine") == "magazine"
        assert LibbyClient._media_type_path("ebook") == "book"
        assert LibbyClient._media_type_path(None) == "book"


class TestPossession:
    def test_possession_parses_position_statistics(self, client):
        possession_body = {
            "timestamps": {"updated": 1787577677},
            "position": {"percentageOfBook": 0.7703, "spinePosition": 12},
            "marks": {"bookmarks": [], "highlights": []},
            "statistics": {"readingTime": 3600, "positions": 12},
        }
        with patch.object(client.session, "get", return_value=make_response(200, possession_body)) as mock_get:
            data = client.get_possession("https://dewey-abc.read.libbyapp.com/_d/possession")

        assert data["position"]["percentageOfBook"] == 0.7703
        assert data["statistics"]["readingTime"] == 3600
        args, kwargs = mock_get.call_args
        assert "Authorization" not in kwargs.get("headers", {})

    def test_possession_404_returns_none(self, client):
        with patch.object(client.session, "get", return_value=make_response(404)):
            assert client.get_possession("https://host/_d/possession") is None


class TestDisconnect:
    def test_disconnect_revokes_chip(self, client):
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}),
            patch.object(client.session, "request", return_value=make_response(200)) as mock_request,
        ):
            assert client.disconnect() is True
        assert mock_request.call_args.args[1].endswith("/chip/revoke")

    def test_disconnect_treats_already_invalid_chip_as_success(self, client):
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}),
            patch.object(client.session, "request", return_value=make_response(401)),
        ):
            assert client.disconnect() is True

    def test_disconnect_network_failure_is_false(self, client):
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}),
            patch.object(client.session, "request", return_value=None),
        ):
            assert client.disconnect() is False

    def test_clear_credentials_wipes_settings(self, client, mock_db):
        with patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok", "LIBBY_DEVICE_ID": "dev"}):
            client.clear_credentials()
        assert os.environ.get("LIBBY_IDENTITY_TOKEN") == ""
        assert os.environ.get("LIBBY_DEVICE_ID") == ""
        mock_db.set_setting.assert_any_call("LIBBY_IDENTITY_TOKEN", "")
        mock_db.set_setting.assert_any_call("LIBBY_DEVICE_ID", "")


class TestConfiguration:
    def test_is_configured_requires_token_and_not_disabled(self, mock_db):
        with patch.dict(os.environ, {"LIBBY_ENABLED": "true", "LIBBY_IDENTITY_TOKEN": "tok"}):
            assert LibbyClient(database_service=mock_db).is_configured() is True
        with patch.dict(os.environ, {"LIBBY_ENABLED": "false", "LIBBY_IDENTITY_TOKEN": "tok"}):
            assert LibbyClient(database_service=mock_db).is_configured() is False
        with patch.dict(os.environ, {"LIBBY_ENABLED": "true", "LIBBY_IDENTITY_TOKEN": ""}):
            assert LibbyClient(database_service=mock_db).is_configured() is False

    def test_poll_mins_floor_enforced(self, mock_db):
        with patch.dict(os.environ, {"LIBBY_POLL_MINS": "5"}):
            assert LibbyClient(database_service=mock_db).poll_mins == 10
        with patch.dict(os.environ, {"LIBBY_POLL_MINS": ""}):
            assert LibbyClient(database_service=mock_db).poll_mins == 60

    def test_check_connection_reports_card_count(self, client):
        state_body = {"cards": [{"id": "c1"}, {"id": "c2"}]}
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}),
            patch.object(client.session, "request", return_value=make_response(200, state_body)),
        ):
            assert client.check_connection() is True
