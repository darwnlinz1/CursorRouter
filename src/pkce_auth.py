"""
Cursor PKCE Authentication & Multi-Format Cookie Parser (v3.0)
==============================================================
Supports:
1. Multi-format Cookie Extraction:
   - Netscape HTTP Cookie Jar (7-column TSV; skips '#' comments; picks freshest expiry)
   - Cookie-Editor JSON exports (list of cookie dicts or key-value JSON object)
   - HTTP Cookie header / Key=Value format (`WorkosCursorSessionToken=...; Path=/`)
   - Raw `user_...%3A%3A<jwt>` / `user_...::<jwt>` / raw JWT strings
   - Batch paste parsing (`parse_cookies_batch`) for multi-line / multi-account paste
2. Zero-Latency Direct Session JWT Extraction (`extract_jwt_from_session_cookie`):
   - Decodes the embedded JWT from `WorkosCursorSessionToken` in ~0.01ms.
3. Dual-Path Headless PKCE Handshake (`CursorAuthClient`):
   - Pooled `requests.Session` with modern browser headers & automatic CSRF double-submit
     handshake (`GET /api/csrf-token` -> `x-csrf-token` header + `csrf-token` cookie).
   - Adaptive fast polling intervals (preserving explicit `poll_interval` when specified).
   - Seamless fallback to verified embedded session JWT when Cursor enforces
     `FRESH_SIGN_IN_REQUIRED` on older web cookies.
"""

import os
import re
import json
import uuid
import time
import base64
import hashlib
import urllib.parse
import requests
from requests.adapters import HTTPAdapter
from typing import Optional, Dict, Any, List, Tuple


def base64url_encode(data: bytes) -> str:
    """Encode bytes to URL-safe base64 without padding (RFC 7636)."""
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def generate_pkce_credentials() -> Dict[str, str]:
    """Generate cryptographically secure PKCE verifier, SHA-256 challenge, and Flow UUID."""
    verifier_bytes = os.urandom(32)
    verifier = base64url_encode(verifier_bytes)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64url_encode(digest)
    flow_uuid = str(uuid.uuid4())
    return {
        "uuid": flow_uuid,
        "verifier": verifier,
        "challenge": challenge,
    }


def _clean_cookie_value(val: str) -> str:
    """Strip surrounding quotes, leading '=', trailing cookie attributes, and whitespace."""
    if not val:
        return ""
    v = val.strip()
    if v.startswith("="):
        v = v[1:].strip()
    if ";" in v:
        v = v.split(";", 1)[0].strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1].strip()
    return v


def _is_valid_jwt_structure(token: str) -> bool:
    """Return True if token is a syntactically valid 3-segment JWT with a JSON object payload."""
    if not token or not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return False
    try:
        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
        obj = json.loads(decoded)
        return isinstance(obj, dict) and bool(obj)
    except Exception:
        return False


def extract_jwt_from_session_cookie(session_token: Optional[str]) -> Optional[str]:
    """
    Extract the embedded JWT access token from a WorkosCursorSessionToken string
    (e.g. `user_01...%3A%3AeyJ...` or `user_01...::eyJ...` or raw `eyJ...`).
    Returns the raw JWT string if structurally valid, otherwise None.
    """
    if not session_token or not isinstance(session_token, str):
        return None

    val = _clean_cookie_value(session_token)
    if "WorkosCursorSessionToken=" in val:
        val = _clean_cookie_value(val.split("WorkosCursorSessionToken=", 1)[1])

    decoded = urllib.parse.unquote(val)
    candidate = decoded.split("::", 1)[1].strip() if "::" in decoded else decoded.strip()
    if _is_valid_jwt_structure(candidate):
        return candidate
    return None


