import os
import sys
import sqlite3
import json
import time
import base64
import requests
from typing import Optional, Dict, Any

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def decode_jwt_payload(token: Optional[str]) -> Dict[str, Any]:
    """Giai ma payload cua JWT token ma khong can thu vien ngoai."""
    if not token or not isinstance(token, str) or "." not in token:
        return {}
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            data = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
            return json.loads(data)
    except Exception:
        pass
    return {}

class CursorStorageManager:
    def __init__(self, db_path: Optional[str] = None):
        self.appdata = os.getenv("APPDATA") or ""
        if db_path:
            self.db_path = db_path
        else:
            self.db_path = os.path.join(self.appdata, "Cursor", "User", "globalStorage", "state.vscdb")

    def exists(self) -> bool:
        return bool(self.db_path and os.path.exists(self.db_path))

    def get_active_account(self) -> Dict[str, Any]:
        """Doc toan bo thong tin tai khoan dang hoat dong trong Cursor, ho tro fallback JWT & API."""
        info = {
            "email": None,
            "displayName": None,
            "authId": None,
            "has_token": False,
            "access_token": None,
            "refresh_token": None,
            "membership_type": "free",
            "is_expired": False,
            "token_exp": None,
            "db_exists": self.exists()
        }
        
        if not self.exists():
            return info

        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        except Exception:
            con = sqlite3.connect(self.db_path, timeout=5.0)
        cur = con.cursor()
        
        try:
            cur.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'cursorAuth/%' OR key = 'adminSettings.cachedAuthId'")
            for key, val in cur.fetchall():
                if key == "cursorAuth/cachedEmail":
                    info["email"] = val
                elif key == "cursorAuth/accessToken":
                    info["access_token"] = val
                    info["has_token"] = bool(val and len(val) > 20)
                elif key == "cursorAuth/refreshToken":
                    info["refresh_token"] = val
                elif key == "cursorAuth/stripeMembershipType":
                    info["membership_type"] = val
                elif key == "cursorAuth/cachedScopedProfile":
                    try:
                        p = json.loads(val)
                        info["displayName"] = p.get("displayName")
                    except Exception:
                        pass
                elif key == "cursorAuth/stripeMembershipAuthId":
                    info["authId"] = val
                elif key == "adminSettings.cachedAuthId" and not info["authId"]:
                    info["authId"] = val
        finally:
            con.close()

        # 1. Kiem tra JWT payload cua access_token
        if info["access_token"]:
            payload = decode_jwt_payload(info["access_token"])
            if payload:
                exp = payload.get("exp")
                if exp:
                    try:
                        info["token_exp"] = int(exp)
                        info["is_expired"] = time.time() >= int(exp)
                    except Exception:
                        pass
                if not info["authId"] and payload.get("sub"):
                    info["authId"] = payload.get("sub")
                if not info["email"] and payload.get("email"):
                    info["email"] = payload.get("email")

        # 2. Neu van chua co email nhung co access_token hop le, thu fetch API profile
        if not info["email"] and info["access_token"] and not info.get("is_expired", False):
            profile = self.fetch_profile_from_api(info["access_token"])
            if profile and profile.get("email"):
                info["email"] = profile.get("email")
                if not info["displayName"]:
                    info["displayName"] = profile.get("displayName")
                if not info["authId"]:
                    info["authId"] = profile.get("authId")
            
        info["name"] = info.get("displayName")
        return info


    def fetch_profile_from_api(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Goi API aiserver de lay thong tin chuan xac cua token (email, ten, authId, quota)."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        
        profile = {
            "email": None,
            "displayName": "",
            "authId": None,
            "signUpType": "Google",
            "membershipType": "free",
            "usagePercent": 0,
            "totalSpend": 0,
            "displayMessage": "",
            "usage_fetched": False
        }
        
        try:
            # 1. Lay thong tin user tu GetMe
            me_resp = requests.post("https://api2.cursor.sh/aiserver.v1.DashboardService/GetMe", headers=headers, json={}, timeout=6)
            profile["http_status"] = me_resp.status_code
            if me_resp.status_code == 200:
                me_data = me_resp.json()
                profile["email"] = me_data.get("email")
                profile["authId"] = me_data.get("authId") or me_data.get("workosId")
                fn = me_data.get("firstName", "")
                ln = me_data.get("lastName", "")
                profile["displayName"] = f"{fn} {ln}".strip() or profile["email"]
            
            # 2. Lay signup type
            email_resp = requests.post("https://api2.cursor.sh/aiserver.v1.AuthService/GetEmail", headers=headers, json={}, timeout=6)
            if email_resp.status_code == 200:
                ed = email_resp.json()
                st = ed.get("signUpType", "")
                if "GOOGLE" in st:
                    profile["signUpType"] = "Google"
                elif "GITHUB" in st:
                    profile["signUpType"] = "GitHub"
                else:
                    profile["signUpType"] = "Email"

            # 3. Lay quota & usage
            usage_resp = requests.post("https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage", headers=headers, json={}, timeout=6)
            if usage_resp.status_code == 200:
                ud = usage_resp.json()
                plan_usage = ud.get("planUsage", {})
                profile["usagePercent"] = plan_usage.get("totalPercentUsed", 0)
                profile["autoPercentUsed"] = plan_usage.get("autoPercentUsed", 0)
                profile["totalSpend"] = plan_usage.get("totalSpend", 0)
                profile["displayThreshold"] = ud.get("displayThreshold", 200)
                profile["displayMessage"] = ud.get("displayMessage", "")
                profile["usage_fetched"] = True
            elif usage_resp.status_code in (401, 402, 403, 429):
                profile["http_status"] = usage_resp.status_code
                profile["usage_fetched"] = False
            else:
                profile["usage_fetched"] = False
                
            return profile
        except Exception as e:
            print(f"[-] Loi khi fetch profile tu API: {e}")
            return None

    def inject_full_profile(self, access_token: str, refresh_token: Optional[str] = None, profile: Optional[Dict[str, Any]] = None) -> bool:
        """
        Ghi de toan bo token va dong bo toan bo thong tin profile (email, displayName, authId)
        vao state.vscdb de tranh tinh trang dính data cua tai khoan cu!
        """
        # Neu chua co profile hoac thieu email/authId, thu lay tu JWT hoac API
        jwt_info = decode_jwt_payload(access_token)
        if not profile:
            profile = {}

        if not profile.get("email") or not profile.get("authId"):
            if jwt_info.get("email") and not profile.get("email"):
                profile["email"] = jwt_info["email"]
            if jwt_info.get("sub") and not profile.get("authId"):
                profile["authId"] = jwt_info["sub"]
            
            # Neu van thieu email, goi API de lay
            if not profile.get("email"):
                fetched = self.fetch_profile_from_api(access_token)
                if fetched:
                    for k, v in fetched.items():
                        if not profile.get(k):
                            profile[k] = v

        email = profile.get("email") or jwt_info.get("email")
        display_name = profile.get("displayName") or (email.split("@")[0] if email else "")
        auth_id = profile.get("authId") or jwt_info.get("sub")
        sign_up_type = profile.get("signUpType", "Google")
        membership_type = profile.get("membershipType", "free")

        if self.db_path:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=5.0)
        cur = con.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        
        try:
            records = [
                ("cursorAuth/accessToken", access_token),
                ("cursorAuth/stripeMembershipType", membership_type),
                ("cursorAuth/cachedSignUpType", sign_up_type)
            ]
            
            if refresh_token:
                records.append(("cursorAuth/refreshToken", refresh_token))
            if email:
                records.append(("cursorAuth/cachedEmail", email))
            if display_name:
                records.append(("cursorAuth/cachedScopedProfile", json.dumps({"displayName": display_name})))
            if auth_id:
                records.append(("cursorAuth/stripeMembershipAuthId", auth_id))
                records.append(("adminSettings.cachedAuthId", auth_id))
                
            for k, v in records:
                cur.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", (k, str(v)))
                
            con.commit()
            try:
                cur.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception:
                pass
            try:
                safe_name = str(display_name).encode("ascii", errors="replace").decode("ascii")
                print(f"[+] Dong bo hoan toan tai khoan moi: {email} ({safe_name}) vao state.vscdb!")
            except Exception:
                pass
            return True
        except Exception as e:
            print(f"[-] Loi khi ghi profile vao SQLite: {e}")
            try:
                con.rollback()
            except Exception:
                pass
            return False
        finally:
            con.close()

