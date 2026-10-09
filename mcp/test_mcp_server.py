#!/usr/bin/env python3
"""End-to-end test: real MCP client (stdio) against mcp_server.py,
which talks to the API at CAPTIONPACK_BASE_URL.

Phase 1 (no key): list tools, mint_free_trial.
Phase 2 (key from phase 1): check_balance, generate_captions.
Phase 3: bad key -> 401 mapping; unit checks of _friendly_error.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
BASE_URL = os.environ.get("CAPTIONPACK_BASE_URL", "http://127.0.0.1:8123")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" [{detail}]" if detail and not cond else ""))


async def open_session(env_extra):
    """Returns (stack, session); caller must `await stack.aclose()` when done."""
    from contextlib import AsyncExitStack
    stack = AsyncExitStack()
    env = dict(os.environ)
    env.update(env_extra)
    env["CAPTIONPACK_BASE_URL"] = BASE_URL
    # Workaround: this VM's no_proxy contains bracketed IPv6 entries
    # ([::1], ...) that httpx 0.28.1 cannot parse ("Invalid port: ':1]'").
    # Strip the brackets for the child process; harmless elsewhere.
    for k in ("no_proxy", "NO_PROXY"):
        if k in env:
            env[k] = ",".join(
                p for p in env[k].split(",") if not (p.startswith("[") and p.endswith("]"))
            )
    params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env)
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return stack, session


async def main():
    # ---- Phase 1: no key ----
    stack, session = await open_session({"CAPTIONPACK_API_KEY": ""})
    try:
        tools = await session.list_tools()
        names = sorted(t.name for t in tools.tools)
        check("3 tools listed", names == ["check_balance", "generate_captions", "mint_free_trial"], str(names))

        r = await session.call_tool("generate_captions", {"topic": "x", "audience": "y"})
        text = r.content[0].text
        check("generate without key -> helpful message", "CAPTIONPACK_API_KEY" in text and "mint_free_trial" in text)

        r = await session.call_tool("mint_free_trial", {})
        text = r.content[0].text
        check("mint_free_trial ok", "SAVE THIS KEY NOW" in text, text[:120])
        key = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("cp_free_"):
                key = line
        check("minted key extracted", bool(key))
        if not key:
            print("MINT OUTPUT:\n" + text)
            return 1

    finally:
        await stack.aclose()

    # ---- Phase 2: with key ----
    stack, session = await open_session({"CAPTIONPACK_API_KEY": key})
    try:
        r = await session.call_tool("check_balance", {})
        bal = json.loads(r.content[0].text)
        check("check_balance", bal.get("credits") == 25 and bal.get("tier") == "free", str(bal)[:120])

        r = await session.call_tool(
            "generate_captions",
            {"topic": "password managers", "audience": "small business owners",
             "tone": "warm", "platform": "linkedin", "count": 2},
        )
        pack = json.loads(r.content[0].text)
        check("generate_captions", len(pack.get("captions", [])) == 2, str(pack)[:120])

        r = await session.call_tool("check_balance", {})
        bal2 = json.loads(r.content[0].text)
        check("credit deducted", bal2.get("credits") == 24, str(bal2.get("credits")))

    finally:
        await stack.aclose()

    # ---- Phase 3: bad key -> 401 mapping ----
    stack, session = await open_session({"CAPTIONPACK_API_KEY": "cp_bogus_key_123"})
    try:
        r = await session.call_tool("check_balance", {})
        check("bad key -> 401 message", "Invalid API key" in r.content[0].text, r.content[0].text[:100])

    finally:
        await stack.aclose()

    # ---- Phase 4: unit-test the error mapper ----
    import mcp_server as ms
    import httpx

    def fake_resp(code, json_body=None):
        req = httpx.Request("GET", "http://x")
        return httpx.Response(code, json=json_body or {}, request=req)

    check("402 mapping", "Out of credits" in ms._friendly_error(fake_resp(402)) and "checkout" in ms._friendly_error(fake_resp(402)))
    check("429 mapping", "Rate limited" in ms._friendly_error(fake_resp(429)))
    check("401 mapping", "Invalid API key" in ms._friendly_error(fake_resp(401)))
    check("400 passthrough", "invalid_params" in ms._friendly_error(fake_resp(400, {"error": {"code": "invalid_params", "message": "bad tone"}})))
    # key material never leaks through the mapper
    os.environ["CAPTIONPACK_API_KEY"] = "cp_super_secret_999"
    import importlib
    importlib.reload(ms)
    leaked = any("cp_super_secret_999" in ms._friendly_error(fake_resp(c)) for c in (401, 402, 429, 500))
    check("no key leakage in errors", not leaked)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
