"""Typed exceptions for the Caption-Pack API client.

The API returns errors as JSON envelopes like::

    {"error": {"code": "invalid_key", "message": "API key is missing or invalid."}}

with the HTTP status carrying the error class. This module maps those
statuses to typed exceptions so callers can handle each case explicitly.
"""

from __future__ import annotations


class CaptionPackError(Exception):
    """Base error for all Caption-Pack client failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class AuthenticationError(CaptionPackError):
    """HTTP 401 — the API key is missing, invalid, or revoked."""


class InsufficientCreditsError(CaptionPackError):
    """HTTP 402 — the key is out of credits.

    The API points at a top-up URL when one is configured; it is exposed
    here when present so callers can direct the user to buy more.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        top_up_url: str | None = None,
    ) -> None:
        super().__init__(message, code=code, status_code=status_code)
        self.top_up_url = top_up_url


class RateLimitedError(CaptionPackError):
    """HTTP 429 — too many requests for this key. Slow down and retry."""


class InvalidParamsError(CaptionPackError):
    """HTTP 400 — a request parameter was missing or invalid."""


class ServerError(CaptionPackError):
    """HTTP 5xx — the API had an internal problem. Safe to retry later."""
