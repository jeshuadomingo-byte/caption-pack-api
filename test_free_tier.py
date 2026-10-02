"""Tests for the free tier, keyless playground, and operator metrics.

Covers: free-trial mint (cp_free_ keys, 25 credits, 7-day expiry), the
1-key-per-IP-per-24h throttle, free-key expiry, the 25-call cap, harsher
free rate limits (10/min vs 60/min paid), the Stripe webhook refusing free
key ids, playground IP throttling (5/hour), and /v1/stats auth + contents
(including User-Agent recording).

Runs endpoint functions directly against a throwaway SQLite DB —
the real credits.db is never touched.

Run:  .venv/bin/python test_free_tier.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import time

os.environ["OPERATOR_TOKEN"] = "opsecret"
os.environ["STRIPE_WEBHOOK_SECRET"] = "testsecret"

import app  # noqa: E402
from starlette.requests import Request  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool) -> None:
    (PASSED if cond else FAILED).append(name)
    print(("PASS " if cond else "FAIL ") + name)


def fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app.DB_PATH = tmp.name
    app._hits.clear()
    app._checkout_hits.clear()
    app.init_db()
    return tmp.name


def make_req(ip: str = "9.9.9.9", ua: str | None = None,
             headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    hdrs = list(headers or [])
    if ua is not None:
        hdrs.append((b"user-agent", ua.encode()))
    return Request({
        "type": "http", "method": "GET",
        "headers": hdrs, "client": (ip, 54321),
    })


def auth_for(key: str) -> str:
    return f"Bearer {key}"


def status_of(resp) -> int:
    return getattr(resp, "status_code", 200)


def mint_free(ip: str = "9.9.9.9"):
    return app.free_trial(make_req(ip=ip))


def extract_key(resp) -> str | None:
    body = resp_json(resp)
    m = re.search(r"cp_free_[0-9a-f]{32}", json.dumps(body))
    return m.group(0) if m else None


def resp_json(resp):
    """Endpoint functions return dicts on direct calls, JSONResponse over HTTP."""
    if isinstance(resp, dict):
        return resp
    return json.loads(resp.body.decode())


def paid_key(credits: int = 500) -> str:
    import secrets as _secrets
    raw = "cp_live_" + _secrets.token_hex(16)
    con = app._db()
    con.execute(
        "INSERT INTO api_keys(key_hash, credits, created_at, revoked)"
        " VALUES (?,?,?,0)",
        (app.hash_key(raw), credits, time.time()),
    )
    con.commit()
    con.close()
    return raw


def sign(payload: bytes, secret: str = "testsecret") -> str:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + payload,
                      hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def webhook_request(payload: bytes, sig: str) -> Request:
    req = Request({"type": "http", "method": "POST",
                   "headers": [(b"stripe-signature", sig.encode())]})
    req._body = payload
    return req


def main() -> None:
    fresh_db()

    # --- 1. free-trial mints a marked 25-credit key -------------------------
    r = mint_free("10.0.0.1")
    check("free-trial mints 200", status_of(r) == 200)
    key = extract_key(r)
    check("free key has cp_free_ prefix", bool(key))
    body = resp_json(r)
    check("25 credits granted", body.get("credits") == 25)
    check("tier is free", body.get("tier") == "free")
    check("7-day expiry set",
          abs(body.get("expires_at", 0) - (time.time() + 7 * 86400)) < 60)
    check("harsher rate limit advertised", body.get("rate_limit_per_min") == 10)

    con = app._db()
    row = con.execute(
        "SELECT * FROM api_keys WHERE key_hash = ?", (app.hash_key(key),)
    ).fetchone()
    check("tier flag stored", row["tier"] == "free")
    check("expiry stored", row["expires_at"] is not None)
    mint = con.execute(
        "SELECT * FROM free_trial_mints WHERE key_id = ?", (row["id"],)
    ).fetchone()
    con.close()
    check("mint logged with IP + timestamp",
          mint is not None and mint["ip"] == "10.0.0.1" and mint["ts"] > 0)

    # --- 2. second mint from same IP within 24h -> 429 ----------------------
    r2 = mint_free("10.0.0.1")
    check("second mint same IP -> 429", status_of(r2) == 429)
    # ... but a different IP can still mint
    r3 = mint_free("10.0.0.2")
    check("different IP mints fine", status_of(r3) == 200)
    key2 = extract_key(r3)
    check("second key differs", key2 and key2 != key)

    # --- 3. free key spends from the shared 25-credit wallet -----------------
    pack_req = app.PackRequest(topic="sourdough", audience="home bakers")
    app._hits.clear()
    resp = app.caption_pack(pack_req, auth_for(key), make_req(ip="10.0.0.1"))
    check("free key caption-pack 200", status_of(resp) == 200)
    data = json.loads(resp.body.decode())
    check("real captions returned", len(data.get("captions", [])) == 5)
    check("24 remaining", data.get("credits_remaining") == 24)

    bal = resp_json(app.balance(auth_for(key)))
    check("balance shows free tier",
          bal.get("tier") == "free" and bal.get("credits") == 24)

    # passphrase + json-fix draw from the same wallet
    pp = app.gen_passphrase(app.PassphraseRequest(), auth_for(key),
                            make_req(ip="10.0.0.1"))
    check("free key passphrase 200", status_of(pp) == 200)
    jf = app.json_fix(app.JsonFixRequest(json='{"a":1}'), auth_for(key),
                      make_req(ip="10.0.0.1"))
    check("free key json-fix 200", status_of(jf) == 200)
    bal = resp_json(app.balance(auth_for(key)))
    check("shared wallet: 22 left after 3 calls", bal.get("credits") == 22)

    # --- 4. free keys can't exceed 25 calls ----------------------------------
    con = app._db()
    con.execute("UPDATE api_keys SET credits = 1 WHERE key_hash = ?",
                (app.hash_key(key),))
    con.commit()
    con.close()
    app._hits.clear()
    last = app.caption_pack(pack_req, auth_for(key), make_req(ip="10.0.0.1"))
    check("25th call ok", status_of(last) == 200)
    over = app.caption_pack(pack_req, auth_for(key), make_req(ip="10.0.0.1"))
    check("26th call -> 402", status_of(over) == 402)
    over_body = json.loads(over.body.decode())
    check("free-exhausted code", over_body["error"]["code"] == "free_trial_exhausted")
    check("top_up_url present", "top_up_url" in over_body["error"])

    # --- 5. free key expiry ---------------------------------------------------
    con = app._db()
    con.execute("UPDATE api_keys SET expires_at = ? WHERE key_hash = ?",
                (time.time() - 10, app.hash_key(key2),))
    con.commit()
    con.close()
    row, err = app._authed_key(auth_for(key2))
    check("expired key -> 401", err is not None and status_of(err) == 401)
    check("expired code", json.loads(err.body.decode())["error"]["code"] == "key_expired")

    # --- 6. harsher free rate limit: 10/min vs 60/min paid --------------------
    pkey = paid_key(500)
    app._hits.clear()
    # use a fresh free key for a clean rate-limit window
    r4 = mint_free("10.0.0.9")
    fkey3 = extract_key(r4)
    app._hits.clear()
    statuses = [status_of(app.caption_pack(pack_req, auth_for(fkey3), make_req(ip="10.0.0.9")))
                for _ in range(11)]
    check("free key 11th rapid call -> 429",
          statuses[-1] == 429 and all(s == 200 for s in statuses[:-1]))
    app._hits.clear()
    pstatuses = [status_of(app.caption_pack(pack_req, auth_for(pkey), make_req(ip="10.0.0.8")))
                 for _ in range(11)]
    check("paid key 11 rapid calls all 200", all(s == 200 for s in pstatuses))

    # --- 7. webhook REFUSES free key ids --------------------------------------
    payload = json.dumps({
        "id": "evt_free_1", "type": "checkout.session.completed",
        "data": {"object": {
            "client_reference_id": str(body["key_id"]),
            "metadata": {"credits": "500"}}},
    }).encode()
    import asyncio
    out = asyncio.run(app.stripe_webhook(webhook_request(payload, sign(payload))))
    check("webhook refuses free key (400)",
          status_of(out) == 400
          and json.loads(out.body.decode())["error"]["code"] == "free_key_not_top_uppable")
    bal = resp_json(app.balance(auth_for(key)))
    check("free key credits unchanged", bal.get("credits") == 0)

    # --- 8. paid flow unaffected ----------------------------------------------
    con = app._db()
    kid = con.execute("SELECT id FROM api_keys WHERE key_hash = ?",
                      (app.hash_key(pkey),)).fetchone()["id"]
    con.close()
    payload2 = json.dumps({
        "id": "evt_paid_1", "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(kid),
                            "metadata": {"credits": "500"}}},
    }).encode()
    out2 = asyncio.run(app.stripe_webhook(webhook_request(payload2, sign(payload2))))
    check("webhook still credits paid keys",
          isinstance(out2, dict) and out2.get("credits_added") == 500)

    # --- 9. playground: keyless, real JSON, 5/hour per IP ----------------------
    pg = app.playground(pack_req, make_req(ip="10.0.0.7"))
    check("playground 200 without key", status_of(pg) == 200)
    pgd = json.loads(pg.body.decode())
    check("playground returns real captions", len(pgd.get("captions", [])) == 5)
    check("playground flagged", pgd.get("playground") is True)
    check("no credit fields on playground",
          "credits_used" not in pgd and "credits_remaining" not in pgd)
    for _ in range(4):
        app.playground(pack_req, make_req(ip="10.0.0.7"))
    pg6 = app.playground(pack_req, make_req(ip="10.0.0.7"))
    check("6th playground call in hour -> 429", status_of(pg6) == 429)
    pg_other = app.playground(pack_req, make_req(ip="10.0.0.77"))
    check("different IP unaffected", status_of(pg_other) == 200)

    # --- 10. metrics -----------------------------------------------------------
    # no token configured -> 503
    os.environ.pop("OPERATOR_TOKEN", None)
    s = app.stats(None)
    check("stats 503 without OPERATOR_TOKEN", status_of(s) == 503)
    os.environ["OPERATOR_TOKEN"] = "opsecret"
    s = app.stats(None)
    check("stats 401 without token", status_of(s) == 401)
    s = app.stats("Bearer wrong")
    check("stats 401 with wrong token", status_of(s) == 401)

    # make a call with a distinctive UA, then check the stats reflect it
    app._hits.clear()
    app.caption_pack(pack_req, auth_for(pkey),
                     make_req(ip="10.0.0.8", ua="AgentSmith/2.0 AI-Agent"))
    st = app.stats("Bearer opsecret")
    check("stats 200 with operator token", status_of(st) == 200)
    std = resp_json(st)
    check("keys_created by tier",
          std["keys_created"].get("free", 0) >= 3
          and std["keys_created"].get("paid", 0) >= 1)
    check("calls_by_endpoint has caption-pack",
          std["calls_by_endpoint"].get("/v1/caption-pack", 0) >= 20)
    check("calls_by_key present", len(std.get("calls_by_key", {})) >= 2)
    check("calls_by_day present", len(std.get("calls_by_day", [])) >= 1)
    uas = [u["user_agent"] for u in std.get("top_user_agents", [])]
    check("User-Agent recorded", any("AgentSmith/2.0" in u for u in uas))
    check("playground counted", std["playground"]["calls_total"] >= 6)
    check("conversion field present",
          "converted_ips" in std.get("free_to_paid_conversion", {}))

    # UA truncation: 200 chars max
    long_ua = "X" * 500
    app.caption_pack(pack_req, auth_for(pkey), make_req(ip="10.0.0.8", ua=long_ua))
    con = app._db()
    uas_db = [r["user_agent"] for r in con.execute(
        "SELECT DISTINCT user_agent FROM usage_log WHERE user_agent IS NOT NULL"
    ).fetchall()]
    con.close()
    check("UA truncated to 200 chars", all(len(u) <= 200 for u in uas_db))

    # --- 11. openapi.yaml served -----------------------------------------------
    spec_resp = app.openapi_spec()
    check("openapi.yaml served", status_of(spec_resp) == 200)
    check("spec mentions free-trial",
          b"/v1/free-trial" in open(spec_resp.path, "rb").read())

    print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
    if FAILED:
        print("FAILED:", FAILED)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
