"""Caption-Pack API prototype.

FastAPI wrapper around generator.py with:
  - Bearer API-key auth (keys stored as sha256 hashes in SQLite)
  - Prepaid credit metering: 1 credit per POST /v1/caption-pack
  - Per-key rate limiting (60 req/min, in-memory)
  - Spec-compliant error envelopes: 400 / 401 / 402 / 429

Run:
    pip install -r requirements.txt
    python make_key.py 500        # prints a fresh API key (shown once)
    uvicorn app:app --port 8471
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from generator import PLATFORM_RULES, TONES, generate_pack

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "credits.db")
RATE_LIMIT_PER_MIN = 60
# Real Stripe payment link goes here (Jeshua's layup); env override wins.
TOP_UP_URL = os.environ.get("STRIPE_TOP_UP_URL", "https://buy.stripe.com/cNicN46gF08nctC9FEbsc00")
STRIPE_WEBHOOK_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"

_hits: dict[int, list[float]] = {}

app = FastAPI(
    title="Caption-Pack API",
    version="0.1.0",
    description="POST a topic, audience and tone; get platform-ready captions + hashtags as JSON. 1 credit per call.",
)


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    con = _db()
    con.execute(
        """CREATE TABLE IF NOT EXISTS api_keys(
               id INTEGER PRIMARY KEY,
               key_hash TEXT UNIQUE NOT NULL,
               credits INTEGER NOT NULL DEFAULT 0,
               created_at REAL NOT NULL,
               revoked INTEGER NOT NULL DEFAULT 0)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS usage_log(
               id INTEGER PRIMARY KEY,
               key_id INTEGER NOT NULL,
               endpoint TEXT NOT NULL,
               credits_used INTEGER NOT NULL,
               ts REAL NOT NULL)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS stripe_events(
               id INTEGER PRIMARY KEY,
               event_id TEXT UNIQUE NOT NULL,
               type TEXT NOT NULL,
               key_id INTEGER,
               credits_added INTEGER NOT NULL DEFAULT 0,
               ts REAL NOT NULL)"""
    )
    con.commit()
    con.close()


init_db()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _err(code: str, message: str, status: int, extra: dict | None = None):
    body: dict = {"error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    detail = f"{loc}: {first.get('msg', 'invalid value')}" if loc else "invalid request"
    return _err("invalid_params", detail, 400)


def _authed_key(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        return None, _err("invalid_key", "API key is missing or invalid.", 401)
    raw = authorization[len("Bearer "):].strip()
    con = _db()
    row = con.execute(
        "SELECT * FROM api_keys WHERE key_hash = ?", (hash_key(raw),)
    ).fetchone()
    con.close()
    if not row or row["revoked"]:
        return None, _err("invalid_key", "API key is missing or invalid.", 401)
    return row, None


def _check_rate_limit(key_id: int):
    now = time.time()
    window = [t for t in _hits.get(key_id, []) if now - t < 60]
    if len(window) >= RATE_LIMIT_PER_MIN:
        return _err("rate_limited", "Slow down \u2014 60 requests/minute per key.", 429)
    window.append(now)
    _hits[key_id] = window
    return None


class PackRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    audience: str = Field(min_length=1, max_length=200)
    tone: str = "warm"
    platform: str = "instagram"
    count: int = Field(default=5, ge=1, le=10)
    include_hashtags: bool = True
    niche: str | None = None


@app.post("/v1/caption-pack")
def caption_pack(req: PackRequest, authorization: str | None = Header(default=None)):
    row, err = _authed_key(authorization)
    if err:
        return err
    err = _check_rate_limit(row["id"])
    if err:
        return err

    if req.tone not in TONES:
        return _err("invalid_params", f"tone must be one of {', '.join(TONES)}.", 400)
    if req.platform not in PLATFORM_RULES:
        return _err(
            "invalid_params",
            f"platform must be one of {', '.join(PLATFORM_RULES)}.",
            400,
        )
    if row["credits"] < 1:
        return _err(
            "insufficient_credits",
            "Out of credits.",
            402,
            {"top_up_url": TOP_UP_URL},
        )

    try:
        pack = generate_pack(
            topic=req.topic,
            audience=req.audience,
            tone=req.tone,
            platform=req.platform,
            count=req.count,
            include_hashtags=req.include_hashtags,
            niche=req.niche,
        )
    except ValueError as exc:
        return _err("invalid_params", str(exc), 400)

    remaining = row["credits"] - 1
    con = _db()
    # Atomic decrement: only succeeds if a credit is still available, so two
    # concurrent requests can never both spend the last credit.
    cur = con.execute(
        "UPDATE api_keys SET credits = credits - 1 WHERE id = ? AND credits > 0",
        (row["id"],),
    )
    if cur.rowcount == 0:
        con.close()
        return _err(
            "out_of_credits",
            "Out of credits.",
            402,
            {"top_up_url": TOP_UP_URL},
        )
    remaining = con.execute(
        "SELECT credits FROM api_keys WHERE id = ?", (row["id"],)
    ).fetchone()["credits"]
    con.execute(
        "INSERT INTO usage_log(key_id, endpoint, credits_used, ts) VALUES (?,?,?,?)",
        (row["id"], "/v1/caption-pack", 1, time.time()),
    )
    con.commit()
    con.close()

    pack["credits_used"] = 1
    pack["credits_remaining"] = remaining
    return JSONResponse(
        status_code=200,
        content=pack,
        headers={"X-Credits-Remaining": str(remaining)},
    )


@app.get("/v1/balance")
def balance(authorization: str | None = Header(default=None)):
    row, err = _authed_key(authorization)
    if err:
        return err
    return {"credits": row["credits"], "key_id": row["id"]}


# ---------------------------------------------------------------------------
# Buyer checkout: GET /v1/checkout
# Mints a fresh 0-credit API key and shows the buyer their key (once) plus a
# button into Stripe. The key's id travels as ?client_reference_id= so the
# /v1/stripe-webhook handler can credit exactly this key on payment.
# Light IP throttle (20/hour) so nobody can spam-mint keys.
# ---------------------------------------------------------------------------

_checkout_hits: dict[str, list[float]] = {}
CHECKOUT_PER_HOUR = 20


@app.get("/v1/checkout")
def checkout(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = [t for t in _checkout_hits.get(ip, []) if now - t < 3600]
    if len(window) >= CHECKOUT_PER_HOUR:
        return _err(
            "rate_limited", "Too many checkout attempts — try again later.", 429
        )
    window.append(now)
    _checkout_hits[ip] = window

    raw_key = "cp_live_" + secrets.token_hex(16)
    con = _db()
    cur = con.execute(
        "INSERT INTO api_keys(key_hash, credits, created_at, revoked)"
        " VALUES (?,?,?,0)",
        (hash_key(raw_key), 0, time.time()),
    )
    key_id = cur.lastrowid
    con.commit()
    con.close()

    sep = "&" if "?" in TOP_UP_URL else "?"
    pay_url = f"{TOP_UP_URL}{sep}client_reference_id={key_id}"
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your Caption-Pack API key</title>
<style>
body{{background:#0b0f19;color:#e8ecf4;font-family:system-ui,sans-serif;
display:flex;justify-content:center;padding:48px 16px;margin:0}}
.card{{max-width:560px;background:#131a2b;border:1px solid #243049;border-radius:16px;
padding:32px;text-align:center}}
.key{{font-family:monospace;background:#0b0f19;border:1px dashed #3b82f6;
border-radius:8px;padding:14px;word-break:break-all;margin:20px 0;font-size:15px}}
.btn{{display:inline-block;background:#3b82f6;color:#fff;text-decoration:none;
padding:14px 32px;border-radius:10px;font-weight:600;margin-top:8px}}
.warn{{color:#f5b54a;font-size:14px;margin-top:16px}}
</style></head><body><div class="card">
<h1>Your API key is ready</h1>
<p>Save this now — it's shown <b>once</b>. After payment, this same key gets
500 credits automatically.</p>
<div class="key">{raw_key}</div>
<a class="btn" href="{pay_url}">Continue to payment — $10 for 500 credits</a>
<p class="warn">No refunds — all sales final. Credits never expire.</p>
</div></body></html>"""
    )


