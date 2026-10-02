"""captionpack — Python client for the Caption-Pack API."""

from .client import (
    API_KEY_ENV_VAR,
    DEFAULT_BASE_URL,
    BalanceResult,
    Caption,
    CaptionPackResult,
    Client,
)
from .exceptions import (
    AuthenticationError,
    CaptionPackError,
    InsufficientCreditsError,
    InvalidParamsError,
    RateLimitedError,
    ServerError,
)

__all__ = [
    "API_KEY_ENV_VAR",
    "DEFAULT_BASE_URL",
    "BalanceResult",
    "Caption",
    "CaptionPackResult",
    "Client",
    "AuthenticationError",
    "CaptionPackError",
    "InsufficientCreditsError",
    "InvalidParamsError",
    "RateLimitedError",
    "ServerError",
]

__version__ = "0.1.0"
