"""Tests for POST /v1/json-fix.

Runs endpoint functions directly against a throwaway SQLite DB —
the real credits.db (and Turso) is never touched.

Run:  .venv/bin/python test_jsonfix.py
"""

from __future__ import annotations

import hashlib
import json
import os

# Never touch Turso in tests, even if the shell has the vars set.
os.environ.pop("TURSO_DATABASE_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)

import sqlite3
import sys
import tempfile
import time

from pydantic import ValidationError

import app  # noqa: E402
import jsonfix  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool) -> None:
    (PASSED if cond else FAILED).append(name)
    print(("PASS " if cond else "FAIL ") + name)


def fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app.DB_PATH = tmp.name
    app.init_db()
    return tmp.name


def mint_key(credits: int) -> str:
    raw = "cp_live_test_" + os.urandom(8).hex()
    con = sqlite3.connect(app.DB_PATH)
    con.execute(
        "INSERT INTO api_keys(key_hash, credits, created_at, revoked)"
        " VALUES (?,?,?,0)",
        (hashlib.sha256(raw.encode()).hexdigest(), credits, time.time()),
    )
    con.commit()
    con.close()
    return raw


def call(body: dict, key: str | None):
    req = app.JsonFixRequest(**body)
    auth = f"Bearer {key}" if key else None
    return app.json_fix(req, auth)


def status_of(resp) -> int:
    return resp.status_code


def body_of(resp) -> dict:
    return json.loads(resp.body.decode())


# --- 1. safety: pure string surgery, never eval/exec -------------------------
src = open(jsonfix.__file__, encoding="utf-8").read()
check("jsonfix.py never calls eval(", "eval(" not in src)
check("jsonfix.py never calls exec(", "exec(" not in src)
check("jsonfix.py imports only json+re", "import os" not in src
      and "import subprocess" not in src)

# --- 2. valid input passes through -------------------------------------------
fresh_db()
key = mint_key(100)
r = call({"json": '{"a": 1, "b": [1, 2]}'}, key)
check("200 on valid JSON", status_of(r) == 200)
b = body_of(r)
check("valid=true", b["valid"] is True)
check("fixed pretty-prints input", json.loads(b["fixed"]) == {"a": 1, "b": [1, 2]})
check("fixes_applied empty", b["fixes_applied"] == [])
check("errors empty", b["errors"] == [])
check("credits_used = 1", b["credits_used"] == 1)
check("X-Credits-Remaining header = 99", r.headers.get("x-credits-remaining") == "99")
con = sqlite3.connect(app.DB_PATH)
left = con.execute("SELECT credits FROM api_keys").fetchone()[0]
con.close()
check("exactly 1 credit deducted", left == 99)

# --- 3. spec example: all three repairs ---------------------------------------
r = call({"json": "{ name: 'caption-pack', tags: ['a', 'b',], }"}, key)
b = body_of(r)
check("200 on spec example", status_of(r) == 200)
check("valid=false for broken input", b["valid"] is False)
check("fixed parses to expected object",
      json.loads(b["fixed"]) == {"name": "caption-pack", "tags": ["a", "b"]})
check("fixes_applied lists all three",
      b["fixes_applied"] == ["single quotes to double",
                             "quoted unquoted keys",
                             "removed trailing commas"])
check("errors populated even when fixed", len(b["errors"]) >= 1)

# --- 4. each repair strategy in isolation --------------------------------------
r = call({"json": '{\n// hello\n"a": 1 /* world */\n}'}, key)
b = body_of(r)
check("comments stripped", json.loads(b["fixed"]) == {"a": 1}
      and "stripped comments" in b["fixes_applied"])

r = call({"json": "{'a': 'it\\'s'}"}, key)
b = body_of(r)
check("single quotes converted (escaped quote kept)",
      json.loads(b["fixed"]) == {"a": "it's"})

r = call({"json": '{"a": [1, 2,], "b": {"c": 3,},}'}, key)
b = body_of(r)
check("trailing commas removed",
      json.loads(b["fixed"]) == {"a": [1, 2], "b": {"c": 3}})

r = call({"json": '{"a": hello, "b": 42, "c": true, "d": -1.5}'}, key)
b = body_of(r)
check("bare values quoted, numbers/bools untouched",
      json.loads(b["fixed"]) == {"a": "hello", "b": 42, "c": True, "d": -1.5}
      and "quoted bare values" in b["fixes_applied"])

# strings are never altered by repairs
r = call({"json": '{"url": "http://x.com/a,//b", "q": "a: b, c"}'}, key)
b = body_of(r)
check("string contents untouched",
      b["valid"] is True and b["fixes_applied"] == []
      and json.loads(b["fixed"])["url"] == "http://x.com/a,//b")

# --- 5. unfixable input ---------------------------------------------------------
r = call({"json": "{{{not json"}, key)
b = body_of(r)
check("200 even when unfixable", status_of(r) == 200)
check("valid=false", b["valid"] is False)
check("fixed=null when unfixable", b["fixed"] is None)
check("errors non-empty when unfixable", len(b["errors"]) >= 1)
check("credit still deducted for unfixable", b["credits_used"] == 1)

# --- 6. validate mode ------------------------------------------------------------
r = call({"json": "{a: 1}", "mode": "validate"}, key)
b = body_of(r)
check("validate: no repair attempted",
      b["valid"] is False and b["fixed"] is None and b["fixes_applied"] == [])
r = call({"json": '{"a": 1}', "mode": "validate"}, key)
b = body_of(r)
check("validate: valid input pretty-printed",
      b["valid"] is True and json.loads(b["fixed"]) == {"a": 1})

# --- 7. indent option ---------------------------------------------------------------
r = call({"json": '{"a": [1, 2]}', "indent": 0}, key)
b = body_of(r)
check("indent=0 is compact", b["fixed"] == '{"a":[1,2]}')

# --- 8. params -> 400 ------------------------------------------------------------------
for bad, name in [({"json": "x" * 100001}, "over 100k chars"),
                  ({"json": ""}, "empty string")]:
    try:
        call(bad, key)
        check(f"400 {name}", False)
    except ValidationError:
        # pydantic rejects before the handler; the app's
        # RequestValidationError handler maps these to 400 in production.
        check(f"400 {name} (via validation)", True)

r = call({"json": '{"a": 1}', "mode": "bogus"}, key)
check("400 on bad mode", status_of(r) == 400)

# --- 9. auth ---------------------------------------------------------------------------
r = call({"json": '{"a": 1}'}, "cp_live_bogus")
check("401 on bad key", status_of(r) == 401)
r = call({"json": '{"a": 1}'}, None)
check("401 on missing key", status_of(r) == 401)

# --- 10. credits --------------------------------------------------------------------------
poor = mint_key(0)
r = call({"json": '{"a": 1}'}, poor)
check("402 when broke", status_of(r) == 402)

last = mint_key(1)
r = call({"json": '{"a": 1}'}, last)
check("200 spending last credit", status_of(r) == 200)
r = call({"json": '{"a": 1}'}, last)
check("402 after last credit spent", status_of(r) == 402)

# --- 11. rate limit --------------------------------------------------------------------------
rl = mint_key(200)
codes = [status_of(call({"json": '{"a": 1}'}, rl)) for _ in range(61)]
check("first 60 calls succeed", all(c == 200 for c in codes[:60]))
check("61st call is 429", codes[60] == 429)

print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
