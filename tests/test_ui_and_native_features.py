import os
import time
import sqlite3
import unittest
from unittest.mock import patch, MagicMock
from account_pool import AccountPoolManager, DB_FILE, QUOTA_EXHAUSTION_THRESHOLD
import server
import windows_notifier

class TestUIAndNativeFeatures(unittest.TestCase):
    def setUp(self):
        self.app = server.app.test_client()
        self.app.testing = True
        self.pool = AccountPoolManager()

    def tearDown(self):
        server.pool.stop_auto_rotate_watcher()
        self.pool.stop_auto_rotate_watcher()

    def test_active_account_sorted_to_row_1(self):
        """Kiem tra tai khoan active luon dung dau tien (#1 row) bat ke usage_percent."""
        # Mock storage.get_active_account tra ve email 'active_user@gmail.com'
        active_email = "active_user@gmail.com"
        
        # Tao DB tam trong bo nho hoac mock du lieu
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute("DELETE FROM accounts WHERE email IN ('active_user@gmail.com', 'user_zero@gmail.com', 'user_mid@gmail.com')")
        
        # user_zero co usage 0.0%, active_user co usage 35.0%, user_mid co usage 20.0%
        cur.execute("""
            INSERT INTO accounts (file_name, email, usage_percent, total_spend, status)
            VALUES ('zero.txt', 'user_zero@gmail.com', 0.0, 0.0, 'READY'),
                   ('active.txt', 'active_user@gmail.com', 35.0, 70.0, 'READY'),
                   ('mid.txt', 'user_mid@gmail.com', 20.0, 40.0, 'READY')
        """)
        con.commit()
        con.close()

        try:
            with patch.object(self.pool.storage, "get_active_account", return_value={"email": active_email}):
                accounts = self.pool.get_all_accounts()
                # Loc chi 3 tai khoan vua them
                test_accs = [a for a in accounts if a["email"] in ("active_user@gmail.com", "user_zero@gmail.com", "user_mid@gmail.com")]
                self.assertEqual(len(test_accs), 3)
                # Tai khoan active phai dung dau tien trong danh sach test_accs
                # Va trong toan bo pool, vi tri #0 phai la tai khoan active
                self.assertTrue(accounts[0]["is_active"])
                self.assertEqual(accounts[0]["email"].lower(), active_email.lower())
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute("DELETE FROM accounts WHERE email IN ('active_user@gmail.com', 'user_zero@gmail.com', 'user_mid@gmail.com')")
            con.commit()
            con.close()

    def test_auto_switch_triggers_notification_and_records_event(self):
        """Kiem tra auto_switch_best_account kich hoat windows notification va ghi nhan rotation event."""
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute("DELETE FROM accounts WHERE email IN ('old_user@test.com', 'new_best@test.com')")
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, total_spend, status)
            VALUES ('new_best.txt', 'new_best@test.com', 'valid_token_123', -1.0, 0.0, 'READY')
        """)
        acc_id = cur.lastrowid
        con.commit()
        con.close()

        try:
            with patch.object(self.pool.storage, "get_active_account", return_value={"email": "old_user@test.com"}), \
                 patch.object(self.pool, "switch_to_account", return_value=True), \
                 patch("windows_notifier.send_windows_notification") as mock_notify:

                res = self.pool.auto_switch_best_account(notify=True)
                self.assertIsNotNone(res)
                self.assertEqual(res["email"], "new_best@test.com")
                
                # Kiem tra last_rotation_event da duoc ghi nhan
                self.assertIsNotNone(self.pool.last_rotation_event)
                self.assertEqual(self.pool.last_rotation_event["from"], "old_user@test.com")
                self.assertEqual(self.pool.last_rotation_event["to"], "new_best@test.com")
                
                # Kiem tra mock notification da duoc goi voi dung title va format
                mock_notify.assert_called_once()
                args, kwargs = mock_notify.call_args
                self.assertEqual(kwargs.get("title") or args[0], "Cursor Account Rotated")
                msg = kwargs.get("message") or args[1]
                self.assertIn("old_user@test.com", msg)
                self.assertIn("new_best@test.com", msg)
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute("DELETE FROM accounts WHERE email IN ('old_user@test.com', 'new_best@test.com')")
            con.commit()
            con.close()

    def test_watcher_5_second_interval(self):
        """Kiem tra start_auto_rotate_watcher co gia tri interval mac dinh la 5s va toggle 5s."""
        import inspect
        sig = inspect.signature(self.pool.start_auto_rotate_watcher)
        self.assertEqual(sig.parameters["interval_seconds"].default, 5)

        # Kiem tra toggle API
        resp = self.app.post("/api/auto-rotate/toggle")
        self.assertEqual(resp.status_code, 200)
        # Dung lai
        server.pool.stop_auto_rotate_watcher()
        self.pool.stop_auto_rotate_watcher()
        self.assertFalse(server.pool.auto_rotate_enabled)

    def test_windows_notifier_module(self):
        """Kiem tra module windows_notifier thuc thi native notification."""
        # Test escape XML
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value.returncode = 0
            ok = windows_notifier._run_powershell_toast(
                "Test <>&\"' Title",
                "Test <>&\"' Message",
                "Test App"
            )
            self.assertTrue(ok)
            mock_sub.assert_called_once()

        # Test async call returns True immediately
        ok_async = windows_notifier.send_windows_notification(
            "Test Async",
            "Test Message",
            async_exec=True
        )
        self.assertTrue(ok_async)

    def test_notify_test_api_endpoint(self):
        """Kiem tra endpoint /api/notify/test tren server."""
        with patch("windows_notifier.send_windows_notification", return_value=True) as mock_n:
            resp = self.app.post("/api/notify/test", json={
                "title": "API Test",
                "message": "Testing Notification"
            })
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            mock_n.assert_called_once()

    def test_cleanup_api_endpoints(self):
        """Kiem tra cac API endpoints don dep tai khoan."""
        # Test clean invalid
        with patch.object(server.pool, "delete_invalid_accounts", return_value=3):
            resp = self.app.post("/api/accounts/clean-invalid")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["deleted"], 3)

        # Test clean exhausted
        with patch.object(server.pool, "delete_exhausted_accounts", return_value=5):
            resp = self.app.post("/api/accounts/clean-exhausted")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["deleted"], 5)

    def test_cookies_open_folder_api(self):
        """Kiem tra endpoint /api/cookies/open-folder mo thu muc va dam bao folder ton tai."""
        with patch("os.startfile", create=True) as mock_start:
            resp = self.app.post("/api/cookies/open-folder")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertTrue(data["opened"])
            self.assertTrue(os.path.exists(data["path"]))
            self.assertIn("Cookies", data["path"])

    def test_cookies_info_api(self):
        """Kiem tra endpoint /api/cookies/info tra ve duong dan, so luong file va tong so accounts."""
        resp = self.app.get("/api/cookies/info")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("path", data)
        self.assertIn("count", data)
        self.assertIn("total_accounts", data)
        self.assertIn("valid_tokens", data)

    def test_frontend_template_elements_and_scripts(self):
        tmpl_path = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
        if not os.path.exists(tmpl_path):
            tmpl_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
        with open(tmpl_path, "r", encoding="utf-8") as f:
            html = f.read()

        # 1. Don dep button & modal
        self.assertIn("openCleanupModal()", html)
        self.assertIn('id="cleanup-modal"', html)
        self.assertIn('id="btn-clean-exhausted-modal"', html)
        self.assertIn('id="btn-clean-invalid-modal"', html)
        self.assertIn('closeCleanupModal()', html)

        # 2. Proxy display: khong duplicate :8999
        self.assertIn('id="proxy-port"', html)
        self.assertIn('id="stat-proxy-status-badge"', html)
        # updateProxyUI khong lap lai Proxy :${port} ${port}
        self.assertIn('txt.textContent = "Proxy: BẬT";', html)
        self.assertIn('txt.textContent = "Proxy: TẮT";', html)

        # 3. 5s auto-rotate badge
        self.assertIn('title="Tự động nạp tài khoản mới khi tài khoản hiện tại hết quota (chu kỳ 5s)"', html)
        self.assertIn('>5s</span>', html)

        # 4. Sorting active account in JS
        self.assertIn('a.is_active', html)
        self.assertIn('pollActiveAndRotation()', html)

        # 5. Keyboard status badge & debug modal
        self.assertIn('id="keyboard-status-badge"', html)
        self.assertIn('id="btn-debug-modal"', html)
        self.assertIn('id="debug-modal"', html)
        self.assertIn('openDebugModal()', html)
        self.assertIn('closeDebugModal()', html)

        # 6. Auto-Updater button & modal
        self.assertIn('id="btn-update-trigger"', html)
        self.assertIn('openUpdateModal()', html)
        self.assertIn('id="update-modal"', html)
        self.assertIn('checkUpdates', html)
        self.assertIn('downloadUpdate', html)
        self.assertIn('applyUpdate', html)

        # 7. Cursor IDE Aesthetic: strictly zero neon and no antigravity branding
        import re
        self.assertEqual(len(re.findall(r'antigravity', html, re.IGNORECASE)), 0)
        self.assertEqual(len(re.findall(r'purple-[0-9]+', html)), 0)
        self.assertEqual(len(re.findall(r'cyan-[0-9]+', html)), 0)
        self.assertEqual(len(re.findall(r'bg-gradient', html)), 0)

        # 8. Cookies Management UI elements
        self.assertIn('id="btn-open-cookie-folder"', html)
        self.assertIn('openCookieFolder()', html)
        self.assertIn('id="btn-convert-cookies"', html)
        self.assertIn('convertCookiesToTokens()', html)
        self.assertIn('id="cookie-tier1-count"', html)
        self.assertIn('id="cookie-tier2-count"', html)
        self.assertIn('id="cookie-exhausted-count"', html)
        self.assertIn('id="filter-count-tier1"', html)
        self.assertIn('id="filter-count-tier2"', html)

    def test_cookies_folder_auto_creation(self):
        """Kiem tra AccountPoolManager va native_app tu dong tao thu muc Cookies."""
        import tempfile, shutil
        tmp_dir = tempfile.mkdtemp()
        try:
            target_cookies = os.path.join(tmp_dir, "AutoCreatedCookies")
            self.assertFalse(os.path.exists(target_cookies))
            mgr = AccountPoolManager(cookies_dir=target_cookies)
            self.assertTrue(os.path.exists(target_cookies))
            self.assertTrue(os.path.isdir(target_cookies))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_cookies_info_complete_telemetry(self):
        """Kiem tra endpoint /api/cookies/info cung cap day du telemetry quota."""
        resp = self.app.get("/api/cookies/info")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        for key in ("path", "count", "total_accounts", "valid_tokens", "ready_count", "tier1_count", "tier2_count", "exhausted_count", "pending_count"):
            self.assertIn(key, data)

    def test_cookies_convert_12_threads_api(self):
        """Kiem tra endpoint /api/cookies/convert kich hoat dong bo 12 luong song song."""
        with patch.object(server.pool, "scan_cookies_folder", return_value=3) as mock_scan, \
             patch.object(server.pool, "start_startup_quota_sync", return_value=True) as mock_sync:
            resp = self.app.post("/api/cookies/convert")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["scanned"], 3)
            self.assertTrue(data["sync_started"])
            mock_scan.assert_called_once()
            mock_sync.assert_called_once_with(max_workers=12, force=True)

    def test_scan_cookies_folder_case_insensitive_and_updates(self):
        """Kiem tra scan_cookies_folder ho tro .TXT/.cookie hoa thuong va cap nhat cookie khi drop lai."""
        import tempfile, shutil
        tmp_dir = tempfile.mkdtemp()
        test_email = "test_user_case_scan@gmail.com"
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute("DELETE FROM accounts WHERE LOWER(email) IN ('test_user@gmail.com', ?)", (test_email.lower(),))
        con.commit()
        con.close()

        try:
            mgr = AccountPoolManager(cookies_dir=tmp_dir)
            
            # Tao file cookie hoa .TXT
            cfile = os.path.join(tmp_dir, f"{test_email}.TXT")
            with open(cfile, "w", encoding="utf-8") as f:
                f.write("user_cookie_content_1234567890")

            added = mgr.scan_cookies_folder()
            self.assertEqual(added, 1)

            # Drop lai file voi cookie moi va status PENDING
            with open(cfile, "w", encoding="utf-8") as f:
                f.write("REFRESHED_user_cookie_content_999")

            added2 = mgr.scan_cookies_folder()
            self.assertEqual(added2, 0) # Khong tang so luong tai khoan

            # Kiem tra cookie da duoc cap nhat
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute("SELECT cookie_snippet FROM accounts WHERE LOWER(email) = ?", (test_email.lower(),))
            row = cur.fetchone()
            con.close()
            self.assertIsNotNone(row)
            self.assertIn("REFRESHED", row[0])
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute("DELETE FROM accounts WHERE LOWER(email) IN ('test_user@gmail.com', ?)", (test_email.lower(),))
            con.commit()
            con.close()
            shutil.rmtree(tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
