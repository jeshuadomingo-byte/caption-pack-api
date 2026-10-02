"""Create a new API key with prepaid credits.

Usage:
    python make_key.py [credits]      # default 500

The raw key is printed ONCE — store it somewhere safe.
Only its sha256 hash is kept in credits.db.
"""

import os
import secrets
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import DB_PATH, hash_key, init_db  # noqa: E402


def main() -> None:
    credits = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    init_db()
    raw_key = "cp_live_" + secrets.token_hex(16)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO api_keys(key_hash, credits, created_at, revoked) VALUES (?,?,?,0)",
        (hash_key(raw_key), credits, time.time()),
    )
    con.commit()
    con.close()
    print(raw_key)
    print(f"({credits} credits — hash stored, raw key shown only here)", file=sys.stderr)


if __name__ == "__main__":
    main()
