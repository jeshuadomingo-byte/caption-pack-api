"""Integration tests against a local run of the real Caption-Pack API.

These tests mint keys in the LOCAL credits.db only (backed up and restored
by the session fixture) — they never touch production.
"""

import pytest

from captionpack import (
    AuthenticationError,
    Client,
    InsufficientCreditsError,
    InvalidParamsError,
    RateLimitedError,
)

from .conftest import mint_key


def _client(base_url, key):
    return Client(api_key=key, base_url=base_url, timeout=15)


def test_caption_pack_success(live_server, venv_python):
    key = mint_key(venv_python, 10)
    client = _client(live_server, key)
    pack = client.caption_pack(topic="morning routines", audience="busy parents")

    assert len(pack.captions) == 5  # default count
    first = pack.captions[0]
    assert first.hook and first.body and first.cta
    assert pack.hashtags, "expected hashtags by default"
    assert first.hashtags, "expected per-caption hashtags"
    assert pack.hashtags[0] in first.hashtags  # aggregated from captions
    assert pack.credits_used == 1
    assert pack.credits_remaining == 9
    client.close()


def test_caption_pack_options(live_server, venv_python):
    key = mint_key(venv_python, 10)
    client = _client(live_server, key)
    pack = client.caption_pack(
        topic="password managers",
        audience="small business owners",
        tone="bold",
        platform="linkedin",
        count=3,
        include_hashtags=False,
    )
    assert len(pack.captions) == 3
    assert pack.hashtags == []
    assert pack.credits_remaining == 9
    client.close()


def test_balance_reflects_spend(live_server, venv_python):
    key = mint_key(venv_python, 5)
    client = _client(live_server, key)
    assert client.balance().credits == 5
    client.caption_pack(topic="t", audience="a", count=1)
    client.caption_pack(topic="t", audience="a", count=1)
    assert client.balance().credits == 3
    client.close()


def test_bad_key_raises_authentication_error(live_server):
    client = _client(live_server, "cp_live_thiskeydoesnotexist")
    with pytest.raises(AuthenticationError):
        client.balance()
    with pytest.raises(AuthenticationError):
        client.caption_pack(topic="t", audience="a")
    client.close()


def test_zero_credits_raises_insufficient_credits(live_server, venv_python):
    key = mint_key(venv_python, 0)
    client = _client(live_server, key)
    with pytest.raises(InsufficientCreditsError) as exc_info:
        client.caption_pack(topic="t", audience="a")
    assert exc_info.value.top_up_url  # API always includes the top-up link
    # balance still works with 0 credits
    assert client.balance().credits == 0
    client.close()


def test_invalid_tone_raises_invalid_params(live_server, venv_python):
    key = mint_key(venv_python, 5)
    client = _client(live_server, key)
    with pytest.raises(InvalidParamsError):
        client.caption_pack(topic="t", audience="a", tone="nope")
    # the rejected call must not spend a credit
    assert client.balance().credits == 5
    client.close()


def test_rate_limit_raises_after_retry(live_server, venv_python):
    key = mint_key(venv_python, 100)
    client = _client(live_server, key)
    hit_limit = False
    for _ in range(80):  # limit is 60/min; the 61st trips it
        try:
            client.caption_pack(topic="t", audience="a", count=1)
        except RateLimitedError:
            hit_limit = True
            break
    assert hit_limit, "expected the 60 req/min limit to trip"
    client.close()
