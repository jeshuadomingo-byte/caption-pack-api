# Caption-Pack MCP Server

Use the Caption-Pack API (social captions, no LLM in the loop) from any MCP
client: Claude Desktop, Cursor, or a hosted registry like Smithery.

## Tools

| Tool | What it does | Auth |
|---|---|---|
| `generate_captions(topic, audience, tone?, platform?, count?, include_hashtags?)` | POST /v1/caption-pack → captions + hashtags as JSON (1 credit/call) | key |
| `check_balance()` | GET /v1/balance → credits, tier, expiry | key |
| `mint_free_trial()` | GET /v1/free-trial → free 25-call key, no card (1/IP/day, 7-day expiry) | none |

## Quick start (Claude Desktop)

1. `pip install "mcp>=2" httpx`
2. Mint a key: open https://caption-pack-api.onrender.com/v1/free-trial in a
   browser (25 calls, no card), or buy $10/500 at
   https://caption-pack-api.onrender.com/v1/checkout
3. Add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "caption-pack": {
      "command": "python",
      "args": ["/path/to/mcp_server.py"],
      "env": { "CAPTIONPACK_API_KEY": "cp_..." }
    }
  }
}
```

No `PORT` set → the server runs over stdio. Done.

## Container / hosted (Smithery, Render)

Set the `PORT` environment variable (Smithery sets `8081` automatically) and
the server switches to streamable HTTP:

```
docker build -t caption-pack-mcp .
docker run -e PORT=8081 -p 8081:8081 caption-pack-mcp
```

Pass your key per request as `?apiKey=...` (or `?api_key=...`), or set
`CAPTIONPACK_API_KEY` on the deployment. The included `smithery.yaml`
declares the container build and the `apiKey` config schema — point
Smithery at this directory as the base.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `CAPTIONPACK_API_KEY` | — | Bearer key for the two authed tools |
| `CAPTIONPACK_BASE_URL` | `https://caption-pack-api.onrender.com` | API root (point at localhost for dev) |
| `CAPTIONPACK_TIMEOUT` | `60` | HTTP timeout, seconds (cold starts) |
| `PORT` | — | Set → streamable HTTP mode; unset → stdio mode |

## Security

- The key travels only in the `Authorization` header. It is never logged and
  never echoed, except the single `mint_free_trial` response — save it
  immediately.
- `mint_free_trial` inherits the API's abuse guards (1/IP/day, 7-day expiry,
  free keys never convert to paid).

## Testing

`test_mcp_server.py` runs a real MCP client over stdio (13 checks: tool
listing, mint/balance/generate round-trip, 401/402/429/400 mappings, no key
leakage) against `CAPTIONPACK_BASE_URL`. Point it at a local dev API:

```
CAPTIONPACK_BASE_URL=http://127.0.0.1:8123 python test_mcp_server.py
```

Note: on hosts whose `no_proxy` contains bracketed IPv6 entries (e.g.
`[::1]`), httpx 0.28.x fails to parse them; the test strips those entries
for the child process. Normal environments are unaffected.