def parse_cookie_from_text(content: str) -> str:
    """
    Extract WorkosCursorSessionToken from raw text supporting:
    1. JSON (Cookie-Editor array or dict)
    2. Netscape HTTP Cookie File (7-column TSV, ignoring '#' comments, choosing freshest expiry)
    3. HTTP Cookie Header / Key=Value (`WorkosCursorSessionToken=...; ...`)
    4. Raw token value
    """
    if not content or not isinstance(content, str):
        return ""

    stripped = content.strip()
    if not stripped:
        return ""

    # 1. JSON format (Cookie-Editor array or object)
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                best_val = ""
                best_exp = -1.0
                for item in parsed:
                    if isinstance(item, dict) and item.get("name") == "WorkosCursorSessionToken":
                        val = _clean_cookie_value(str(item.get("value", "")))
                        exp = float(item.get("expirationDate") or item.get("expiry") or 0)
                        if val and exp >= best_exp:
                            best_val = val
                            best_exp = exp
                if best_val:
                    return best_val
            elif isinstance(parsed, dict):
                if "WorkosCursorSessionToken" in parsed:
                    return _clean_cookie_value(str(parsed["WorkosCursorSessionToken"]))
                if parsed.get("name") == "WorkosCursorSessionToken" and parsed.get("value"):
                    return _clean_cookie_value(str(parsed["value"]))
                cookies_nested = parsed.get("cookies")
                if isinstance(cookies_nested, list):
                    for item in cookies_nested:
                        if isinstance(item, dict) and item.get("name") == "WorkosCursorSessionToken":
                            val = _clean_cookie_value(str(item.get("value", "")))
                            if val:
                                return val
        except Exception:
            pass

    # 2. Netscape 7-column TSV (skip comment lines starting with '#')
    netscape_candidates: List[Tuple[float, int, str]] = []
    header_candidate: Optional[str] = None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "WorkosCursorSessionToken" not in line:
            continue

        parts = line.split("\t")
        if len(parts) >= 7 and parts[5].strip() == "WorkosCursorSessionToken":
            val = _clean_cookie_value(parts[6])
            if val:
                domain = parts[0].strip().lower()
                try:
                    exp = float(parts[4].strip())
                except Exception:
                    exp = 0.0
                domain_prio = 2 if domain in ("cursor.com", ".cursor.com") else (1 if "cursor.com" in domain else 0)
                netscape_candidates.append((exp, domain_prio, val))
                continue

        # Key=Value / HTTP Cookie header format
        m = re.search(r"WorkosCursorSessionToken\s*=\s*([^;\s\"']+)", line)
        if m:
            cleaned = _clean_cookie_value(m.group(1))
            if cleaned:
                header_candidate = cleaned
                break

        # Fallback: token after keyword
        sub = _clean_cookie_value(line.split("WorkosCursorSessionToken", 1)[-1])
        if sub:
            header_candidate = sub
            break

    if netscape_candidates:
        netscape_candidates.sort(key=lambda x: (x[0], x[1], len(x[2])), reverse=True)
        return netscape_candidates[0][2]

    if header_candidate:
        return header_candidate

    # 3. Raw cookie / JWT fallback (ignore leading comment lines)
    non_comment_lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
    if non_comment_lines:
        return _clean_cookie_value(non_comment_lines[0])
    return ""


def parse_cookies_batch(content: str) -> List[Dict[str, Optional[str]]]:
    """
    Parse a pasted text blob that may contain ONE or MULTIPLE cookies in any format:
    - Single Netscape cookie jar or Cookie-Editor JSON export
    - Multi-line list of `email|cookie`, `email:cookie`, `WorkosCursorSessionToken=...`,
      or raw `user_...%3A%3AeyJ...` lines.
    Returns list of `{"email_hint": Optional[str], "cookie": str, "format": str}`.
    """
    if not content or not isinstance(content, str):
        return []

    stripped = content.strip()
    if not stripped:
        return []

    # Case 1: JSON export (single account cookie jar)
    if stripped.startswith("[") or stripped.startswith("{"):
        tok = parse_cookie_from_text(stripped)
        if tok:
            return [{"email_hint": None, "cookie": tok, "format": "json"}]

    # Case 2: Netscape TSV jar (may have multiple lines for 1 account, or multiple unique tokens)
    lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
    if any("\t" in l and len(l.split("\t")) >= 7 for l in lines):
        seen_vals = set()
        results = []
        for l in lines:
            parts = l.split("\t")
            if len(parts) >= 7 and parts[5].strip() == "WorkosCursorSessionToken":
                val = _clean_cookie_value(parts[6])
                if val and val not in seen_vals:
                    seen_vals.add(val)
                    results.append({"email_hint": None, "cookie": val, "format": "netscape"})
        if results:
            return results

    # Case 3: Multi-line batch (`email|cookie`, `WorkosCursorSessionToken=...`, or raw tokens)
    results = []
    seen = set()
    for line in lines:
        email_hint = None
        tok = ""
        fmt = "raw"

        # Pattern: email|cookie or email\tcookie
        if "|" in line:
            left, right = line.split("|", 1)
            if "@" in left and len(right.strip()) > 15:
                email_hint = left.strip()
                tok = parse_cookie_from_text(right)
                fmt = "delimited"

        if not tok:
            if "WorkosCursorSessionToken=" in line:
                tok = parse_cookie_from_text(line)
                fmt = "header"
            else:
                candidate = _clean_cookie_value(line)
                if candidate.startswith("user_") or _is_valid_jwt_structure(candidate) or len(candidate) >= 32:
                    tok = candidate
                    fmt = "jwt" if _is_valid_jwt_structure(candidate) else "raw"

        if tok and tok not in seen:
            seen.add(tok)
            results.append({"email_hint": email_hint, "cookie": tok, "format": fmt})

    return results


def parse_cookie_from_file(file_path: str) -> str:
    """Read and extract WorkosCursorSessionToken from Netscape, JSON, Header, or raw text file."""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    return parse_cookie_from_text(content)


