"""Deterministic JSON validation + repair for POST /v1/json-fix.

Pure string surgery + json.loads. No eval, no exec, no network, no LLM.
Every repair pass is string-aware: the contents of quoted strings are never
altered. When a repair cannot be made safely, we return fixed=None instead
of a confident wrong answer.
"""

from __future__ import annotations

import json
import re

MAX_INPUT_CHARS = 100_000

# Fix labels reported in `fixes_applied`. Stable strings — part of the API.
FIX_COMMENTS = "stripped comments"
FIX_QUOTES = "single quotes to double"
FIX_KEYS = "quoted unquoted keys"
FIX_TRAILING = "removed trailing commas"
FIX_VALUES = "quoted bare values"

_JSON_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_NON_FINITE = {
    "nan", "+nan", "-nan",
    "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity",
}
_BARE_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]*")
_BARE_KEY = re.compile(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _segments(s: str) -> list[tuple[bool, str]]:
    """Split into (is_code, text) pieces.

    Quoted regions (single or double, backslash escapes honored) come back
    with is_code=False and are always passed through verbatim. An
    unterminated quote runs to end of string.
    """
    segs: list[tuple[bool, str]] = []
    buf: list[str] = []
    i, n = 0, len(s)

    def flush_code() -> None:
        if buf:
            segs.append((True, "".join(buf)))
            buf.clear()

    while i < n:
        c = s[i]
        if c == '"' or c == "'":
            flush_code()
            q = c
            j = i + 1
            chars = [c]
            while j < n:
                ch = s[j]
                chars.append(ch)
                if ch == "\\" and j + 1 < n:
                    chars.append(s[j + 1])
                    j += 2
                    continue
                if ch == q:
                    j += 1
                    break
                j += 1
            segs.append((False, "".join(chars)))
            i = j
        else:
            buf.append(c)
            i += 1
    flush_code()
    return segs


def _strip_comments(s: str) -> tuple[str, bool]:
    """Remove // line comments and /* */ block comments outside strings."""
    out: list[str] = []
    changed = False
    for is_code, text in _segments(s):
        if not is_code:
            out.append(text)
            continue
        new = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        new = re.sub(r"//[^\n]*", "", new)
        if new != text:
            changed = True
        out.append(new)
    return "".join(out), changed


def _single_to_double(seg: str) -> str:
    """Convert one single-quoted segment to double-quoted.

    Handles \\' escapes, escapes embedded double quotes, and encodes
    literal control characters. An unterminated segment stays unterminated
    (still broken — reported honestly downstream).
    """
    inner = seg[1:]
    terminated = inner.endswith("'")
    if terminated:
        inner = inner[:-1]
    out: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            if nxt == "'":
                out.append("'")
            else:
                out.append(ch)
                out.append(nxt)
            i += 2
        elif ch == '"':
            out.append('\\"')
            i += 1
        elif ch == "\n":
            out.append("\\n")
            i += 1
        elif ch == "\r":
            out.append("\\r")
            i += 1
        elif ch == "\t":
            out.append("\\t")
            i += 1
        else:
            out.append(ch)
            i += 1
    return '"' + "".join(out) + ('"' if terminated else "")


def _convert_single_quotes(s: str) -> tuple[str, bool]:
    out: list[str] = []
    changed = False
    for is_code, text in _segments(s):
        if is_code or not text.startswith("'"):
            out.append(text)
            continue
        out.append(_single_to_double(text))
        changed = True
    return "".join(out), changed


def _quote_bare_keys(s: str) -> tuple[str, bool]:
    """Quote unquoted object keys: {name: -> {"name": (outside strings)."""
    out: list[str] = []
    changed = False
    for is_code, text in _segments(s):
        if not is_code:
            out.append(text)
            continue
        new = _BARE_KEY.sub(r'\1"\2"\3', text)
        if new != text:
            changed = True
        out.append(new)
    return "".join(out), changed


def _remove_trailing_commas(s: str) -> tuple[str, bool]:
    """Drop commas directly before } or ] (outside strings)."""
    out: list[str] = []
    changed = False
    for is_code, text in _segments(s):
        if not is_code:
            out.append(text)
            continue
        new = text
        for _ in range(10):  # bounded; handles ,,} chains
            nxt = _TRAILING_COMMA.sub(r"\1", new)
            if nxt == new:
                break
            new = nxt
        if new != text:
            changed = True
        out.append(new)
    return "".join(out), changed


def _is_json_number(tok: str) -> bool:
    return bool(_JSON_NUMBER.match(tok)) and tok.lower() not in _NON_FINITE


def _quote_bare_values(s: str) -> tuple[str, bool]:
    """Quote unquoted scalar values after : [ or , (outside strings).

    Leaves true/false/null and JSON numbers alone. A token followed by `:`
    is key-shaped — strategy 3 owns that case, so it is left untouched.
    """
    out: list[str] = []
    changed = False
    for is_code, text in _segments(s):
        if not is_code:
            out.append(text)
            continue
        buf: list[str] = []
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c == ":" or c == "," or c == "[":
                buf.append(c)
                i += 1
                while i < n and text[i] in " \t\r\n":
                    buf.append(text[i])
                    i += 1
                m = _BARE_TOKEN.match(text, i)
                if m:
                    tok = m.group(0)
                    j = m.end()
                    k = j
                    while k < n and text[k] in " \t\r\n":
                        k += 1
                    nxt = text[k] if k < n else ""
                    if nxt == ":" or tok in ("true", "false", "null") or _is_json_number(tok):
                        buf.append(tok)
                    else:
                        buf.append('"' + tok + '"')
                        changed = True
                    i = j
            else:
                buf.append(c)
                i += 1
        out.append("".join(buf))
    return "".join(out), changed


_STRATEGIES: list[tuple[str, object]] = [
    (FIX_COMMENTS, _strip_comments),
    (FIX_QUOTES, _convert_single_quotes),
    (FIX_KEYS, _quote_bare_keys),
    (FIX_TRAILING, _remove_trailing_commas),
    (FIX_VALUES, _quote_bare_values),
]


def _dump(obj: object, indent: int) -> str:
    if indent == 0:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return json.dumps(obj, indent=indent, ensure_ascii=False)


def process(text: str, mode: str = "fix", indent: int = 2):
    """Validate (and optionally repair) a JSON document.

    Returns (valid, fixed, fixes_applied, errors):
      valid         — the input parsed as-is.
      fixed         — repaired pretty-printed JSON, or None when unfixable.
                      When the input was already valid, the pretty input.
      fixes_applied — repair strategies that fired, in order.
      errors        — parse errors; always populated when valid is False.
    """
    errors: list[str] = []
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as exc:
        errors.append(str(exc) or type(exc).__name__)
    else:
        return True, _dump(obj, indent), [], []

    if mode == "validate":
        return False, None, [], errors

    current = text
    fixes: list[str] = []
    for label, fn in _STRATEGIES:
        new, changed = fn(current)  # type: ignore[operator]
        if changed:
            fixes.append(label)
            current = new
        try:
            obj = json.loads(current)
        except (ValueError, RecursionError) as exc:
            errors.append(str(exc) or type(exc).__name__)
            continue
        return False, _dump(obj, indent), fixes, errors
    return False, None, fixes, errors
