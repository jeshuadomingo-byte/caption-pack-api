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
import time

import libsql_experimental as libsql

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from generator import PLATFORM_RULES, TONES, generate_pack
from passphrase import WORDLIST_NAME, entropy_bits, generate_passphrases
import jsonfix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "credits.db")
RATE_LIMIT_PER_MIN = 60  # paid tier
FREE_TIER_PER_MIN = 10  # free tier: harsher, so farmed keys are slow
# Real Stripe payment link goes here (Jeshua's layup); env override wins.
TOP_UP_URL = os.environ.get("STRIPE_TOP_UP_URL", "https://buy.stripe.com/cNicN46gF08nctC9FEbsc00")
# Buyer checkout page: mints a paid key and links Stripe with
# ?client_reference_id=<key_id> so the webhook credits the right key.
# Surfaced in 402 bodies — the raw STRIPE_TOP_UP_URL can't credit anyone,
# so it must never appear in error responses.
CHECKOUT_URL = os.environ.get(
    "CHECKOUT_URL", "https://caption-pack-api.onrender.com/v1/checkout"
)
# Pro tier payment link ($25 / 1,500 credits). Hardcoded default keeps the tier
# live without a Render dashboard change; env override wins if set. The
# webhook needs no change: it already credits session.metadata["credits"]
# (default 500), and the Pro link carries metadata credits=1500.
TOP_UP_URL_PRO = os.environ.get("STRIPE_TOP_UP_URL_PRO", "https://buy.stripe.com/bJe00i7kJdZd0KUg42bsc01")
STRIPE_WEBHOOK_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"
OPERATOR_TOKEN_ENV = "OPERATOR_TOKEN"

# --- Free tier (25-call trial) -------------------------------------------
FREE_KEY_PREFIX = "cp_free_"
FREE_TIER_CREDITS = 25
FREE_TIER_EXPIRY_DAYS = 7
FREE_MINTS_PER_IP_PER_DAY = 1  # one free key per IP per 24h
PLAYGROUND_PER_HOUR_PER_IP = 5  # keyless playground throttle

# --- Schema freeze ---------------------------------------------------------
# Request/response JSON shapes are frozen as of this build for 30 days.
# No breaking changes without Jeshua's explicit approval.
SCHEMA_FREEZE_VERSION = "2026-10-02"
SCHEMA_FREEZE_UNTIL = "2026-11-02"

_hits: dict[int, list[float]] = {}

app = FastAPI(
    title="Caption-Pack API",
    version="0.1.0",
    description="POST a topic, audience and tone; get platform-ready captions + hashtags as JSON. 1 credit per call.",
)

# CORS for the public, unauthenticated GET endpoints only (not a schema
# change — HTTP-level headers). Lets the landing page fetch live public
# stats cross-origin. Deliberately scoped: /v1/free-trial is excluded so
# third-party pages can't mint keys from visitors' browsers.
_PUBLIC_CORS_PATHS = {"/v1/public-stats", "/openapi.yaml", "/openapi.json"}


