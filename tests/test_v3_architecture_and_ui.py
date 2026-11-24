"""
CursorRouter v3.1 Pure OAuth Architecture & Offline UI Regression Suite
=======================================================================
Covers:
- Pure Official OAuth PKCE client & interactive API (/api/auth/pkce/*)
- 1-Click Capture Active Cursor IDE Session (/api/accounts/capture-active)
- Direct OAuth session token restore (/api/accounts/import-token)
- OAuth pool summary (/api/pool/summary) & HTTP/2 sync trigger (/api/accounts/sync-all)
- GET /api/dashboard/snapshot 1-RTT state & delta versioning
- GET /api/events SSE stream handshake
- TokenPoolManager decrypting enc:v1: tokens, excluding web-only tokens, and blind token_hash updates
- Native Windows DPAPI cursor-safe-storage:v1 encrypt/decrypt round-trip on authTokenEncryptionAtRest DBs
- token_cipher bounded LRU cache correctness
- String-aware JSONC parsing (preserves http:// URLs inside strings)
- app_paths data/resource resolution
- Anti-detect native_standard preservation
- /api/cursor/network-test & secret redaction on /api/accounts & /api/status
- 100% offline local static assets (zero Tailwind CDN / Google Fonts)
"""

import base64
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import app_paths
import jsonc_utils
import server
from cursor_settings import CursorSettingsManager
from cursor_storage import (
    CursorStorageManager,
    decrypt_cursor_safe_storage,
    detect_token_type,
    encrypt_cursor_safe_storage,
)
from token_cipher import (
    clear_decrypt_cache,
    compute_token_hash,
    decrypt_token,
    encrypt_token,
)
from token_pool import TokenPoolManager


def _make_jwt(email: str, sub: str = "auth0|user_v3", tok_type: str = "session") -> str:
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    pay = base64.urlsafe_b64encode(
        json.dumps({"email": email, "sub": sub, "type": tok_type, "exp": 1999999999}).encode()
    ).decode().rstrip("=")
    return f"{hdr}.{pay}.sig_v3_test"


