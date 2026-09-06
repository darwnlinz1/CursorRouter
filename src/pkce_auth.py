import os
import hashlib
import base64
import uuid
import time
import requests
from typing import Optional, Dict, Any

def base64url_encode(data: bytes) -> str:
    """Encode bytes to URL-safe base64 without padding (Khớp với hàm F(e) trong Cursor)."""
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')

def generate_pkce_credentials() -> Dict[str, str]:
    """Sinh cặp khóa PKCE và Flow UUID."""
    verifier_bytes = os.urandom(32)
    verifier = base64url_encode(verifier_bytes)
    
    digest = hashlib.sha256(verifier.encode('utf-8')).digest()
    challenge = base64url_encode(digest)
    
    flow_uuid = str(uuid.uuid4())
    
    return {
        "uuid": flow_uuid,
        "verifier": verifier,
        "challenge": challenge
    }

def parse_cookie_from_file(file_path: str) -> str:
    """Doc va trich xuat cookie WorkosCursorSessionToken tu file Netscape hoac raw text."""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    for line in content.splitlines():
        if "WorkosCursorSessionToken" in line:
            parts = line.strip().split("\t")
            if len(parts) >= 7:
                return parts[6]
            sub = line.split("WorkosCursorSessionToken")[-1].strip()
            return sub

    return content.strip()

class CursorAuthClient:
    def __init__(self, website_url: str = "https://cursor.com", api_base_url: str = "https://api2.cursor.sh"):
        self.website_url = website_url.rstrip('/')
        self.api_base_url = api_base_url.rstrip('/')
        self.session = requests.Session()
        
        # Thiết lập browser-like headers để tương thích với Cloudflare/Next.js WAF
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        })

    def exchange_cookie_to_tokens(self, session_token: str, max_polls: int = 15, poll_interval: float = 1.0) -> Optional[Dict[str, Any]]:
        """
        Thực hiện quy trình Headless PKCE Handshake:
        1. Sinh verifier, challenge, uuid
        2. Gửi callback xác thực với Cookie lên cursor.com
        3. Poll api2.cursor.sh để lấy accessToken và refreshToken
        """
        # Chuẩn hóa cookie format
        if not session_token.startswith("user_") and "WorkosCursorSessionToken=" in session_token:
            # Nếu người dùng truyền cả chuỗi cookie header
            cookie_val = session_token.split("WorkosCursorSessionToken=")[1].split(";")[0]
        else:
            cookie_val = session_token

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
            "Cookie": f"WorkosCursorSessionToken={cookie_val}"
        }

        payload = {
            "uuid": flow_uuid,
            "challenge": challenge
        }

        print(f"[*] Bat dau Handshake (UUID: {flow_uuid[:8]}...)")
        try:
            # Buoc 1: Gui callback xac nhan phien
            cb_resp = self.session.post(callback_url, json=payload, headers=headers, timeout=10)
            if cb_resp.status_code != 200:
                print(f"[-] Callback that bai voi ma loi HTTP {cb_resp.status_code}: {cb_resp.text[:200]}")
                return None
            print("[+] Callback xac nhan phien thanh cong (HTTP 200)")

            # Buoc 2: Polling token tu backend
            print(f"[*] Dang lay token tu {poll_url}...")
            for attempt in range(1, max_polls + 1):
                time.sleep(poll_interval)
                poll_resp = self.session.get(
                    f"{poll_url}?uuid={flow_uuid}&verifier={verifier}",
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )

                if poll_resp.status_code == 200:
                    data = poll_resp.json()
                    if isinstance(data, dict) and "accessToken" in data:
                        print("[+] Lay Access Token thanh cong!")
                        return data
                elif poll_resp.status_code == 404:
                    continue
                elif poll_resp.status_code == 403:
                    print("[-] Bi tu choi xac thuc (403 Forbidden - Verifier/Challenge khong khop hoac phien het han)")
                    return None
                else:
                    print(f"[-] Poll nhan phan hoi la: HTTP {poll_resp.status_code}")

            print(f"[-] Qua thoi gian cho ({max_polls} lan thu).")
            return None

        except Exception as e:
            print(f"[-] Ngoai le ket noi: {e}")
            return None