@app.middleware("http")
async def _public_cors(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in _PUBLIC_CORS_PATHS:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "86400"
    return response


TURSO_URL_ENV = "TURSO_DATABASE_URL"
TURSO_TOKEN_ENV = "TURSO_AUTH_TOKEN"


class _DictCursor:
    """Wraps a libsql cursor so rows behave like sqlite3.Row (name access).

    libsql returns plain tuples; the app reads row["credits"] etc., so we
    map columns via cursor.description. rowcount/lastrowid pass through.
    """

    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount
        self.lastrowid = cur.lastrowid

    def _cols(self):
        desc = self._cur.description
        return [d[0] for d in desc] if desc else []

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(zip(self._cols(), row)) if row is not None else None

    def fetchall(self):
        cols = self._cols()
        return [dict(zip(cols, r)) for r in self._cur.fetchall()]


class _Connection:
    """Thin wrapper: dict-like rows + commit/close, whatever the backend."""

    def __init__(self, con):
        self._con = con

    def execute(self, sql, params=()):
        return _DictCursor(self._con.execute(sql, params))

    def commit(self):
        return self._con.commit()

    def close(self):
        return self._con.close()


def _db() -> "_Connection":
    """Single DB helper for the whole app.

    Turso cloud when TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are set,
    otherwise the local embedded SQLite file (zero-config dev/tests).
    The rest of the code never cares which backend is live.
    """
    url = os.environ.get(TURSO_URL_ENV, "").strip()
    token = os.environ.get(TURSO_TOKEN_ENV, "").strip()
    if url and token:
        con = libsql.connect(url, auth_token=token)
    else:
        con = libsql.connect("file:" + DB_PATH)
    return _Connection(con)


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
    # --- Free-tier / metrics tables (added 2026-10-02) --------------------
    con.execute(
        """CREATE TABLE IF NOT EXISTS free_trial_mints(
               id INTEGER PRIMARY KEY,
               ip TEXT NOT NULL,
               peer_ip TEXT,
               xff TEXT,
               fingerprint TEXT,
               key_id INTEGER,
               ts REAL NOT NULL)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS checkout_mints(
               id INTEGER PRIMARY KEY,
               ip TEXT NOT NULL,
               key_id INTEGER,
               ts REAL NOT NULL)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS playground_log(
               id INTEGER PRIMARY KEY,
               ip TEXT NOT NULL,
               user_agent TEXT,
               ts REAL NOT NULL)"""
    )
    # Migrate existing databases: api_keys gains tier + expires_at,
    # usage_log gains ip + user_agent for the metrics endpoint.
    _ensure_column(con, "api_keys", "tier", "TEXT NOT NULL DEFAULT 'paid'")
    _ensure_column(con, "api_keys", "expires_at", "REAL")
    _ensure_column(con, "usage_log", "ip", "TEXT")
    _ensure_column(con, "usage_log", "user_agent", "TEXT")
    con.commit()
    con.close()


def _ensure_column(con, table: str, column: str, ddl: str) -> None:
    """ALTER TABLE ADD COLUMN if missing — lets existing DBs (local file or
    Turso) pick up new columns on next boot without a manual migration."""
    cols = [r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


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
    if row.get("expires_at") and row["expires_at"] < time.time():
        return None, _err("key_expired", "This API key has expired.", 401)
    return row, None


def _check_rate_limit(key_id: int, per_min: int = RATE_LIMIT_PER_MIN):
    now = time.time()
    window = [t for t in _hits.get(key_id, []) if now - t < 60]
    if len(window) >= per_min:
        return _err(
            "rate_limited",
            f"Slow down \u2014 {per_min} requests/minute per key.",
            429,
        )
    window.append(now)
    _hits[key_id] = window
    return None


def _tier_rate_limit(row) -> int:
    """Free-tier keys are throttled harder (10/min) than paid keys (60/min)."""
    return FREE_TIER_PER_MIN if row.get("tier") == "free" else RATE_LIMIT_PER_MIN


def _client_ip(request) -> str:
    """Best-effort client IP for throttles and abuse review.

    Behind Render's edge proxy the TCP peer is the load balancer, so we
    trust the RIGHTMOST X-Forwarded-For entry — the one our own edge
    appended, which a caller cannot spoof past. With no XFF header (local
    dev, tests) we fall back to the direct peer IP.
    """
    xff = ""
    try:
        xff = request.headers.get("x-forwarded-for", "") if request else ""
    except Exception:
        xff = ""
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if parts:
        return parts[-1]
    client = getattr(request, "client", None) if request else None
    return client.host if client and getattr(client, "host", None) else "unknown"


def _truncated_ua(request, limit: int = 200) -> str | None:
    try:
        ua = request.headers.get("user-agent", "") if request else ""
    except Exception:
        ua = ""
    ua = (ua or "").strip()[:limit]
    return ua or None


def _credit_check(row):
    """Pre-generation 402 when the wallet is empty (tier-aware message).

    The 402 body carries checkout_url (the /v1/checkout page), never the raw
    Stripe link \u2014 only /v1/checkout mints a key whose client_reference_id
    lets the webhook credit the payment.
    """
    if row["credits"] < 1:
        if row.get("tier") == "free":
            return _err(
                "free_trial_exhausted",
                "Free trial credits exhausted \u2014 $10 gets 500 calls on a paid key.",
                402,
                {"checkout_url": CHECKOUT_URL},
            )
        return _err(
            "insufficient_credits",
            "Out of credits \u2014 $10 gets 500 calls.",
            402,
            {"checkout_url": CHECKOUT_URL},
        )
    return None


def _trial_ending_extras(row, remaining):
    """Soft upgrade nudge for free-trial keys running low (<=3 credits).

    Returns (body_extra, header_extra): a `trial_ending: true` body flag plus
    an `X-Trial-Ending: true` header, so clients can prompt an upgrade
    before the user hits the 402 wall. Free tier only.
    """
    if row.get("tier") == "free" and remaining is not None and remaining <= 3:
        return {"trial_ending": True}, {"X-Trial-Ending": "true"}
    return {}, {}


def _spend_credit(row, endpoint: str, ip: str | None, user_agent: str | None):
    """Atomically decrement 1 credit and log the call (with IP + UA for the
    metrics endpoint). Returns (credits_remaining, error_response)."""
    con = _db()
    # Atomic decrement: only succeeds if a credit is still available, so two
    # concurrent requests can never both spend the last credit.
    cur = con.execute(
        "UPDATE api_keys SET credits = credits - 1 WHERE id = ? AND credits > 0",
        (row["id"],),
    )
    if cur.rowcount == 0:
        con.close()
        return None, _credit_check({"credits": 0, "tier": row.get("tier")})
    remaining = con.execute(
        "SELECT credits FROM api_keys WHERE id = ?", (row["id"],)
    ).fetchone()["credits"]
    con.execute(
        "INSERT INTO usage_log(key_id, endpoint, credits_used, ts, ip, user_agent)"
        " VALUES (?,?,?,?,?,?)",
        (row["id"], endpoint, 1, time.time(), ip, user_agent),
    )
    con.commit()
    con.close()
    return remaining, None


class PackRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    audience: str = Field(min_length=1, max_length=200)
    tone: str = "warm"
    platform: str = "instagram"
    count: int = Field(default=5, ge=1, le=10)
    include_hashtags: bool = True
    niche: str | None = None


@app.post("/v1/caption-pack")
def caption_pack(
    req: PackRequest,
    authorization: str | None = Header(default=None),
    request: Request = None,
):
    row, err = _authed_key(authorization)
    if err:
        return err
    err = _check_rate_limit(row["id"], _tier_rate_limit(row))
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
    err = _credit_check(row)
    if err:
        return err

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

    remaining, err = _spend_credit(
        row, "/v1/caption-pack", _client_ip(request), _truncated_ua(request)
    )
    if err:
        return err

    pack["credits_used"] = 1
    pack["credits_remaining"] = remaining
    body_extra, header_extra = _trial_ending_extras(row, remaining)
    pack.update(body_extra)
    headers = {"X-Credits-Remaining": str(remaining)}
    headers.update(header_extra)
    return JSONResponse(status_code=200, content=pack, headers=headers)


@app.get("/v1/balance")
def balance(authorization: str | None = Header(default=None)):
    row, err = _authed_key(authorization)
    if err:
        return err
    return {
        "credits": row["credits"],
        "key_id": row["id"],
        "tier": row.get("tier") or "paid",
        "expires_at": row.get("expires_at"),
    }


# ---------------------------------------------------------------------------
# Passphrase API: POST /v1/passphrase
# Memorable, policy-compliant passphrases from the EFF short wordlist #2,
# drawn with the `secrets` module (CSPRNG) only. 1 credit per call, shared
# wallet with caption-pack. Same auth / rate-limit / error-envelope patterns.
# ---------------------------------------------------------------------------


class PassphraseRequest(BaseModel):
    words: int = Field(default=4, ge=3, le=8)
    separator: str = Field(default="-", max_length=8)
    capitalize: bool = False
    digit: bool = False
    symbol: bool = False
    count: int = Field(default=5, ge=1, le=20)


@app.post("/v1/passphrase")
def gen_passphrase(
    req: PassphraseRequest,
    authorization: str | None = Header(default=None),
    request: Request = None,
):
    row, err = _authed_key(authorization)
    if err:
        return err
    err = _check_rate_limit(row["id"], _tier_rate_limit(row))
    if err:
        return err
    err = _credit_check(row)
    if err:
        return err

    try:
        phrases = generate_passphrases(
            words=req.words,
            separator=req.separator,
            capitalize=req.capitalize,
            digit=req.digit,
            symbol=req.symbol,
            count=req.count,
        )
    except ValueError as exc:
        return _err("invalid_params", str(exc), 400)

    remaining, err = _spend_credit(
        row, "/v1/passphrase", _client_ip(request), _truncated_ua(request)
    )
    if err:
        return err
    body_extra, header_extra = _trial_ending_extras(row, remaining)

    return JSONResponse(
        status_code=200,
        content={
            "passphrases": phrases,
            "entropy_bits": entropy_bits(
                req.words, digit=req.digit, symbol=req.symbol
            ),
            "wordlist": WORDLIST_NAME,
            "count": req.count,
            "credits_used": 1,
            "credits_remaining": remaining,
            **body_extra,
        },
        headers={"X-Credits-Remaining": str(remaining), **header_extra},
    )


# ---------------------------------------------------------------------------
# JSON-Fix API: POST /v1/json-fix
# Deterministic JSON validation + repair (string surgery + json.loads only;
# never eval/exec). 1 credit per call, shared wallet with the other endpoints.
# Same auth / rate-limit / error-envelope patterns.
# ---------------------------------------------------------------------------


class JsonFixRequest(BaseModel):
    # Field is named json_text with wire alias "json" (a literal `json`
    # field name shadows BaseModel internals and trips a pydantic warning).
    json_text: str = Field(alias="json", min_length=1, max_length=100000)
    mode: str = "fix"
    indent: int = Field(default=2, ge=0, le=8)


@app.post("/v1/json-fix")
def json_fix(
    req: JsonFixRequest,
    authorization: str | None = Header(default=None),
    request: Request = None,
):
    row, err = _authed_key(authorization)
    if err:
        return err
    err = _check_rate_limit(row["id"], _tier_rate_limit(row))
    if err:
        return err
    if req.mode not in ("fix", "validate"):
        return _err("invalid_params", "mode must be 'fix' or 'validate'.", 400)
    err = _credit_check(row)
    if err:
        return err

    valid, fixed, fixes_applied, errors = jsonfix.process(
        req.json_text, mode=req.mode, indent=req.indent
    )

    remaining, err = _spend_credit(
        row, "/v1/json-fix", _client_ip(request), _truncated_ua(request)
    )
    if err:
        return err
    body_extra, header_extra = _trial_ending_extras(row, remaining)

    return JSONResponse(
        status_code=200,
        content={
            "valid": valid,
            "fixed": fixed,
            "fixes_applied": fixes_applied,
            "errors": errors,
            "credits_used": 1,
            "credits_remaining": remaining,
            **body_extra,
        },
        headers={"X-Credits-Remaining": str(remaining), **header_extra},
    )


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
    # Log the mint IP: lets /v1/stats measure free→paid conversion
    # (an IP that minted a free trial key and later bought credits).
    con.execute(
        "INSERT INTO checkout_mints(ip, key_id, ts) VALUES (?,?,?)",
        (_client_ip(request), key_id, time.time()),
    )
    con.commit()
    con.close()

    def _pay_url(base: str) -> str:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}client_reference_id={key_id}"

    pay_url = _pay_url(TOP_UP_URL)
    pro_block = ""
    if TOP_UP_URL_PRO:
        pro_block = f"""
<p style="margin-top:28px">Need more? <b>Pro pack</b> \u2014 1,500 credits for $25
(1.7&cent; per call).</p>
<a class="btn" href="{_pay_url(TOP_UP_URL_PRO)}">Continue to payment \u2014 $25 for 1,500 credits</a>"""
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
credited automatically.</p>
<div class="key">{raw_key}</div>
<a class="btn" href="{pay_url}">Continue to payment — $10 for 500 credits</a>
{pro_block}
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
        if row.get("tier") == "free":
            # Free-trial keys can NEVER be topped up or converted to paid.
            # Record the attempt for the abuse review, credit nothing.
            con.execute(
                "UPDATE stripe_events SET key_id = ? WHERE event_id = ?",
                (key_id, event_id),
            )
            con.commit()
            con.close()
            return _err(
                "free_key_not_top_uppable",
                "Free-trial keys cannot be topped up. Mint a paid key at"
                " GET /v1/checkout and pay there.",
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


# ---------------------------------------------------------------------------
# Free trial: GET /v1/free-trial
# Mints a 25-call free-tier key. Exploitation-resistant by design:
#   - max 1 free key per IP per 24h (DB-tracked, survives restarts)
#   - the throttle keys on the edge-appended X-Forwarded-For IP (unspoofable
#     past our proxy) AND the raw TCP peer IP, so header spoofing alone
#     cannot farm keys
#   - free keys are visibly marked (cp_free_...) with a tier flag in the DB
#   - harsher rate limit (10/min vs 60/min paid), 7-day expiry (they rot)
#   - free keys can NEVER be topped up (webhook refuses) or converted;
#     buying credits mints a separate paid key via /v1/checkout
#   - optional X-Device-Fingerprint header is logged as a second abuse
#     signal (never blocks on its own)
# Economics: 25 calls ~= $0.50 of value; a fresh residential IP costs more
# than that, so farming is uneconomical.
# ---------------------------------------------------------------------------


@app.get("/v1/free-trial")
def free_trial(request: Request):
    ip = _client_ip(request)
    peer_ip = request.client.host if request.client else None
    try:
        xff = request.headers.get("x-forwarded-for", "") or ""
    except Exception:
        xff = ""
    try:
        fingerprint = (request.headers.get("x-device-fingerprint", "") or "")[:128]
    except Exception:
        fingerprint = ""
    now = time.time()
    cutoff = now - 86400

    con = _db()
    mints = con.execute(
        "SELECT COUNT(*) AS n FROM free_trial_mints WHERE ip = ? AND ts > ?",
        (ip, cutoff),
    ).fetchone()["n"]
    if peer_ip and peer_ip != ip:
        # Same TCP peer minting under different XFF values: still one device.
        peer_mints = con.execute(
            "SELECT COUNT(*) AS n FROM free_trial_mints"
            " WHERE peer_ip = ? AND ts > ?",
            (peer_ip, cutoff),
        ).fetchone()["n"]
        mints = max(mints, peer_mints)
    if mints >= FREE_MINTS_PER_IP_PER_DAY:
        con.close()
        return _err(
            "free_trial_limit",
            "One free trial key per IP per 24 hours.",
            429,
        )

    raw_key = FREE_KEY_PREFIX + secrets.token_hex(16)
    expires_at = now + FREE_TIER_EXPIRY_DAYS * 86400
    cur = con.execute(
        "INSERT INTO api_keys(key_hash, credits, created_at, revoked, tier,"
        " expires_at) VALUES (?,?,?,0,'free',?)",
        (hash_key(raw_key), FREE_TIER_CREDITS, now, expires_at),
    )
    key_id = cur.lastrowid
    con.execute(
        "INSERT INTO free_trial_mints(ip, peer_ip, xff, fingerprint, key_id, ts)"
        " VALUES (?,?,?,?,?,?)",
        (ip, peer_ip, xff[:500], fingerprint or None, key_id, now),
    )
    con.commit()
    con.close()

    return {
        "api_key": raw_key,  # shown once — store it now
        "key_id": key_id,
        "tier": "free",
        "credits": FREE_TIER_CREDITS,
        "calls_remaining": FREE_TIER_CREDITS,
        "expires_at": expires_at,
        "rate_limit_per_min": FREE_TIER_PER_MIN,
        "endpoints": ["/v1/caption-pack", "/v1/passphrase", "/v1/json-fix"],
        "message": (
            "25 free API calls across caption-pack, passphrase and json-fix"
            " (shared wallet, 1 credit per call). No card required. The key"
            " expires in 7 days, is throttled to 10 req/min, and can never be"
            " topped up \u2014 buying $10 of credits mints a separate paid key"
            " at GET /v1/checkout."
        ),
    }


# ---------------------------------------------------------------------------
# Keyless playground: POST /v1/playground
# Try-before-you-key: real caption JSON with no API key. Tight per-IP
# throttle (5/hour) bounds scraping; the keyed 25-call free tier at
# GET /v1/free-trial is the builder/integration surface.
# ---------------------------------------------------------------------------


@app.post("/v1/playground")
def playground(req: PackRequest, request: Request):
    ip = _client_ip(request)
    ua = _truncated_ua(request)
    now = time.time()

    con = _db()
    used = con.execute(
        "SELECT COUNT(*) AS n FROM playground_log WHERE ip = ? AND ts > ?",
        (ip, now - 3600),
    ).fetchone()["n"]
    if used >= PLAYGROUND_PER_HOUR_PER_IP:
        con.close()
        return _err(
            "rate_limited",
            "Playground limit is 5 calls/hour per IP \u2014 mint a free API key"
            " at GET /v1/free-trial for 25 calls.",
            429,
        )

    if req.tone not in TONES:
        con.close()
        return _err("invalid_params", f"tone must be one of {', '.join(TONES)}.", 400)
    if req.platform not in PLATFORM_RULES:
        con.close()
        return _err(
            "invalid_params",
            f"platform must be one of {', '.join(PLATFORM_RULES)}.",
            400,
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
        con.close()
        return _err("invalid_params", str(exc), 400)

    con.execute(
        "INSERT INTO playground_log(ip, user_agent, ts) VALUES (?,?,?)",
        (ip, ua, now),
    )
    con.commit()
    con.close()

    pack["playground"] = True
    return JSONResponse(status_code=200, content=pack)


@app.get("/v1/playground")
def playground_page():
    return HTMLResponse(
        """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caption-Pack playground</title>
<style>
body{background:#0b0f19;color:#e8ecf4;font-family:system-ui,sans-serif;display:flex;justify-content:center;padding:48px 16px;margin:0}
.card{max-width:560px;width:100%;background:#131a2b;border:1px solid #243049;border-radius:16px;padding:32px}
label{display:block;font-size:13px;margin:14px 0 4px;color:#9fb0cc}
input,select{width:100%;box-sizing:border-box;background:#0b0f19;color:#e8ecf4;border:1px solid #243049;border-radius:8px;padding:10px;font-size:14px}
button{background:#3b82f6;color:#fff;border:none;padding:12px 28px;border-radius:10px;font-weight:600;margin-top:18px;cursor:pointer}
pre{background:#0b0f19;border:1px solid #243049;border-radius:8px;padding:14px;overflow:auto;font-size:12px;margin-top:18px;white-space:pre-wrap}
.note{color:#9fb0cc;font-size:13px;margin-top:14px}
</style></head><body><div class="card">
<h1>Caption-Pack playground</h1>
<p class="note">Keyless try — 5 calls per hour per IP. For more, mint a free key (25 calls): <a href="/v1/free-trial">/v1/free-trial</a>.</p>
<form id="f">
<label>Topic</label><input name="topic" value="password managers" required>
<label>Audience</label><input name="audience" value="small business owners" required>
<label>Tone</label><select name="tone"><option>warm</option><option>bold</option><option>professional</option><option>playful</option></select>
<label>Platform</label><select name="platform"><option>linkedin</option><option>instagram</option><option>tiktok</option><option>x</option></select>
<label>Count</label><input name="count" type="number" value="5" min="1" max="10">
<button type="submit">Generate captions</button>
</form>
<pre id="out">Your captions will appear here.</pre>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const d = new FormData(e.target);
  const body = {
    topic: d.get('topic'), audience: d.get('audience'), tone: d.get('tone'),
    platform: d.get('platform'), count: parseInt(d.get('count'), 10)
  };
  const out = document.getElementById('out');
  out.textContent = 'Working…';
  try {
    const r = await fetch('/v1/playground', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const j = await r.json();
    out.textContent = JSON.stringify(j, null, 2);
  } catch (err) { out.textContent = 'Error: ' + err.message; }
});
</script></div></body></html>"""
    )




# ---------------------------------------------------------------------------
# Operator stats: GET /v1/stats
# Lightweight usage metrics (keys created by tier, calls per key/endpoint/day,
# unique IPs per day, caller User-Agents, free->paid conversion). Protected by
# a shared operator token: Authorization: Bearer <OPERATOR_TOKEN>.
# 503 when OPERATOR_TOKEN is not configured (same pattern as the webhook).
# ---------------------------------------------------------------------------


@app.get("/v1/stats")
def stats(authorization: str | None = Header(default=None)):
    token = os.environ.get(OPERATOR_TOKEN_ENV, "")
    if not token:
        return _err(
            "stats_not_configured",
            "Operator stats are not configured on this server.",
            503,
        )
    presented = (
        authorization[len("Bearer "):].strip()
        if authorization and authorization.startswith("Bearer ")
        else ""
    )
    if not presented or not hmac.compare_digest(presented, token):
        return _err("invalid_operator_token", "Operator token is missing or invalid.", 401)

    con = _db()
    keys = con.execute(
        "SELECT tier, COUNT(*) AS n FROM api_keys GROUP BY tier"
    ).fetchall()
    keys_created = {r["tier"] or "paid": r["n"] for r in keys}

    calls_by_endpoint = {
        r["endpoint"]: r["n"]
        for r in con.execute(
            "SELECT endpoint, COUNT(*) AS n FROM usage_log GROUP BY endpoint"
        ).fetchall()
    }
    calls_total = sum(calls_by_endpoint.values())

    calls_by_key = {
        r["key_id"]: r["n"]
        for r in con.execute(
            "SELECT key_id, COUNT(*) AS n FROM usage_log GROUP BY key_id"
        ).fetchall()
    }

    by_day = con.execute(
        "SELECT date(ts, 'unixepoch') AS day, COUNT(*) AS calls,"
        " COUNT(DISTINCT ip) AS unique_ips FROM usage_log"
        " GROUP BY day ORDER BY day DESC LIMIT 14"
    ).fetchall()
    calls_by_day = [
        {"day": r["day"], "calls": r["calls"], "unique_ips": r["unique_ips"]}
        for r in by_day
    ]

    top_ua = con.execute(
        "SELECT user_agent, COUNT(*) AS n FROM usage_log"
        " WHERE user_agent IS NOT NULL GROUP BY user_agent"
        " ORDER BY n DESC LIMIT 10"
    ).fetchall()
    top_user_agents = [
        {"user_agent": r["user_agent"], "calls": r["n"]} for r in top_ua
    ]

    pg = con.execute(
        "SELECT COUNT(*) AS calls, COUNT(DISTINCT ip) AS ips FROM playground_log"
    ).fetchone()
    pg_24h = con.execute(
        "SELECT COUNT(*) AS calls, COUNT(DISTINCT ip) AS ips FROM playground_log"
        " WHERE ts > ?",
        (time.time() - 86400,),
    ).fetchone()

    free_mints_24h = con.execute(
        "SELECT COUNT(*) AS n FROM free_trial_mints WHERE ts > ?",
        (time.time() - 86400,),
    ).fetchone()["n"]
    # Free->paid conversion: IPs that minted a free trial key AND later had a
    # checkout key actually credited by Stripe.
    converted = con.execute(
        "SELECT COUNT(DISTINCT f.ip) AS n FROM free_trial_mints f"
        " JOIN checkout_mints c ON c.ip = f.ip"
        " JOIN stripe_events s ON s.key_id = c.key_id AND s.credits_added > 0"
    ).fetchone()["n"]
    # Checkout funnel (lever #5): /v1/checkout mints vs completed payments.
    ck = con.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT ip) AS ips FROM checkout_mints"
    ).fetchone()
    ck_24h = con.execute(
        "SELECT COUNT(*) AS n FROM checkout_mints WHERE ts > ?",
        (time.time() - 86400,),
    ).fetchone()["n"]
    paid = con.execute(
        "SELECT COUNT(DISTINCT key_id) AS n FROM stripe_events"
        " WHERE credits_added > 0"
    ).fetchone()["n"]
    paid_24h = con.execute(
        "SELECT COUNT(DISTINCT key_id) AS n FROM stripe_events"
        " WHERE credits_added > 0 AND ts > ?",
        (time.time() - 86400,),
    ).fetchone()["n"]

    con.close()
    return {
        "schema_freeze_version": SCHEMA_FREEZE_VERSION,
        "keys_created": keys_created,
        "calls_total": calls_total,
        "calls_by_endpoint": calls_by_endpoint,
        "calls_by_key": calls_by_key,
        "calls_by_day": calls_by_day,
        "top_user_agents": top_user_agents,
        "playground": {
            "calls_total": pg["calls"],
            "unique_ips_total": pg["ips"],
            "calls_24h": pg_24h["calls"],
            "unique_ips_24h": pg_24h["ips"],
        },
        "free_tier": {"mints_24h": free_mints_24h},
        "free_to_paid_conversion": {"converted_ips": converted},
        "checkout_funnel": {
            "mints_total": ck["n"],
            "mints_unique_ips_total": ck["ips"],
            "mints_24h": ck_24h,
            "paid_keys_total": paid,
            "paid_keys_24h": paid_24h,
        },
    }


# ---------------------------------------------------------------------------
# Public aggregate stats: GET /v1/public-stats
# No auth. Aggregate counts ONLY — no IPs, keys, user agents, or any
# per-visitor detail ever leaves this endpoint. Lets anyone (including the
# owner) answer "is anyone using this?" at a glance. Light per-IP throttle
# (60/hour, same in-memory pattern as /v1/checkout).
# ---------------------------------------------------------------------------

_public_stats_hits: dict[str, list[float]] = {}
PUBLIC_STATS_PER_HOUR = 60


@app.get("/v1/public-stats")
def public_stats(request: Request):
    ip = _client_ip(request)
    now = time.time()
    window = [t for t in _public_stats_hits.get(ip, []) if now - t < 3600]
    if len(window) >= PUBLIC_STATS_PER_HOUR:
        return _err(
            "rate_limited",
            "Slow down — 60 requests/hour per IP on /v1/public-stats.",
            429,
        )
    window.append(now)
    _public_stats_hits[ip] = window

    con = _db()
    cutoff = now - 86400
    free_keys_24h = con.execute(
        "SELECT COUNT(*) AS n FROM free_trial_mints WHERE ts > ?", (cutoff,)
    ).fetchone()["n"]
    unique_visitors_24h = con.execute(
        "SELECT COUNT(*) AS n FROM ("
        " SELECT ip FROM free_trial_mints WHERE ts > ?"
        " UNION SELECT ip FROM playground_log WHERE ts > ?"
        " UNION SELECT ip FROM usage_log WHERE ts > ?)",
        (cutoff, cutoff, cutoff),
    ).fetchone()["n"]
    metered_calls_24h = con.execute(
        "SELECT COUNT(*) AS n FROM usage_log WHERE ts > ?", (cutoff,)
    ).fetchone()["n"]
    playground_calls_24h = con.execute(
        "SELECT COUNT(*) AS n FROM playground_log WHERE ts > ?", (cutoff,)
    ).fetchone()["n"]
    con.close()
    return {
        "schema_freeze_version": SCHEMA_FREEZE_VERSION,
        "window": "last_24h",
        "free_keys_minted_24h": free_keys_24h,
        "unique_visitors_24h": unique_visitors_24h,
        "metered_calls_24h": metered_calls_24h,
        "playground_calls_24h": playground_calls_24h,
    }


# ---------------------------------------------------------------------------
# Machine-readable API contract: GET /openapi.yaml
# The frozen schema (v2026-10-02, frozen until 2026-11-02) lives in
# openapi.yaml next to this file; served here so agents and tooling can
# fetch it straight from the API root.
# ---------------------------------------------------------------------------


@app.get("/openapi.yaml")
def openapi_spec():
    from fastapi.responses import FileResponse

    return FileResponse(
        os.path.join(BASE_DIR, "openapi.yaml"), media_type="application/yaml"
    )
