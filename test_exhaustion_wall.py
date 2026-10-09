#!/usr/bin/env python3
"""Exhaustion-wall checks: 402 checkout_url body + trial_ending nudge + checkout funnel."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("OPERATOR_TOKEN", "test-op-token")

import app
from fastapi.testclient import TestClient

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f" [{detail[:120]}]" if detail and not cond else ""))


def fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app.DB_PATH = tmp.name
    app._hits.clear()
    app._checkout_hits.clear()
    app._public_stats_hits.clear()
    app.init_db()
    return tmp.name


def make_key(tier="free", credits=25):
    import secrets, hashlib
    raw = ("cp_free_" if tier == "free" else "cp_live_") + secrets.token_hex(16)
    con = app._db()
    con.execute(
        "INSERT INTO api_keys(key_hash, credits, created_at, revoked, tier, expires_at)"
        " VALUES (?,?,?,0,?,?)",
        (hashlib.sha256(raw.encode()).hexdigest(), credits, time.time(), tier,
         time.time() + 86400 if tier == "free" else None),
    )
    con.commit()
    con.close()
    return raw


fresh_db()
c = TestClient(app.app, raise_server_exceptions=False)
H = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731
CAP = {"topic": "t", "audience": "a", "tone": "warm", "platform": "linkedin", "count": 1}

# --- 402: exhausted free key ---
fk = make_key("free", 0)
r = c.post("/v1/caption-pack", json=CAP, headers=H(fk))
b = r.json()
check("free 402 status", r.status_code == 402, str(r.status_code))
check("free 402 code", b["error"]["code"] == "free_trial_exhausted")
check("free 402 has checkout_url", b["error"].get("checkout_url", "").endswith("/v1/checkout"), str(b["error"]))
check("free 402 drops top_up_url", "top_up_url" not in b["error"])

# --- 402: exhausted paid key ---
pk = make_key("paid", 0)
r = c.post("/v1/caption-pack", json=CAP, headers=H(pk))
b = r.json()
check("paid 402 status", r.status_code == 402)
check("paid 402 code", b["error"]["code"] == "insufficient_credits")
check("paid 402 has checkout_url", b["error"].get("checkout_url", "").endswith("/v1/checkout"))
check("paid 402 drops top_up_url", "top_up_url" not in b["error"])
check("paid 402 CTA", "$10" in b["error"]["message"])

# --- trial_ending: free key at exactly 3 credits ---
fk3 = make_key("free", 4)
r = c.post("/v1/caption-pack", json=CAP, headers=H(fk3))  # 4 -> 3
b = r.json()
check("3 left: trial_ending body", b.get("trial_ending") is True, str(b.get("trial_ending")))
check("3 left: X-Trial-Ending header", r.headers.get("x-trial-ending") == "true")
check("3 left: X-Credits-Remaining", r.headers.get("x-credits-remaining") == "3")

# --- no nudge above 3 ---
fk10 = make_key("free", 10)
r = c.post("/v1/caption-pack", json=CAP, headers=H(fk10))  # 10 -> 9
b = r.json()
check("9 left: no trial_ending body", "trial_ending" not in b)
check("9 left: no X-Trial-Ending", "x-trial-ending" not in r.headers)

# --- no nudge for paid keys even at low balance ---
pk3 = make_key("paid", 4)
r = c.post("/v1/caption-pack", json=CAP, headers=H(pk3))  # 4 -> 3
b = r.json()
check("paid 3 left: no trial_ending", "trial_ending" not in b)

# --- passphrase + json-fix carry the nudge too ---
fk2 = make_key("free", 2)
r = c.post("/v1/passphrase", json={"words": 4}, headers=H(fk2))  # 2 -> 1
b = r.json()
check("passphrase: trial_ending", b.get("trial_ending") is True and r.headers.get("x-trial-ending") == "true")
r = c.post("/v1/json-fix", json={"json": "{bad json", "mode": "fix"}, headers=H(fk2))  # 1 -> 0
b = r.json()
check("json-fix: trial_ending at 0", b.get("trial_ending") is True)

# --- checkout funnel in operator stats ---
r = c.get("/v1/stats", headers={"Authorization": "Bearer test-op-token"})
check("stats 200", r.status_code == 200, str(r.status_code))
f = r.json().get("checkout_funnel", {})
check("funnel keys", set(f) == {"mints_total", "mints_unique_ips_total", "mints_24h", "paid_keys_total", "paid_keys_24h"}, str(f))

n = len(results)
print(f"\n{n - sum(1 for x in results if not x)}/{n} passed")
sys.exit(0 if all(results) else 1)