class TestV3ArchitectureAndUI(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.client.testing = True

    def test_01_jsonc_preserves_urls_inside_strings(self):
        raw = """
        // Leading comment
        {
            "http.proxy": "http://127.0.0.1:8999", // inline comment
            /* multi-line
               block comment */
            "cursor.note": "keep /* not a comment */ and https://api2.cursor.sh",
            "trailing": true,
        }
        """
        parsed = jsonc_utils.loads_jsonc(raw)
        self.assertEqual(parsed["http.proxy"], "http://127.0.0.1:8999")
        self.assertIn("https://api2.cursor.sh", parsed["cursor.note"])
        self.assertTrue(parsed["trailing"])

    def test_02_token_cipher_lru_cache_and_blind_hash(self):
        tok = _make_jwt("cipher@test.com")
        enc = encrypt_token(tok)
        self.assertTrue(enc.startswith("enc:v1:"))
        clear_decrypt_cache()
        self.assertEqual(decrypt_token(enc), tok)
        self.assertEqual(decrypt_token(enc), tok)
        self.assertEqual(len(compute_token_hash(tok)), 32)
        self.assertEqual(compute_token_hash(tok), compute_token_hash(tok))

    def test_03_token_pool_decrypts_and_marks_rate_limited(self):
        tmp = tempfile.mkdtemp(prefix="cr_v3_tpool_")
        db_path = os.path.join(tmp, "test.db")
        try:
            tok = _make_jwt("tpool@test.com")
            enc = encrypt_token(tok)
            thash = compute_token_hash(tok)
            con = sqlite3.connect(db_path)
            con.execute("""
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT,
                    access_token TEXT,
                    token_hash TEXT,
                    token_type TEXT DEFAULT 'session',
                    usage_percent REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'READY',
                    cooldown_until INTEGER DEFAULT 0
                )
            """)
            con.execute(
                "INSERT INTO accounts (email, access_token, token_hash, token_type, usage_percent, status) VALUES (?, ?, ?, 'session', 10.0, 'READY')",
                ("tpool@test.com", enc, thash),
            )
            con.commit()
            con.close()

            tp = TokenPoolManager(db_path=db_path)
            self.assertEqual(tp.get_stats()["ready_count"], 1)
            picked = tp.get_token_from_cache()
            self.assertIsNotNone(picked)
            self.assertEqual(picked["access_token"], tok)
            self.assertFalse(picked["access_token"].startswith("enc:v1:"))

            tp.mark_rate_limited(tok, async_db=False)
            self.assertIsNone(tp.get_token_from_cache())

            con = sqlite3.connect(db_path)
            status, cd = con.execute("SELECT status, cooldown_until FROM accounts WHERE token_hash = ?", (thash,)).fetchone()
            con.close()
            self.assertEqual(status, "EXHAUSTED")
            self.assertGreater(cd, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_04_antidetect_native_standard_preserves_storage(self):
        tmp = tempfile.mkdtemp(prefix="cr_v3_ad_")
        try:
            storage_path = os.path.join(tmp, "storage.json")
            original = {
                "telemetry.machineId": "orig_machine_123",
                "telemetry.macMachineId": "orig_mac_456",
                "telemetry.devDeviceId": "orig-dev-789",
                "telemetry.sqmId": "{ORIG-SQM}",
            }
            with open(storage_path, "w", encoding="utf-8") as f:
                json.dump(original, f)

            mgr = CursorSettingsManager(workspace_dir=tmp, cursor_user_dir=tmp)
            mgr.storage_path = storage_path
            mgr.storage_bak_path = storage_path + ".bak"
            mgr.storage_orig_bak_path = storage_path + ".orig.bak"
            mgr.update_auto_switch_config({"antidetect_mode": "native_standard"})

            res = mgr.apply_antidetect_mode(fingerprint={"machineId": "should_not_apply"})
            self.assertTrue(res)
            with open(storage_path, "r", encoding="utf-8") as f:
                after = json.load(f)
            self.assertEqual(after, original)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_05_app_paths_env_override(self):
        tmp = tempfile.mkdtemp(prefix="cr_v3_paths_")
        try:
            with patch.dict(os.environ, {"CURSOR_ROUTER_DATA_DIR": tmp}):
                self.assertEqual(os.path.abspath(app_paths.get_data_dir()), os.path.abspath(tmp))
                self.assertTrue(app_paths.get_db_path().startswith(os.path.abspath(tmp)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_06_pool_summary_sync_all_and_dashboard_snapshot(self):
        res = self.client.get("/api/pool/summary")
        self.assertEqual(res.status_code, 200)
        summary = res.get_json()
        self.assertTrue(summary["success"])
        self.assertEqual(summary["auth_mode"], "oauth_pkce")
        for key in ("total_accounts", "valid_tokens", "session_tokens", "ready_count", "tier1_count", "tier2_count", "exhausted_count"):
            self.assertIn(key, summary)

        with patch.object(server.pool, "start_startup_quota_sync", return_value=True) as mock_sync:
            sync_res = self.client.post("/api/accounts/sync-all")
            self.assertEqual(sync_res.status_code, 200)
            self.assertTrue(sync_res.get_json()["success"])
            mock_sync.assert_called_once_with(max_workers=12, force=True)

        snap_res = self.client.get("/api/dashboard/snapshot?include_accounts=1")
        self.assertEqual(snap_res.status_code, 200)
        snap = snap_res.get_json()
        self.assertTrue(snap["success"])
        for key in ("version", "status", "stats", "auto_rotate", "proxy", "keyboard", "accounts"):
            self.assertIn(key, snap)

        snap_delta = self.client.get(f"/api/dashboard/snapshot?include_accounts=1&since_version={snap['version']}").get_json()
        self.assertNotIn("accounts", snap_delta)

    def test_07_api_network_test_and_secret_redaction(self):
        fake_resp = MagicMock(status_code=405)
        with patch("server.requests.get", return_value=fake_resp):
            res = self.client.get("/api/cursor/network-test")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertTrue(data["upstream_ok"])
            self.assertIsInstance(data["proxy_running"], bool)

        accs = self.client.get("/api/accounts").get_json()
        for a in accs:
            self.assertNotIn("access_token", a)
            self.assertNotIn("refresh_token", a)
            self.assertNotIn("cookie_full", a)

        st = self.client.get("/api/status").get_json()
        self.assertNotIn("access_token", st)
        self.assertNotIn("refresh_token", st)

    def test_08_offline_first_ui_and_static_assets(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        self.assertNotIn("cdn.tailwindcss.com", html)
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("fonts.gstatic.com", html)
        self.assertIn("startInteractiveOAuthLogin", html)
        self.assertIn("captureActiveCursorAccount", html)

        for asset in (
            "/static/css/tokens.css",
            "/static/css/app.css",
            "/static/js/utils.js",
            "/static/js/cockpit.js",
            "/static/js/modals.js",
        ):
            self.assertIn(asset, html)
            r = self.client.get(asset)
            self.assertEqual(r.status_code, 200, f"Static asset failed: {asset}")
            self.assertGreater(len(r.data), 200)

    def test_09_sse_events_handshake(self):
        res = self.client.get("/api/events")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/event-stream", res.content_type)
        first_chunk = next(res.response).decode("utf-8", errors="ignore")
        self.assertIn("event: connected", first_chunk)
        res.close()

    def test_10_token_type_capture_active_and_import_token(self):
        tok_sess = _make_jwt("ide@test.com", tok_type="session")
        tok_web = _make_jwt("web@test.com", tok_type="web")
        self.assertEqual(detect_token_type(tok_sess), "session")
        self.assertEqual(detect_token_type(tok_web), "web")
        self.assertEqual(detect_token_type("not-a-jwt"), "unknown")

        with patch.object(server.pool.storage, "get_active_account", return_value={
            "email": "captured_ide@test.com",
            "displayName": "Captured IDE User",
            "authId": "auth0|captured_ide",
            "access_token": tok_sess,
            "refresh_token": tok_sess,
            "token_type": "session",
        }), patch.object(server.pool.storage, "fetch_profile_from_api", return_value={
            "email": "captured_ide@test.com",
            "displayName": "Captured IDE User",
            "authId": "auth0|captured_ide",
            "usagePercent": 12.5,
            "totalSpend": 25.0,
            "usage_fetched": True,
        }):
            res = self.client.post("/api/accounts/capture-active")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["email"], "captured_ide@test.com")
            self.assertEqual(data["token_type"], "session")
            self.client.delete(f"/api/accounts/{data['account_id']}")

        # Direct OAuth session token restore endpoint
        tok_imp = _make_jwt("restored_oauth@test.com", tok_type="session")
        with patch.object(server.pool.storage, "fetch_profile_from_api", return_value={
            "email": "restored_oauth@test.com",
            "displayName": "Restored OAuth",
            "authId": "auth0|restored",
            "usagePercent": 4.0,
            "totalSpend": 8.0,
            "usage_fetched": True,
        }):
            res2 = self.client.post("/api/accounts/import-token", json={"access_token": tok_imp})
            self.assertEqual(res2.status_code, 200)
            d2 = res2.get_json()
            self.assertTrue(d2["success"])
            self.assertEqual(d2["email"], "restored_oauth@test.com")
            self.client.delete(f"/api/accounts/{d2['account_id']}")

    def test_11_interactive_pkce_and_web_only_exclusion(self):
        with patch("webbrowser.open", return_value=True), \
             patch("server._pkce_poll_worker", return_value=None):
            res = self.client.post("/api/auth/pkce/start", json={"open_browser": False})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertIn("https://cursor.com/loginDeepControl?", data["login_url"])
            sid = data["session_id"]

            st = self.client.get(f"/api/auth/pkce/status/{sid}").get_json()
            self.assertEqual(st["status"], "waiting")

            cn = self.client.post(f"/api/auth/pkce/cancel/{sid}").get_json()
            self.assertEqual(cn["status"], "cancelled")

        tmp = tempfile.mkdtemp(prefix="cr_v3_web_excl_")
        db_path = os.path.join(tmp, "test.db")
        try:
            web_tok = _make_jwt("webonly@test.com", tok_type="web")
            sess_tok = _make_jwt("idesess@test.com", tok_type="session")
            con = sqlite3.connect(db_path)
            con.execute("""
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT,
                    access_token TEXT,
                    token_hash TEXT,
                    token_type TEXT DEFAULT 'session',
                    usage_percent REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'READY',
                    cooldown_until INTEGER DEFAULT 0
                )
            """)
            con.execute(
                "INSERT INTO accounts (email, access_token, token_hash, token_type, usage_percent, status) VALUES (?, ?, ?, 'web', 1.0, 'READY')",
                ("webonly@test.com", encrypt_token(web_tok), compute_token_hash(web_tok)),
            )
            con.execute(
                "INSERT INTO accounts (email, access_token, token_hash, token_type, usage_percent, status) VALUES (?, ?, ?, 'session', 20.0, 'READY')",
                ("idesess@test.com", encrypt_token(sess_tok), compute_token_hash(sess_tok)),
            )
            con.commit()
            con.close()

            tp = TokenPoolManager(db_path=db_path)
            self.assertEqual(tp.get_stats()["ready_count"], 1)
            picked = tp.get_token_from_cache()
            self.assertEqual(picked["email"], "idesess@test.com")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_12_safe_storage_encrypt_decrypt_roundtrip_on_at_rest_db(self):
        tok = _make_jwt("safestorage@test.com", tok_type="session")
        enc = encrypt_cursor_safe_storage(tok)
        if enc is not None:
            self.assertTrue(enc.startswith("cursor-safe-storage:v1:"))
            self.assertEqual(decrypt_cursor_safe_storage(enc), tok)

            tmp = tempfile.mkdtemp(prefix="cr_v3_ss_")
            try:
                vscdb = os.path.join(tmp, "state.vscdb")
                con = sqlite3.connect(vscdb)
                con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
                con.execute("INSERT INTO ItemTable VALUES ('cursor.storage.authTokenEncryptionAtRest', 'true')")
                con.commit()
                con.close()

                sm = CursorStorageManager(db_path=vscdb)
                ok = sm.inject_full_profile(tok, tok, {"email": "safestorage@test.com", "authId": "auth0|ss_user"})
                self.assertTrue(ok)

                con = sqlite3.connect(vscdb)
                raw_stored = con.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()[0]
                last_auth = con.execute("SELECT value FROM ItemTable WHERE key='glass.lastSignedInAuthId'").fetchone()[0]
                con.close()
                self.assertTrue(raw_stored.startswith("cursor-safe-storage:v1:"))
                self.assertEqual(last_auth, "auth0|ss_user")

                active = sm.get_active_account()
                self.assertEqual(active["access_token"], tok)
                self.assertTrue(active["safe_storage_encrypted"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