class CursorAuthClient:
    """
    Pooled HTTP client for Cursor PKCE authentication with automatic CSRF double-submit
    handling and direct embedded session JWT fallback.
    """

    ADAPTIVE_POLL_SCHEDULE = (0.15, 0.25, 0.4, 0.6, 0.8, 1.0, 1.25, 1.5)

    def __init__(self, website_url: str = "https://cursor.com", api_base_url: str = "https://api2.cursor.sh"):
        self.website_url = website_url.rstrip("/")
        self.api_base_url = api_base_url.rstrip("/")
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        })

    def _fetch_csrf_token(self, cookie_val: str) -> Tuple[Optional[str], Optional[str]]:
        """Fetch Cursor double-submit CSRF token + csrf-token cookie."""
        try:
            resp = self.session.get(
                f"{self.website_url}/api/csrf-token",
                headers={"Cookie": f"WorkosCursorSessionToken={cookie_val}"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token") if isinstance(data, dict) else None
                csrf_cookie = resp.cookies.get("csrf-token") if hasattr(resp, "cookies") else None
                return token, csrf_cookie
        except Exception:
            pass
        return None, None

    def exchange_cookie_to_tokens(
        self,
        session_token: str,
        max_polls: int = 15,
        poll_interval: Optional[float] = None,
        allow_direct_jwt_fallback: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Execute Headless PKCE Handshake with automatic CSRF handling and direct JWT fallback:
        1. Normalize session cookie.
        2. POST /api/auth/loginDeepCallbackControl (automatically fetching /api/csrf-token if 403 CSRF).
        3. Poll api2.cursor.sh/auth/poll using adaptive fast backoff (or caller's poll_interval).
        4. If Cursor enforces FRESH_SIGN_IN_REQUIRED / callback rejection on an older valid web
           cookie that embeds a valid 3-segment JWT, fall back seamlessly to the embedded JWT.
        """
        if not session_token:
            return None

        if not session_token.startswith("user_") and "WorkosCursorSessionToken=" in session_token:
            cookie_val = _clean_cookie_value(session_token.split("WorkosCursorSessionToken=", 1)[1])
        else:
            cookie_val = _clean_cookie_value(session_token)

        pkce = generate_pkce_credentials()
        flow_uuid = pkce["uuid"]
        challenge = pkce["challenge"]
        verifier = pkce["verifier"]

        callback_url = f"{self.website_url}/api/auth/loginDeepCallbackControl"
        poll_url = f"{self.api_base_url}/auth/poll"

        headers = {
            "Origin": self.website_url,
            "Referer": f"{self.website_url}/loginDeepControl?challenge={challenge}&uuid={flow_uuid}&mode=login",
            "Content-Type": "application/json",
            "Cookie": f"WorkosCursorSessionToken={cookie_val}",
        }
        payload = {"uuid": flow_uuid, "challenge": challenge}

        try:
            cb_resp = self.session.post(callback_url, json=payload, headers=headers, timeout=10)

            # Auto-handle Cursor's double-submit CSRF requirement when challenged
            if cb_resp.status_code == 403 and "csrf" in (getattr(cb_resp, "text", "") or "").lower():
                csrf_tok, csrf_cookie = self._fetch_csrf_token(cookie_val)
                if csrf_tok:
                    retry_headers = dict(headers)
                    retry_headers["x-csrf-token"] = csrf_tok
                    if csrf_cookie:
                        retry_headers["Cookie"] = f"WorkosCursorSessionToken={cookie_val}; csrf-token={csrf_cookie}"
                    else:
                        retry_headers["Cookie"] = f"WorkosCursorSessionToken={cookie_val}; csrf-token={csrf_tok}"
                    cb_resp = self.session.post(callback_url, json=payload, headers=retry_headers, timeout=10)

            if cb_resp.status_code != 200:
                # Graceful fallback for real cookies containing a valid embedded session JWT
                # (e.g., when Cursor returns 403 FRESH_SIGN_IN_REQUIRED for older web cookies)
                if allow_direct_jwt_fallback:
                    embedded_jwt = extract_jwt_from_session_cookie(cookie_val)
                    if embedded_jwt:
                        return {
                            "accessToken": embedded_jwt,
                            "refreshToken": embedded_jwt,
                            "authMethod": "direct_cookie_jwt",
                        }
                return None

            for attempt in range(1, max_polls + 1):
                if poll_interval is not None:
                    sleep_s = poll_interval
                else:
                    idx = min(attempt - 1, len(self.ADAPTIVE_POLL_SCHEDULE) - 1)
                    sleep_s = self.ADAPTIVE_POLL_SCHEDULE[idx]
                time.sleep(sleep_s)

                poll_resp = self.session.get(
                    f"{poll_url}?uuid={flow_uuid}&verifier={verifier}",
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )

                if poll_resp.status_code == 200:
                    data = poll_resp.json()
                    if isinstance(data, dict) and "accessToken" in data:
                        return data
                elif poll_resp.status_code == 404:
                    continue
                elif poll_resp.status_code == 403:
                    break

            return None

        except Exception as e:
            if allow_direct_jwt_fallback:
                embedded_jwt = extract_jwt_from_session_cookie(cookie_val)
                if embedded_jwt:
                    return {
                        "accessToken": embedded_jwt,
                        "refreshToken": embedded_jwt,
                        "authMethod": "direct_cookie_jwt",
                    }
            print(f"[-] Ngoai le ket noi PKCE: {e}")
            return None
