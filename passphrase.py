"""Passphrase generation for the Passphrase API.

Uses the EFF short wordlist #2 (wordlist.txt, 1296 words) and the Python
`secrets` module ONLY — never `random`. The `secrets` module draws from the
OS CSPRNG, which is the correct source for credential material.
"""

from __future__ import annotations

import math
import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORDLIST_PATH = os.path.join(BASE_DIR, "wordlist.txt")
WORDLIST_NAME = "eff-short-v1"

DIGITS = "0123456789"
SYMBOLS = "!@#$%^&*"

MIN_WORDS, MAX_WORDS = 3, 8
MIN_COUNT, MAX_COUNT = 1, 20
MAX_SEPARATOR_LEN = 8


def _load_wordlist() -> list[str]:
    words: list[str] = []
    with open(WORDLIST_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
    if len(words) < 1000:
        raise RuntimeError(f"wordlist too small: {len(words)} words")
    return words


WORDLIST: list[str] = _load_wordlist()


def entropy_bits(words: int, digit: bool = False, symbol: bool = False) -> float:
    """Honest entropy math: log2(wordlist_size ** words), plus digit/symbol bits."""
    bits = words * math.log2(len(WORDLIST))
    if digit:
        bits += math.log2(len(DIGITS))
    if symbol:
        bits += math.log2(len(SYMBOLS))
    return round(bits, 1)


def generate_passphrases(
    words: int = 4,
    separator: str = "-",
    capitalize: bool = False,
    digit: bool = False,
    symbol: bool = False,
    count: int = 5,
) -> list[str]:
    if not (MIN_WORDS <= words <= MAX_WORDS):
        raise ValueError(f"words must be {MIN_WORDS}-{MAX_WORDS}")
    if not (MIN_COUNT <= count <= MAX_COUNT):
        raise ValueError(f"count must be {MIN_COUNT}-{MAX_COUNT}")
    if not isinstance(separator, str) or len(separator) > MAX_SEPARATOR_LEN:
        raise ValueError("separator must be a short string")
    out: list[str] = []
    for _ in range(count):
        picks = [secrets.choice(WORDLIST) for _ in range(words)]
        if capitalize:
            picks = [p[:1].upper() + p[1:] for p in picks]
        phrase = separator.join(picks)
        tail = ""
        if digit:
            tail += secrets.choice(DIGITS)
        if symbol:
            tail += secrets.choice(SYMBOLS)
        if tail:
            phrase += (separator + tail) if separator else tail
        out.append(phrase)
    return out
