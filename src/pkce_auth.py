"""
CursorRouter v3.1 — Pure Official OAuth 2.0 PKCE Client (RFC 7636)
==================================================================
Implements Cursor IDE's standard interactive browser OAuth PKCE flow:
  1. Generate cryptographically secure 256-bit `verifier`, SHA-256 `challenge`, and `uuid`.
  2. Construct official Cursor sign-in URL:
       https://cursor.com/loginDeepControl?challenge=<challenge>&uuid=<uuid>&mode=login
  3. Poll `https://api2.cursor.sh/auth/poll?uuid=<uuid>&verifier=<verifier>` until the
     user confirms sign-in in their browser and Cursor issues a genuine 60-day desktop
     IDE OAuth token pair (`accessToken` + `refreshToken`, JWT `type: "session"`).
"""

import base64
import hashlib
import os
import sys
import threading
import time
import uuid
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_WEBSITE_URL = "https://cursor.com"
DEFAULT_API_BASE_URL = "https://api2.cursor.sh"


def base64url_encode(data: bytes) -> str:
    """RFC 7636 URL-safe Base64 encoding without '=' padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce_credentials() -> Dict[str, str]:
    """
    Generate RFC 7636 S256 PKCE credentials + unique login flow UUID:
      - verifier: 32 random bytes base64url-encoded (43 chars)
      - challenge: SHA-256(verifier) base64url-encoded (43 chars)
      - uuid: UUIDv4 string
    """
    verifier = base64url_encode(os.urandom(32))
    challenge = base64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    flow_uuid = str(uuid.uuid4())
    return {
        "verifier": verifier,
        "challenge": challenge,
        "uuid": flow_uuid,
    }


def build_oauth_login_url(
    challenge: str,
    flow_uuid: str,
    website_url: str = DEFAULT_WEBSITE_URL,
    mode: str = "login",
) -> str:
    """Construct Cursor's official interactive PKCE sign-in URL."""
    base = website_url.rstrip("/")
    return f"{base}/loginDeepControl?challenge={challenge}&uuid={flow_uuid}&mode={mode}"


def is_valid_jwt_structure(token: Optional[str]) -> bool:
    """Return True if string has a standard 3-segment JWT structure."""
    if not token or not isinstance(token, str):
        return False
    parts = token.strip().split(".")
    return len(parts) == 3 and all(len(p) >= 8 for p in parts)


class CursorOAuthPKCEClient:
    """
    Pooled HTTP client for Cursor's official interactive OAuth 2.0 PKCE flow.
    """

    ADAPTIVE_POLL_SCHEDULE = (0.5, 1.0, 1.25, 1.5, 1.5, 2.0)

    def __init__(
        self,
        website_url: str = DEFAULT_WEBSITE_URL,
        api_base_url: str = DEFAULT_API_BASE_URL,
    ):
        self.website_url = website_url.rstrip("/")
        self.api_base_url = api_base_url.rstrip("/")
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "CursorRouter/3.1 (OAuth-PKCE)",
        })

    def create_login_session(self, mode: str = "login") -> Dict[str, str]:
        """Create a new PKCE OAuth login session containing verifier, challenge, uuid, and login_url."""
        pkce = generate_pkce_credentials()
        pkce["login_url"] = build_oauth_login_url(
            challenge=pkce["challenge"],
            flow_uuid=pkce["uuid"],
            website_url=self.website_url,
            mode=mode,
        )
        return pkce

    def poll_login_once(self, flow_uuid: str, verifier: str, timeout: float = 8.0) -> Dict[str, Any]:
        """
        Perform one non-blocking poll against `api2.cursor.sh/auth/poll`.
        Returns:
          - {"status": "completed", "accessToken": str, "refreshToken": str, "raw": dict}
          - {"status": "pending"}
          - {"status": "rejected", "http_status": int}
          - {"status": "error", "error": str}
        """
        poll_url = f"{self.api_base_url}/auth/poll?uuid={flow_uuid}&verifier={verifier}"
        try:
            resp = self.session.get(poll_url, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                if isinstance(data, dict) and data.get("accessToken"):
                    return {
                        "status": "completed",
                        "accessToken": data["accessToken"],
                        "refreshToken": data.get("refreshToken") or data["accessToken"],
                        "raw": data,
                    }
                return {"status": "pending"}
            if resp.status_code == 404:
                return {"status": "pending"}
            if resp.status_code in (401, 403):
                return {"status": "rejected", "http_status": resp.status_code}
            return {"status": "pending", "http_status": resp.status_code}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def poll_until_complete(
        self,
        flow_uuid: str,
        verifier: str,
        timeout_sec: float = 240.0,
        poll_interval: Optional[float] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Poll `api2.cursor.sh/auth/poll` until the user finishes interactive sign-in
        in their browser, or until timeout / stop_event is triggered.
        """
        deadline = time.time() + max(1.0, float(timeout_sec))
        attempt = 0
        while time.time() < deadline:
            if stop_event and stop_event.is_set():
                return None

            res = self.poll_login_once(flow_uuid, verifier)
            if res.get("status") == "completed":
                return {
                    "accessToken": res["accessToken"],
                    "refreshToken": res["refreshToken"],
                    "authMethod": "oauth_pkce",
                }
            if res.get("status") == "rejected":
                return None

            if poll_interval is not None:
                sleep_s = float(poll_interval)
            else:
                idx = min(attempt, len(self.ADAPTIVE_POLL_SCHEDULE) - 1)
                sleep_s = self.ADAPTIVE_POLL_SCHEDULE[idx]
            attempt += 1

            if stop_event:
                if stop_event.wait(timeout=sleep_s):
                    return None
            else:
                time.sleep(sleep_s)

        return None


# Alias for clean internal imports
CursorAuthClient = CursorOAuthPKCEClient
