"""
String-Aware JSONC Parser & Atomic JSON Writer
==============================================
Safely parses VS Code / Cursor JSONC files (comments + trailing commas)
without corrupting string values containing '//' or '/*' (e.g. URLs like
'http://127.0.0.1:8999'), and writes JSON atomically via temporary replace.
"""

import os
import json
import threading
from typing import Any


def strip_jsonc(content: str) -> str:
    """
    Remove // single-line comments, /* ... */ block comments, and trailing commas
    while strictly preserving string literals and escape sequences.
    """
    if not content:
        return content

    # Step 1: Strip comments outside of quoted strings
    out = []
    n = len(content)
    i = 0
    in_string = False

    while i < n:
        ch = content[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(content[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        # Single-line comment //
        if ch == "/" and i + 1 < n and content[i + 1] == "/":
            i += 2
            while i < n and content[i] not in ("\n", "\r"):
                i += 1
            continue

        # Block comment /* ... */
        if ch == "/" and i + 1 < n and content[i + 1] == "*":
            i += 2
            while i + 1 < n and not (content[i] == "*" and content[i + 1] == "/"):
                i += 1
            i += 2 if i + 1 < n else 1
            continue

        out.append(ch)
        i += 1

    no_comments = "".join(out)

    # Step 2: Strip trailing commas before '}' or ']' outside of strings
    res = []
    m = len(no_comments)
    j = 0
    in_string = False
    while j < m:
        ch = no_comments[j]
        if in_string:
            res.append(ch)
            if ch == "\\" and j + 1 < m:
                res.append(no_comments[j + 1])
                j += 2
                continue
            if ch == '"':
                in_string = False
            j += 1
            continue

        if ch == '"':
            in_string = True
            res.append(ch)
            j += 1
            continue

        if ch == ",":
            k = j + 1
            while k < m and no_comments[k] in (" ", "\t", "\r", "\n"):
                k += 1
            if k < m and no_comments[k] in ("}", "]"):
                j += 1
                continue

        res.append(ch)
        j += 1

    return "".join(res)


def loads_jsonc(content: str) -> Any:
    """Parse a JSON or JSONC string cleanly."""
    cleaned = strip_jsonc(content).strip()
    if not cleaned:
        return {}
    return json.loads(cleaned)


def atomic_write_json(path: str, data: Any, indent: int = 4) -> None:
    """Atomically write JSON to disk via a temp sibling file and os.replace()."""
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{abs_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    payload = json.dumps(data, indent=indent, ensure_ascii=False)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_path, abs_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