# ---------------------------------------------------------------------------
# Stripe webhook: prepaid credit top-ups (spec section 4)
#
# Jeshua's Stripe Payment Link is configured with:
#   ?client_reference_id=<api_keys.id>   (which key to credit)
#   metadata[credits]=500                (how many credits $10 buys)
# Stripe posts checkout.session.completed here; we verify the signature and
# credit the key. Idempotent: replayed event ids are ignored.
# Env: STRIPE_WEBHOOK_SECRET (signing secret from the Stripe dashboard).
# ---------------------------------------------------------------------------


def _verify_stripe_signature(
    payload: bytes, sig_header: str | None, secret: str
) -> bool:
    """Stripe's HMAC-SHA256 webhook signature scheme (no stripe lib needed)."""
    if not sig_header or not secret:
        return False
    timestamp: str | None = None
    signatures: list[str] = []
    for part in sig_header.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k == "t":
            timestamp = v
        elif k == "v1":
            signatures.append(v)
    if timestamp is None or not signatures:
        return False
    signed = timestamp.encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


@app.post("/v1/stripe-webhook")
async def stripe_webhook(request: Request):
    secret = os.environ.get(STRIPE_WEBHOOK_SECRET_ENV, "")
    if not secret:
        return _err(
            "webhook_not_configured",
            "Stripe webhook is not configured on this server.",
            503,
        )
    payload = await request.body()
    if not _verify_stripe_signature(
        payload, request.headers.get("stripe-signature"), secret
    ):
        return _err("invalid_signature", "Stripe signature verification failed.", 400)
    try:
        event = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _err("invalid_params", "Webhook body is not valid JSON.", 400)

    event_id = event.get("id")
    event_type = event.get("type", "")
    if not event_id:
        return _err("invalid_params", "Webhook event has no id.", 400)

    con = _db()
    # Claim this event FIRST (INSERT OR IGNORE on the UNIQUE event_id): only
    # the delivery that wins the insert may credit the key. A concurrent
    # duplicate delivery inserts 0 rows and credits nothing.
    cur = con.execute(
        "INSERT OR IGNORE INTO stripe_events(event_id, type, key_id,"
        " credits_added, ts) VALUES (?,?,?,?,?)",
        (event_id, event_type, None, 0, time.time()),
    )
    if cur.rowcount == 0:
        con.close()
        return {"received": True, "duplicate": True}

    credits_added = 0
    key_id: int | None = None
    if event_type == "checkout.session.completed":
        session = (event.get("data") or {}).get("object") or {}
        try:
            key_id = int(session.get("client_reference_id"))
        except (TypeError, ValueError):
            key_id = None
        row = (
            con.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
            if key_id is not None
            else None
        )
        if not row or row["revoked"]:
            con.close()
            return _err(
                "invalid_key_reference",
                "client_reference_id did not match an active API key.",
                400,
            )
        try:
            credits_added = int((session.get("metadata") or {}).get("credits", 500))
        except (TypeError, ValueError):
            credits_added = 500
        con.execute(
            "UPDATE api_keys SET credits = credits + ? WHERE id = ?",
            (credits_added, key_id),
        )
        con.execute(
            "UPDATE stripe_events SET key_id = ?, credits_added = ?"
            " WHERE event_id = ?",
            (key_id, credits_added, event_id),
        )

    con.commit()
    con.close()
    return {"received": True, "credits_added": credits_added, "key_id": key_id}
