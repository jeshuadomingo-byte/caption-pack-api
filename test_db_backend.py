"""Backend-switch tests for the libsql/Turso database layer.

Proves: no env vars -> local embedded SQLite file (same file plain sqlite3
sees); env vars set -> libsql.connect called with the Turso URL + token;
only one var set -> falls back to local.

Run:  .venv/bin/python test_db_backend.py
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import app  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool) -> None:
    (PASSED if cond else FAILED).append(name)
    print(("PASS " if cond else "FAIL ") + name)


def use_tmp_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app.DB_PATH = tmp.name
    os.environ.pop("TURSO_DATABASE_URL", None)
    os.environ.pop("TURSO_AUTH_TOKEN", None)
    app.init_db()
    return tmp.name


real_connect = app.libsql.connect

# 1. No env vars -> local embedded file, name-accessible rows.
use_tmp_db()
con = app._db()
con.execute(
    "INSERT INTO api_keys(key_hash, credits, created_at, revoked)"
    " VALUES (?,?,?,0)",
    ("hash1", 7, 1.0),
)
con.commit()
row = con.execute(
    "SELECT * FROM api_keys WHERE key_hash = ?", ("hash1",)
).fetchone()
check("local mode: row name access", row["credits"] == 7 and row["id"] >= 1)
con.close()
# Same file visible to plain sqlite3: proves embedded file mode, not a
# different store.
scon = sqlite3.connect(app.DB_PATH)
srow = scon.execute(
    "SELECT credits FROM api_keys WHERE key_hash = 'hash1'"
).fetchone()
check("local mode: same file plain sqlite3 sees", srow[0] == 7)
scon.close()

# 2. Both env vars set -> libsql.connect called with the Turso URL + token.
calls: dict = {}


def fake_connect(url, auth_token=None):
    calls["url"] = url
    calls["auth_token"] = auth_token
    return real_connect("file:" + app.DB_PATH)


app.libsql.connect = fake_connect
os.environ["TURSO_DATABASE_URL"] = "libsql://example.turso.io"
os.environ["TURSO_AUTH_TOKEN"] = "tok123"
try:
    c2 = app._db()
    c2.close()
    check("remote mode: url passed through",
          calls.get("url") == "libsql://example.turso.io")
    check("remote mode: token passed through",
          calls.get("auth_token") == "tok123")
finally:
    app.libsql.connect = real_connect
    os.environ.pop("TURSO_DATABASE_URL", None)
    os.environ.pop("TURSO_AUTH_TOKEN", None)

# 3. Only one env var set -> falls back to local file (both required).
os.environ["TURSO_DATABASE_URL"] = "libsql://example.turso.io"
calls2: dict = {}


def fake_connect2(url, auth_token=None):
    calls2["url"] = url
    calls2["auth_token"] = auth_token
    return real_connect("file:" + app.DB_PATH)


app.libsql.connect = fake_connect2
try:
    c3 = app._db()
    c3.close()
    check("partial env: falls back to local file",
          str(calls2.get("url", "")).startswith("file:"))
finally:
    app.libsql.connect = real_connect
    os.environ.pop("TURSO_DATABASE_URL", None)

print()
print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
if FAILED:
    raise SystemExit("FAILURES: " + ", ".join(FAILED))
