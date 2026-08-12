"""Timeout coverage for Audiobookshelf collection writes."""

from unittest.mock import Mock

import requests

from src.api.api_clients import ABSClient


def _response(status_code, payload=None):
    response = Mock(status_code=status_code)
    response.json.return_value = payload or {}
    return response


def test_add_to_collection_bounds_every_request():
    client = ABSClient()
    client.session = Mock()
    client.session.get.side_effect = [
        _response(200, {"collections": []}),
        _response(200, {"libraries": [{"id": "library-1"}]}),
        _response(200, {"media": {"metadata": {"title": "Book"}}}),
    ]
    client.session.post.side_effect = [
        _response(201, {"id": "collection-1"}),
        _response(204),
    ]

    assert client.add_to_collection("book-1", "Synced") is True
    assert all(call.kwargs["timeout"] == client.timeout for call in client.session.get.call_args_list)
    assert all(call.kwargs["timeout"] == client.timeout for call in client.session.post.call_args_list)


def test_add_to_collection_timeout_is_retriable_false_result():
    client = ABSClient()
    client.session = Mock()
    client.session.get.side_effect = requests.Timeout("slow ABS")

    assert client.add_to_collection("book-1", "Synced") is False
    assert client.session.get.call_args.kwargs["timeout"] == client.timeout
