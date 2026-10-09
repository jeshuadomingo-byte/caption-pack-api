#!/usr/bin/env python3
"""Caption-Pack MCP server (stdio transport).

Exposes the Caption-Pack API as three MCP tools so AI assistants can
generate social captions without touching raw HTTP:

  - generate_captions(topic, audience, ...) -> POST /v1/caption-pack
  - check_balance()                      -> GET  /v1/balance
  - mint_free_trial()                    -> GET  /v1/free-trial

Configuration (environment):
  CAPTIONPACK_API_KEY   Bearer key for generate_captions / check_balance.
                        Mint one free (25 calls, no card) with the
                        mint_free_trial tool, or buy $10/500 at
                        https://caption-pack-api.onrender.com/v1/checkout
  CAPTIONPACK_BASE_URL  API root. Default:
                        https://caption-pack-api.onrender.com
  CAPTIONPACK_TIMEOUT   HTTP timeout in seconds (default 60; Render's free
                        tier can be slow on a cold start).

Install:  pip install "mcp>=2" httpx
Run:      CAPTIONPACK_API_KEY=cp_... python mcp_server.py   (stdio)

Claude Desktop (~/.claude_desktop_config.json):
  {"mcpServers": {"caption-pack": {
      "command": "python",
      "args": ["/path/to/mcp_server.py"],
      "env": {"CAPTIONPACK_API_KEY": "cp_..."}
  }}}

Security notes:
  - The API key is read from the environment and sent only as an
    Authorization header. It is never logged, never echoed in tool output
    (except the single mint_free_trial response — save it immediately),
    and never written to disk by this server.
  - mint_free_trial inherits the API's abuse guards: one key per IP per
    day, 7-day expiry, free keys can never convert to paid.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar

import httpx
from mcp.server.mcpserver import MCPServer

BASE_URL = os.environ.get(
    "CAPTIONPACK_BASE_URL", "https://caption-pack-api.onrender.com"
).rstrip("/")
API_KEY = os.environ.get("CAPTIONPACK_API_KEY", "").strip()
TIMEOUT = float(os.environ.get("CAPTIONPACK_TIMEOUT", "60"))

# Per-request API key override (HTTP mode): Smithery-style `?apiKey=...`
# query param. Falls back to CAPTIONPACK_API_KEY. Empty in stdio mode.
_request_api_key: ContextVar[str] = ContextVar("captionpack_api_key", default="")

server = MCPServer(
    name="caption-pack",
    instructions=(
        "Generate ready-to-post social captions (hooks, bodies, CTAs) plus "
        "hashtags as JSON. Deterministic template engine — no LLM in the loop. "
        "One API credit per generation. Set CAPTIONPACK_API_KEY, or mint a "
        "free 25-call trial key with mint_free_trial."
    ),
)


def _active_key() -> str:
    """Request-scoped key (HTTP `?apiKey=`) wins; env CAPTIONPACK_API_KEY next."""
    return _request_api_key.get() or API_KEY


def _headers() -> dict:
    # Key travels only in the Authorization header; never logged or echoed.
    key = _active_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _friendly_error(resp: httpx.Response) -> str:
    """Agent-friendly error mapping (no key material ever included)."""
    code = resp.status_code
    if code == 402:
        return (
            "Out of credits. Top up at "
            f"{BASE_URL}/v1/checkout ($10 for 500 calls, about 2c each)."
        )
    if code == 429:
        return "Rate limited. Back off and retry after the Retry-After window."
    if code == 401:
        return (
            "Invalid API key. Check the CAPTIONPACK_API_KEY environment variable."
        )
    try:
        err = resp.json().get("error", {})
        return f"API error {code}: {err.get('code', 'unknown')} — {err.get('message', '')}".strip()
    except Exception:
        return f"API error {code}: {resp.text[:200]}"


async def _post(path: str, payload: dict) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{BASE_URL}{path}", json=payload, headers=_headers()
        )
    if resp.status_code != 200:
        return _friendly_error(resp)
    return json.dumps(resp.json())


async def _get(path: str) -> tuple[int, dict | str]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{BASE_URL}{path}", headers=_headers())
    if resp.status_code != 200:
        return resp.status_code, _friendly_error(resp)
    return resp.status_code, resp.json()


@server.tool()
async def generate_captions(
    topic: str,
    audience: str,
    tone: str = "warm",
    platform: str = "linkedin",
    count: int = 5,
    include_hashtags: bool = True,
) -> str:
    """Generate social captions + hashtags as JSON.

    tone: warm | bold | professional | playful
    platform: linkedin | instagram | tiktok | x
    count: 1-10 captions. Costs 1 API credit per call.
    """
    if not _active_key():
        return (
            "No API key configured. Set the CAPTIONPACK_API_KEY environment "
            "variable (or pass ?apiKey= in HTTP mode), or call mint_free_trial "
            "for a free 25-call key (no card)."
        )
    return await _post(
        "/v1/caption-pack",
        {
            "topic": topic,
            "audience": audience,
            "tone": tone,
            "platform": platform,
            "count": count,
            "include_hashtags": include_hashtags,
        },
    )


@server.tool()
async def check_balance() -> str:
    """Show remaining API credits, key tier, and expiry."""
    if not _active_key():
        return (
            "No API key configured. Set the CAPTIONPACK_API_KEY environment "
            "variable (or pass ?apiKey= in HTTP mode) first."
        )
    status, body = await _get("/v1/balance")
    if status != 200:
        return body  # type: ignore[return-value]
    return json.dumps(body)


@server.tool()
async def mint_free_trial() -> str:
    """Mint a free 25-call trial API key (no card, no account).

    Abuse guards: one key per IP per day, 7-day expiry, free keys can never
    convert to paid. The key is shown ONCE — save it as CAPTIONPACK_API_KEY
    immediately.
    """
    status, body = await _get("/v1/free-trial")
    if status != 200:
        if status == 429:
            return (
                "Free-trial limit reached: one key per IP per day. "
                "Try again tomorrow, or buy $10/500 calls at "
                f"{BASE_URL}/v1/checkout."
            )
        return body  # type: ignore[return-value]
    key = body.get("api_key") if isinstance(body, dict) else None
    return (
        "Free trial key minted (25 calls, 7-day expiry). "
        "SAVE THIS KEY NOW — it is shown only once:\n"
        f"{key}\n"
        "Set it as the CAPTIONPACK_API_KEY environment variable, then use "
        "generate_captions."
    )


class _ApiKeyMiddleware:
    """Pull Smithery-style ?apiKey= / ?api_key= into the request context."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            qs = scope.get("query_string", b"").decode("latin1")
            key = ""
            for part in qs.split("&"):
                name, _, value = part.partition("=")
                if name in ("apiKey", "api_key") and value:
                    from urllib.parse import unquote

                    key = unquote(value)
                    break
            if key:
                _request_api_key.set(key)
        await self.app(scope, receive, send)


if __name__ == "__main__":
    _port = os.environ.get("PORT", "").strip()
    if _port:
        # Container mode (Smithery, Render): streamable HTTP, stateless.
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        _app = server.streamable_http_app(stateless_http=True)

        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def _health(_request):
            return JSONResponse({"ok": True, "service": "caption-pack-mcp"})

        _app.routes.append(Route("/health", _health))
        _app.add_middleware(_ApiKeyMiddleware)
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
            allow_headers=["*"],
            expose_headers=["mcp-session-id", "mcp-protocol-version"],
        )
        uvicorn.run(_app, host="0.0.0.0", port=int(_port))
    else:
        # Local mode (Claude Desktop, Cursor, CLI): stdio.
        server.run()
