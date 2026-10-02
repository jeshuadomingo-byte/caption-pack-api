"""Synchronous client for the Caption-Pack API.

Quickstart::

    from captionpack import Client

    client = Client(api_key="cp_live_...")
    pack = client.caption_pack(topic="morning routines", audience="busy parents")
    print(pack.captions[0].body, pack.hashtags)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import requests

from .exceptions import (
    AuthenticationError,
    CaptionPackError,
    InsufficientCreditsError,
    InvalidParamsError,
    RateLimitedError,
    ServerError,
)

DEFAULT_BASE_URL = "https://caption-pack-api.onrender.com"
API_KEY_ENV_VAR = "CAPTIONPACK_API_KEY"

# One retry on 429 only; everything else fails fast so callers see real errors.
_MAX_429_RETRIES = 1
_DEFAULT_BACKOFF_SECONDS = 1.0


@dataclass
class Caption:
    """A single generated caption: hook + body + call to action."""

    hook: str
    body: str
    cta: str
    hashtags: list[str] = field(default_factory=list)


@dataclass
class CaptionPackResult:
    """Result of :meth:`Client.caption_pack`."""

    captions: list[Caption] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    credits_used: int = 1
    credits_remaining: int | None = None


@dataclass
class BalanceResult:
    """Result of :meth:`Client.balance`."""

    credits: int
    key_id: int | None = None


def _error_from_response(resp: requests.Response) -> CaptionPackError:
    """Map an HTTP error response to the matching typed exception."""
    code: str | None = None
    message = f"Caption-Pack API request failed (HTTP {resp.status_code})."
    top_up_url: str | None = None
    try:
        payload = resp.json()
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        code = err.get("code")
        if err.get("message"):
            message = err["message"]
        top_up_url = err.get("top_up_url")
    except ValueError:
        pass  # non-JSON body: keep the generic message

    status = resp.status_code
    if status == 400:
        return InvalidParamsError(message, code=code, status_code=status)
    if status == 401:
        return AuthenticationError(message, code=code, status_code=status)
    if status == 402:
        return InsufficientCreditsError(
            message, code=code, status_code=status, top_up_url=top_up_url
        )
    if status == 429:
        return RateLimitedError(message, code=code, status_code=status)
    if 500 <= status < 600:
        return ServerError(message, code=code, status_code=status)
    return CaptionPackError(message, code=code, status_code=status)


class Client:
    """Client for the Caption-Pack API.

    Args:
        api_key: Your ``cp_live_...`` key. Falls back to the
            ``CAPTIONPACK_API_KEY`` environment variable. Required.
        base_url: API base URL. Defaults to the production service.
        timeout: Seconds to wait for a response before giving up.
        user_agent: Optional custom User-Agent suffix for your app/agent.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        user_agent: str | None = None,
    ) -> None:
        key = api_key or os.environ.get(API_KEY_ENV_VAR)
        if not key:
            raise ValueError(
                "An API key is required: pass api_key=... or set the "
                f"{API_KEY_ENV_VAR} environment variable."
            )
        self.api_key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        ua = "captionpack-python/0.1.0"
        if user_agent:
            ua = f"{ua} {user_agent}"
        self.session.headers.update(
            {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": ua,
            }
        )

    # -- internals ----------------------------------------------------

    def _request(
        self, method: str, path: str, *, json: dict | None = None
    ) -> tuple[dict, requests.Response]:
        """Send a request; retry once on 429, then raise typed errors."""
        url = f"{self.base_url}{path}"
        attempts = 0
        while True:
            try:
                resp = self.session.request(
                    method, url, json=json, timeout=self.timeout
                )
            except requests.RequestException as exc:
                raise CaptionPackError(f"Could not reach {url}: {exc}") from exc

            if resp.status_code == 429 and attempts < _MAX_429_RETRIES:
                attempts += 1
                wait = self._retry_wait_seconds(resp)
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise _error_from_response(resp)

            try:
                return resp.json(), resp
            except ValueError as exc:
                raise CaptionPackError(
                    f"API returned a non-JSON response (HTTP {resp.status_code}).",
                    status_code=resp.status_code,
                ) from exc

    @staticmethod
    def _retry_wait_seconds(resp: requests.Response) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return _DEFAULT_BACKOFF_SECONDS

    # -- public API ---------------------------------------------------

    def caption_pack(
        self,
        topic: str,
        audience: str,
        *,
        tone: str = "warm",
        platform: str = "instagram",
        count: int = 5,
        include_hashtags: bool = True,
        niche: str | None = None,
    ) -> CaptionPackResult:
        """Generate a pack of captions + hashtags.

        Costs 1 credit per call. ``X-Credits-Remaining`` from the API is
        surfaced on the result so callers always know their balance.
        """
        body: dict = {
            "topic": topic,
            "audience": audience,
            "tone": tone,
            "platform": platform,
            "count": count,
            "include_hashtags": include_hashtags,
        }
        if niche is not None:
            body["niche"] = niche

        payload, resp = self._request("POST", "/v1/caption-pack", json=body)

        captions = []
        seen_tags: list[str] = []
        for c in payload.get("captions", []):
            tags = list(c.get("hashtags", []))
            for tag in tags:
                if tag not in seen_tags:
                    seen_tags.append(tag)
            captions.append(
                Caption(
                    hook=c.get("hook", ""),
                    body=c.get("body", ""),
                    cta=c.get("cta", ""),
                    hashtags=tags,
                )
            )
        # The API may return hashtags per-caption rather than top-level;
        # aggregate a de-duplicated list so callers get one simple field.
        top_level_tags = payload.get("hashtags")
        hashtags = (
            list(top_level_tags) if isinstance(top_level_tags, list) else seen_tags
        )
        remaining = payload.get("credits_remaining")
        if remaining is None:
            header_val = resp.headers.get("X-Credits-Remaining")
            remaining = int(header_val) if header_val is not None else None

        return CaptionPackResult(
            captions=captions,
            hashtags=hashtags,
            credits_used=int(payload.get("credits_used", 1)),
            credits_remaining=remaining,
        )

    def balance(self) -> BalanceResult:
        """Return the remaining credit balance for this API key."""
        payload, _ = self._request("GET", "/v1/balance")
        return BalanceResult(
            credits=int(payload.get("credits", 0)),
            key_id=payload.get("key_id"),
        )

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def __enter__(self) -> "Client":  # pragma: no cover - convenience
        return self

    def __exit__(self, *exc: object) -> None:  # pragma: no cover - convenience
        self.close()
