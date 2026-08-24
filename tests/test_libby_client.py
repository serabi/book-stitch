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


class TestPairing:
    def test_begin_pairing_returns_code_and_stores_token(self, client, mock_db):
        chip_response = make_response(200, {"identity": "new-identity-token"})
        generate_response = make_response(200, {"code": "87654321"})

        with (
            patch.object(client.session, "post", return_value=chip_response) as mock_post,
            patch.object(client.session, "request", return_value=generate_response) as mock_request,
        ):
            result = client.begin_pairing()

        assert result["success"] is True
        assert result["code"] == "87654321"
        # Chip creation hit sentry with the dewey client param
        assert mock_post.call_args.args[0] == "https://sentry.libbyapp.com/chip?client=dewey"
        # Code generation used the new chip's bearer token via GET (POST 404s)
        gen_call = mock_request.call_args
        assert gen_call.args[0] == "GET"
        assert gen_call.args[1].endswith("/chip/clone/code")
        assert gen_call.kwargs["headers"]["Authorization"] == "Bearer new-identity-token"
        # Token was persisted to DB and env
        mock_db.set_setting.assert_any_call("LIBBY_IDENTITY_TOKEN", "new-identity-token")
        assert os.environ.get("LIBBY_IDENTITY_TOKEN") == "new-identity-token"

    def test_begin_pairing_chip_network_failure(self, client):
        import requests as requests_lib

        with patch.object(client.session, "post", side_effect=requests_lib.ConnectionError("down")):
            result = client.begin_pairing()
        assert result["success"] is False
        assert result["error"] == "network_error"
        assert os.environ.get("LIBBY_IDENTITY_TOKEN") == ""

    def test_begin_pairing_generate_failure_clears_token(self, client):
        chip_response = make_response(200, {"identity": "tok"})
        generate_response = make_response(500)

        with (
            patch.object(client.session, "post", return_value=chip_response),
            patch.object(client.session, "request", return_value=generate_response),
        ):
            result = client.begin_pairing()

        assert result["success"] is False
        assert result["error"] == "http_error"
        assert os.environ.get("LIBBY_IDENTITY_TOKEN") == ""

    def test_check_pairing_incomplete_when_no_cards(self, client):
        state_body = {"result": "synchronized", "cards": []}
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}),
            patch.object(client.session, "request", return_value=make_response(200, state_body)),
        ):
            result = client.check_pairing()
        assert result == {"complete": False, "cards": []}

    def test_check_pairing_completes_with_cards_and_generates_device_id(self, client, mock_db):
        state_body = {
            "result": "synchronized",
            "cards": [{"id": "card-1", "niceName": "My Card", "library": {"key": "libkey", "name": "Town Library"}}],
        }
        with (
            patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}),
            patch.object(client.session, "request", return_value=make_response(200, state_body)),
        ):
            result = client.check_pairing()
            assert result["complete"] is True
            assert result["cards"][0]["library_key"] == "libkey"
            # Assert env persistence inside the patch.dict scope: exiting it
            # discards keys written while active.
            assert os.environ.get("LIBBY_DEVICE_ID")

        device_calls = [c for c in mock_db.set_setting.call_args_list if c.args[0] == "LIBBY_DEVICE_ID"]
        assert device_calls and device_calls[0].args[1]


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

    def test_active_loans_normalization_includes_psn_key_and_isbn(self, client):
        state_body = {
            "loans": [
                {
                    "cardId": "42",
                    "titleId": "99",
                    "expires": 1789395929,
                    "title": {
                        "id": "99",
                        "name": "A Book",
                        "type": {"id": "audiobook"},
                        "isbn": "9780123456789",
                        "authors": [{"name": "Author One"}, {"name": "Author Two"}],
                    },
                    "card": {"id": "42", "library": {"key": "lk"}},
                }
            ],
        }
        with patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}):
            with patch.object(client.session, "request", return_value=make_response(200, state_body)):
                loans = client.get_active_loans()

        assert len(loans) == 1
        loan = loans[0]
        assert loan["psn_key"] == "42-99"
        assert loan["format"] == "audiobook"
        assert loan["isbn"] == "9780123456789"
        assert loan["authors"] == "Author One, Author Two"
        assert loan["library_key"] == "lk"


class TestPassport:
    def test_passport_request_construction(self, client):
        passport_body = {
            "urls": {
                "web": "https://dewey-abc.read.libbyapp.com/",
                "possession": "https://dewey-abc.read.libbyapp.com/_d/possession",
            },
            "expires": 1789395929,
            "leeway": 3600,
        }
        with patch.object(client.session, "request", return_value=make_response(200, passport_body)) as mock_request:
            passport = client.get_passport("42", "99", library_key="lk")

        assert passport["urls"]["possession"].endswith("/_d/possession")
        call_args = mock_request.call_args
        url_arg = call_args.args[1]
        assert call_args.args[0] == "GET"
        # No Authorization header on passport-session endpoints
        assert "Authorization" not in call_args.kwargs["headers"]
        # tData decodes to the documented codex shape
        assert "t=" in url_arg and "website_id=lk" in url_arg
        t_param = url_arg.split("?t=")[1].split("&")[0]
        tdata = json.loads(base64.b64decode(t_param))
        assert tdata["codex"]["title"]["titleId"] == "99"
        assert tdata["codex"]["loan"]["psnKey"] == "42-99"
        assert tdata["codex"]["library"]["key"] == "lk"
        assert tdata["spec"] == "V22"

    def test_passport_resolves_library_key_from_cached_loans(self, client):
        state_body = {
            "loans": [
                {"cardId": "42", "titleId": "99", "card": {"id": "42", "library": {"key": "resolved-key"}}},
            ],
        }
        passport_body = {"urls": {"possession": "x"}}
        with patch.dict(os.environ, {"LIBBY_IDENTITY_TOKEN": "tok"}):
            client._last_sync_state = state_body
            with patch.object(
                client.session, "request", return_value=make_response(200, passport_body)
            ) as mock_request:
                result = client.get_passport("42", "99")

        assert result is not None
        url_arg = mock_request.call_args.args[1]
        assert "website_id=resolved-key" in url_arg


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
