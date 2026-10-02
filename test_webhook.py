"""Tests for the Stripe webhook handler (/v1/stripe-webhook).

Runs the endpoint function directly against a throwaway SQLite DB —
the real credits.db is never touched.

Run:  .venv/bin/python test_webhook.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile

SECRET = "whsec_test_abc123"
os.environ["STRIPE_WEBHOOK_SECRET"] = SECRET

import app  # noqa: E402

from starlette.requests import Request  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool) -> None:
    (PASSED if cond else FAILED).append(name)
    print(("PASS " if cond else "FAIL ") + name)


def sign(payload: bytes, secret: str = SECRET, ts: str = "1727740000") -> str:
    digest = hmac.new(secret.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def make_request(payload: bytes, sig: str | None) -> Request:
    headers = []
    if sig is not None:
        headers.append((b"stripe-signature", sig.encode()))
    req = Request({"type": "http", "method": "POST", "headers": headers})
    req._body = payload  # starlette's body() uses _body when preset
    return req


def call(payload: bytes, sig: str | None):
    return asyncio.run(app.stripe_webhook(make_request(payload, sig)))


def setup() -> int:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app.DB_PATH = tmp.name
    app.init_db()
    con = sqlite3.connect(tmp.name)
    con.execute(
        "INSERT INTO api_keys(key_hash, credits, created_at, revoked) VALUES (?,?,?,0)",
        (app.hash_key("cp_live_testkey"), 0, 1727740000.0),
    )
    con.commit()
    key_id = con.execute("SELECT id FROM api_keys").fetchone()[0]
    con.close()
    return key_id


def credits_of(key_id: int) -> int:
    con = sqlite3.connect(app.DB_PATH)
    v = con.execute("SELECT credits FROM api_keys WHERE id = ?", (key_id,)).fetchone()[0]
    con.close()
    return v


def checkout_event(event_id: str, key_id, credits: str = "500") -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_1",
                    "client_reference_id": key_id,
                    "metadata": {"credits": credits},
                }
            },
        }
    ).encode()


def main() -> None:
    key_id = setup()

    # 1. valid signature verifier unit checks
    body = b'{"hello":"world"}'
    check("verify: valid signature passes", app._verify_stripe_signature(body, sign(body), SECRET))
    check("verify: wrong secret fails", not app._verify_stripe_signature(body, sign(body, secret="nope"), SECRET))
    check("verify: tampered payload fails", not app._verify_stripe_signature(b'{"hello":"mars"}', sign(body), SECRET))
    check("verify: malformed header fails", not app._verify_stripe_signature(body, "garbage", SECRET))
    check("verify: missing header fails", not app._verify_stripe_signature(body, None, SECRET))

    # 2. happy path: checkout.session.completed credits the key
    payload = checkout_event("evt_1", str(key_id))
    resp = call(payload, sign(payload))
    check("webhook: returns received + 500 credits", isinstance(resp, dict) and resp.get("received") and resp.get("credits_added") == 500 and resp.get("key_id") == key_id)
    check("webhook: key balance is now 500", credits_of(key_id) == 500)

    # 3. replay of the same event id is ignored (idempotent)
    resp = call(payload, sign(payload))
    check("webhook: duplicate event flagged", isinstance(resp, dict) and resp.get("duplicate") is True)
    check("webhook: no double-credit on replay", credits_of(key_id) == 500)

    # 4. unknown event types are acknowledged without crediting
    other = json.dumps({"id": "evt_other", "type": "payment_intent.created", "data": {"object": {}}}).encode()
    resp = call(other, sign(other))
    check("webhook: unknown type acked, 0 credits", isinstance(resp, dict) and resp.get("received") and resp.get("credits_added") == 0)

    # 5. bad signature -> 400
    resp = call(payload, sign(payload, secret="wrong"))
    check("webhook: bad signature -> 400", hasattr(resp, "status_code") and resp.status_code == 400)

    # 6. missing client_reference_id -> 400, no credit
    bad = checkout_event("evt_badref", None)
    resp = call(bad, sign(bad))
    check("webhook: missing key ref -> 400", hasattr(resp, "status_code") and resp.status_code == 400)
    check("webhook: balance unchanged after bad ref", credits_of(key_id) == 500)

    # 7. revoked key -> 400, no credit
    con = sqlite3.connect(app.DB_PATH)
    con.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    con.commit()
    con.close()
    rev = checkout_event("evt_revoked", str(key_id))
    resp = call(rev, sign(rev))
    check("webhook: revoked key -> 400", hasattr(resp, "status_code") and resp.status_code == 400)
    check("webhook: balance unchanged after revoked", credits_of(key_id) == 500)

    # 8. unconfigured secret -> 503
    del os.environ["STRIPE_WEBHOOK_SECRET"]
    resp = call(other, sign(other))
    check("webhook: no secret configured -> 503", hasattr(resp, "status_code") and resp.status_code == 503)
    os.environ["STRIPE_WEBHOOK_SECRET"] = SECRET

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
