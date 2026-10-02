"""Unit tests for error mapping, retries, and key handling.

These tests never touch the network: they stub out
``requests.Session.request`` with canned responses.
"""

import json
import os

import pytest
import requests

from captionpack import (
    API_KEY_ENV_VAR,
    AuthenticationError,
    CaptionPackError,
    Client,
    InsufficientCreditsError,
    InvalidParamsError,
    RateLimitedError,
    ServerError,
)


def _fake_response(status_code, payload=None, headers=None, raw_body=None):
    resp = requests.Response()
    resp.status_code = status_code
    if raw_body is not None:
        resp._content = raw_body
    else:
        resp._content = json.dumps(payload or {}).encode()
    resp.headers.update(headers or {})
    return resp


def _client_with_stub(monkeypatch, responses, sleep_recorder=None):
    """Build a Client whose session.request yields the given responses."""
    calls = {"n": 0, "sleeps": []}
    recorder = sleep_recorder if sleep_recorder is not None else calls["sleeps"]

    def fake_request(self, method, url, **kwargs):
        idx = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[idx]

    def fake_sleep(seconds):
        recorder.append(seconds)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr("captionpack.client.time.sleep", fake_sleep)
    client = Client(api_key="cp_live_test")
    return client, calls


def test_missing_key_raises_value_error(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    with pytest.raises(ValueError, match="API key is required"):
        Client(api_key=None)


def test_key_from_env_var(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV_VAR, "cp_live_from_env")
    monkeypatch.delenv("nope", raising=False)
    client = Client(api_key=None)
    assert client.session.headers["Authorization"] == "Bearer cp_live_from_env"


def test_401_maps_to_authentication_error(monkeypatch):
    client, _ = _client_with_stub(
        monkeypatch,
        [_fake_response(401, {"error": {"code": "invalid_key", "message": "bad key"}})],
    )
    with pytest.raises(AuthenticationError) as exc_info:
        client.balance()
    assert exc_info.value.code == "invalid_key"
    assert exc_info.value.status_code == 401


def test_400_maps_to_invalid_params_error(monkeypatch):
    client, _ = _client_with_stub(
        monkeypatch,
        [_fake_response(400, {"error": {"code": "invalid_params", "message": "nope"}})],
    )
    with pytest.raises(InvalidParamsError):
        client.caption_pack(topic="t", audience="a", tone="bogus")


def test_402_maps_to_insufficient_credits_with_top_up_url(monkeypatch):
    client, _ = _client_with_stub(
        monkeypatch,
        [
            _fake_response(
                402,
                {
                    "error": {
                        "code": "insufficient_credits",
                        "message": "Out of credits.",
                        "top_up_url": "https://example.com/topup",
                    }
                },
            )
        ],
    )
    with pytest.raises(InsufficientCreditsError) as exc_info:
        client.caption_pack(topic="t", audience="a")
    assert exc_info.value.top_up_url == "https://example.com/topup"


def test_429_retries_once_then_succeeds(monkeypatch):
    ok = _fake_response(200, {"credits": 7, "key_id": 1})
    client, calls = _client_with_stub(
        monkeypatch,
        [_fake_response(429, {"error": {"code": "rate_limited", "message": "slow"}}), ok],
    )
    assert client.balance().credits == 7
    assert calls["n"] == 2  # one retry happened
    assert calls["sleeps"] == [1.0]  # default backoff, no Retry-After header


def test_429_honors_retry_after_header(monkeypatch):
    ok = _fake_response(200, {"credits": 3, "key_id": 1})
    client, calls = _client_with_stub(
        monkeypatch,
        [
            _fake_response(
                429, {"error": {"code": "rate_limited"}}, headers={"Retry-After": "2"}
            ),
            ok,
        ],
    )
    assert client.balance().credits == 3
    assert calls["sleeps"] == [2.0]


def test_429_twice_raises_rate_limited_error(monkeypatch):
    client, calls = _client_with_stub(
        monkeypatch,
        [_fake_response(429, {"error": {"code": "rate_limited", "message": "slow"}})],
    )
    with pytest.raises(RateLimitedError):
        client.balance()
    assert calls["n"] == 2  # initial + exactly one retry


def test_500_maps_to_server_error(monkeypatch):
    client, _ = _client_with_stub(
        monkeypatch, [_fake_response(500, {"error": {"code": "boom"}})]
    )
    with pytest.raises(ServerError):
        client.balance()


def test_non_json_error_body_still_raises(monkeypatch):
    client, _ = _client_with_stub(
        monkeypatch, [_fake_response(401, raw_body=b"<html>nope</html>")]
    )
    with pytest.raises(AuthenticationError):
        client.balance()


def test_connection_failure_raises_caption_pack_error(monkeypatch):
    def boom(self, method, url, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests.Session, "request", boom)
    client = Client(api_key="cp_live_test")
    with pytest.raises(CaptionPackError, match="Could not reach"):
        client.balance()
