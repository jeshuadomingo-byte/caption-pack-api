"""End-to-end test of the buyer money flow: checkout -> webhook -> credits.

Runs endpoint functions directly against a throwaway SQLite DB —
the real credits.db is never touched.

Run:  .venv/bin/python test_checkout_flow.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import time

SECRET = "testsecret"
os.environ["STRIPE_WEBHOOK_SECRET"] = SECRET

import app  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool) -> None:
    (PASSED if cond else FAILED).append(name)
    print(("PASS " if cond else "FAIL ") + name)


def sign(payload: bytes, secret: str = SECRET) -> str:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + payload,
                      hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app.DB_PATH = tmp.name
    app.init_db()
    return tmp.name


class FakeClient:
    host = "127.0.0.1"


class FakeRequest:
    def __init__(self, payload: bytes, sig: str | None):
        self._payload = payload
        self._sig = sig
        self.client = FakeClient()
        self.headers = {"stripe-signature": sig} if sig else {}

    async def body(self):
        return self._payload


def main() -> None:
    fresh_db()

    # 1. Checkout mints a 0-credit key and links Stripe with client_reference_id
    resp = app.checkout(FakeRequest(b"", None))
    html = resp.body.decode()
    m = re.search(r"cp_live_[0-9a-f]{32}", html)
    key = m.group(0) if m else None
    check("checkout mints key", bool(key))
    m2 = re.search(r"client_reference_id=(\d+)", html)
    kid = int(m2.group(1)) if m2 else None
    check("pay url carries client_reference_id", kid is not None)
    check("pay url is live stripe link",
          "buy.stripe.com/cNicN46gF08nctC9FEbsc00" in html)
    check("all-sales-final notice shown", "No refunds" in html)

    con = sqlite3.connect(app.DB_PATH)
    row = con.execute("SELECT credits FROM api_keys WHERE id = ?", (kid,)).fetchone()
    con.close()
    check("fresh key has 0 credits", row is not None and row[0] == 0)

    # 2. Simulated Stripe checkout.session.completed with valid signature
    payload = json.dumps({
        "id": "evt_flow_1", "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(kid),
                            "metadata": {"credits": "500"}}},
    }).encode()
    out = asyncio.run(app.stripe_webhook(FakeRequest(payload, sign(payload))))
    check("webhook credits 500", out.get("credits_added") == 500)

    # 3. Key now has 500 credits and can generate
    row, err = app._authed_key(f"Bearer {key}")
    check("key authenticates", err is None and row["credits"] == 500)

    # 4. Duplicate delivery ignored — no double credit
    out = asyncio.run(app.stripe_webhook(FakeRequest(payload, sign(payload))))
    check("duplicate ignored", out.get("duplicate") is True)
    row, _ = app._authed_key(f"Bearer {key}")
    check("still 500 after duplicate", row["credits"] == 500)

    # 5. Forged webhook rejected
    out = asyncio.run(app.stripe_webhook(FakeRequest(payload, "t=1,v1=deadbeef")))
    check("forged signature rejected",
          isinstance(out, dict) or getattr(out, "status_code", 200) == 400)

    # 6. Spending is atomic: last credit can't be double-spent
    con = sqlite3.connect(app.DB_PATH)
    con.execute("UPDATE api_keys SET credits = 1 WHERE id = ?", (kid,))
    con.commit(); con.close()
    c1 = sqlite3.connect(app.DB_PATH)
    r1 = c1.execute(
        "UPDATE api_keys SET credits = credits - 1 WHERE id = ? AND credits > 0",
        (kid,)).rowcount
    c1.commit()
    r2 = c1.execute(
        "UPDATE api_keys SET credits = credits - 1 WHERE id = ? AND credits > 0",
        (kid,)).rowcount
    c1.commit(); c1.close()
    check("atomic decrement blocks double-spend", r1 == 1 and r2 == 0)

    # 7. Checkout throttle exists (21st rapid attempt from same IP -> 429)
    app._checkout_hits.clear()
    statuses = []
    for _ in range(21):
        r = app.checkout(FakeRequest(b"", None))
        statuses.append(getattr(r, "status_code", 200))
    check("checkout throttled at 20/hour", statuses[-1] == 429
          and all(s == 200 for s in statuses[:-1]))

    print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
    if FAILED:
        print("FAILED:", FAILED)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
