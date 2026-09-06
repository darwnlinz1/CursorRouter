import os
import sys
import time
import json
import sqlite3
import unittest
from unittest.mock import patch, MagicMock

import server
from account_pool import AccountPoolManager, DB_FILE, QUOTA_EXHAUSTION_THRESHOLD
from cursor_storage import CursorStorageManager

class TestRealtimeAndQuotaPersistence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.app.config['TESTING'] = True
        cls.client = server.app.test_client()
        cls.pool = AccountPoolManager()

    def setUp(self):
        self.pool.stop_auto_rotate_watcher()
        server.pool.stop_auto_rotate_watcher()

    def tearDown(self):
        self.pool.stop_auto_rotate_watcher()
        server.pool.stop_auto_rotate_watcher()

    def test_01_api_time_endpoint(self):
        res = self.client.get('/api/time')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertIn('server_time', data)
        self.assertIn('server_time_iso', data)
        self.assertIn('uptime_seconds', data)
        self.assertIsInstance(data['server_time'], int)
        self.assertGreater(data['server_time'], 1700000000)
        self.assertGreaterEqual(data['uptime_seconds'], 0)

    def test_02_api_status_realtime_fields(self):
        res = self.client.get('/api/status')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn('server_time', data)
        self.assertIn('server_time_iso', data)
        self.assertIn('uptime_seconds', data)
        self.assertIn('last_sync', data)
        self.assertIn('usagePercent', data)
        self.assertIn('usage_percent', data)
        self.assertIn('totalSpend', data)
        self.assertIn('total_spend', data)
        self.assertIsInstance(data['server_time'], int)
        self.assertIsInstance(data['uptime_seconds'], int)

    def test_03_quota_snapshots_table_and_recording(self):
        test_email = 'snapshot_test_user@example.com'
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (test_email,))
        cur.execute('DELETE FROM accounts WHERE email = ?', (test_email,))
        cur.execute('''
            INSERT INTO accounts (file_name, email, usage_percent, total_spend, status)
            VALUES ('snap_test.txt', ?, 15.0, 30.0, 'READY')
        ''', (test_email,))
        acc_id = cur.lastrowid
        con.commit()
        con.close()

        try:
            snap_id_1 = self.pool.record_quota_snapshot(
                account_id=acc_id,
                email=test_email,
                usage_percent=25.5,
                total_spend=51.0,
                display_message='Used 25.5%',
                status='READY',
                source='test'
            )
            self.assertGreater(snap_id_1, 0)

            latest_snap = self.pool.get_latest_quota_snapshot(account_id=acc_id)
            self.assertIsNotNone(latest_snap)
            self.assertEqual(latest_snap['email'], test_email)
            self.assertAlmostEqual(latest_snap['usage_percent'], 25.5)
            self.assertAlmostEqual(latest_snap['total_spend'], 51.0)
            self.assertEqual(latest_snap['status'], 'READY')

            persisted = self.pool.get_persisted_account_quota(account_id=acc_id)
            self.assertIsNotNone(persisted)
            self.assertAlmostEqual(persisted['usage_percent'], 25.5)
            self.assertAlmostEqual(persisted['total_spend'], 51.0)

            res = self.client.get(f'/api/quota/snapshots?email={test_email}')
            self.assertEqual(res.status_code, 200)
            snaps = res.get_json().get('snapshots', [])
            self.assertGreaterEqual(len(snaps), 1)
            self.assertEqual(snaps[0]['email'], test_email)
            self.assertAlmostEqual(snaps[0]['usage_percent'], 25.5)
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (test_email,))
            cur.execute('DELETE FROM accounts WHERE email = ?', (test_email,))
            con.commit()
            con.close()

    def test_04_f5_reload_retains_persisted_quota_without_resetting_to_zero(self):
        active_email = 'f5_persistence_test@example.com'
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute('DELETE FROM accounts WHERE email = ?', (active_email,))
        cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (active_email,))
        cur.execute('''
            INSERT INTO accounts (file_name, email, access_token, usage_percent, total_spend, display_message, status, last_checked)
            VALUES ('f5_test.txt', ?, 'f5_dummy_token_abc', 37.5, 75.0, '37.5% used', 'READY', ?)
        ''', (active_email, int(time.time())))
        acc_id = cur.lastrowid
        con.commit()
        con.close()

        try:
            self.pool.record_quota_snapshot(
                account_id=acc_id,
                email=active_email,
                usage_percent=37.5,
                total_spend=75.0,
                display_message='37.5% used',
                status='READY',
                source='f5_init'
            )

            mock_active = {
                'email': active_email,
                'displayName': 'F5 Tester',
                'authId': 'test_auth_f5',
                'access_token': 'f5_dummy_token_abc',
                'has_token': True,
                'is_expired': False,
                'membership_type': 'free'
            }

            with patch.object(self.pool.storage, 'get_active_account', return_value=mock_active), \
                 patch.object(self.pool.storage, 'fetch_profile_from_api', return_value=None):
                synced = self.pool.sync_active_from_cursor()
                self.assertIsNotNone(synced)
                self.assertEqual(synced['email'], active_email)
                self.assertAlmostEqual(synced['usagePercent'], 37.5)
                self.assertAlmostEqual(synced['totalSpend'], 75.0)

            fake_failed_profile = {
                'email': active_email,
                'displayName': 'F5 Tester',
                'authId': 'test_auth_f5',
                'usagePercent': 0,
                'totalSpend': 0,
                'usage_fetched': False,
                'http_status': 429
            }
            with patch.object(self.pool.storage, 'get_active_account', return_value=mock_active), \
                 patch.object(self.pool.storage, 'fetch_profile_from_api', return_value=fake_failed_profile):
                synced = self.pool.sync_active_from_cursor()
                self.assertIsNotNone(synced)
                self.assertAlmostEqual(synced['usagePercent'], 37.5)
                self.assertAlmostEqual(synced['totalSpend'], 75.0)

            with patch.object(server.pool.storage, 'get_active_account', return_value=mock_active), \
                 patch.object(server.pool.storage, 'fetch_profile_from_api', return_value=None):
                res = self.client.get('/api/status')
                self.assertEqual(res.status_code, 200)
                status_data = res.get_json()
                self.assertEqual(status_data['email'], active_email)
                self.assertAlmostEqual(status_data['usagePercent'], 37.5)
                self.assertAlmostEqual(status_data['totalSpend'], 75.0)

            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('SELECT usage_percent, total_spend FROM accounts WHERE email = ?', (active_email,))
            row = cur.fetchone()
            con.close()
            self.assertIsNotNone(row)
            self.assertAlmostEqual(row[0], 37.5)
            self.assertAlmostEqual(row[1], 75.0)

        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('DELETE FROM accounts WHERE email = ?', (active_email,))
            cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (active_email,))
            con.commit()
            con.close()

    def test_05_quota_persists_across_new_pool_instance_and_db_reload(self):
        persist_email = 'reboot_persistence_test@example.com'
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute('DELETE FROM accounts WHERE email = ?', (persist_email,))
        cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (persist_email,))
        cur.execute('''
            INSERT INTO accounts (file_name, email, access_token, usage_percent, total_spend, status, last_checked)
            VALUES ('reboot_test.txt', ?, 'tok_reboot_123', 44.0, 88.0, 'HIGH_USAGE', ?)
        ''', (persist_email, int(time.time())))
        con.commit()
        con.close()

        try:
            fresh_pool = AccountPoolManager()
            persisted = fresh_pool.get_persisted_account_quota(email=persist_email)
            self.assertIsNotNone(persisted)
            self.assertAlmostEqual(persisted['usage_percent'], 44.0)
            self.assertAlmostEqual(persisted['total_spend'], 88.0)
            self.assertEqual(persisted['status'], 'HIGH_USAGE')
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('DELETE FROM accounts WHERE email = ?', (persist_email,))
            cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (persist_email,))
            con.commit()
            con.close()

    def test_06_auto_switch_rejects_exhausted_snapshots_and_picks_valid_quota(self):
        email_exhausted = 'bad_snapshot_user@example.com'
        email_good = 'good_quota_user@example.com'

        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute('DELETE FROM accounts WHERE email IN (?, ?)', (email_exhausted, email_good))
        cur.execute('DELETE FROM quota_snapshots WHERE email IN (?, ?)', (email_exhausted, email_good))
        
        cur.execute('''
            INSERT INTO accounts (file_name, email, access_token, usage_percent, total_spend, status)
            VALUES ('acc_a.txt', ?, 'tok_a', -20.0, 0.0, 'READY')
        ''', (email_exhausted,))
        id_a = cur.lastrowid

        cur.execute('''
            INSERT INTO accounts (file_name, email, access_token, usage_percent, total_spend, status)
            VALUES ('acc_b.txt', ?, 'tok_b', -10.0, 20.0, 'READY')
        ''', (email_good,))
        id_b = cur.lastrowid
        con.commit()
        con.close()

        try:
            self.pool.record_quota_snapshot(
                account_id=id_a,
                email=email_exhausted,
                usage_percent=52.0,
                total_spend=104.0,
                display_message='Limit exceeded',
                status='EXHAUSTED',
                source='realtime_check'
            )

            with patch.object(self.pool.storage, 'get_active_account', return_value={'email': 'current_active@example.com'}), \
                 patch.object(self.pool, 'switch_to_account', return_value=True):
                
                chosen = self.pool.auto_switch_best_account(notify=False)
                self.assertIsNotNone(chosen)
                self.assertEqual(chosen['email'], email_good)
                self.assertAlmostEqual(chosen['usage_percent'], -10.0)

        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('DELETE FROM accounts WHERE email IN (?, ?)', (email_exhausted, email_good))
            cur.execute('DELETE FROM quota_snapshots WHERE email IN (?, ?)', (email_exhausted, email_good))
            con.commit()
            con.close()

        tmpl_path = os.path.join(os.path.dirname(__file__), '..', 'templates', 'index.html')
        if not os.path.exists(tmpl_path):
            tmpl_path = os.path.join(os.path.dirname(__file__), 'templates', 'index.html')
        with open(tmpl_path, 'r', encoding='utf-8') as f:
            html = f.read()

        self.assertIn('id="live-clock"', html)
        self.assertIn('id="live-date"', html)
        self.assertIn('id="live-sync-ticker"', html)
        self.assertIn('id="live-uptime-ticker"', html)
        self.assertIn('id="active-last-sync"', html)
        self.assertIn('id="active-quota-text"', html)
        self.assertIn('updateRealtimeClock()', html)
        self.assertIn('updateSyncTicker()', html)
        self.assertIn('updateUptimeTicker()', html)
        self.assertIn('restoreCachedActiveProfile()', html)
        self.assertIn('applyActiveProfileToUI', html)
        self.assertIn("localStorage.getItem('cursor_active_profile')", html)
        self.assertIn("localStorage.setItem('cursor_active_profile'", html)
        self.assertIn("localStorage.getItem('cursor_last_sync_time')", html)
        self.assertIn('setInterval(updateRealtimeClock, 1000)', html)
        self.assertIn('Zero-delay cached paint', html)

    def test_08_get_latest_quota_snapshot_finds_newest_snapshot_across_id_and_email(self):
        test_email = 'cross_id_email_snap@example.com'
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (test_email,))
        t_now = int(time.time())
        # Snap 1 recorded at t_now - 100 with account_id=9999
        cur.execute('''
            INSERT INTO quota_snapshots (account_id, email, usage_percent, total_spend, status, recorded_at)
            VALUES (9999, ?, 12.0, 24.0, 'READY', ?)
        ''', (test_email, t_now - 100))
        # Snap 2 recorded at t_now with account_id=NULL (e.g. from background watcher)
        cur.execute('''
            INSERT INTO quota_snapshots (account_id, email, usage_percent, total_spend, status, recorded_at)
            VALUES (NULL, ?, 48.5, 97.0, 'READY', ?)
        ''', (test_email, t_now))
        con.commit()
        con.close()

        try:
            # Must find the latest snapshot (48.5%) even though account_id was passed
            latest = self.pool.get_latest_quota_snapshot(account_id=9999, email=test_email)
            self.assertIsNotNone(latest)
            self.assertAlmostEqual(latest['usage_percent'], 48.5)
            self.assertAlmostEqual(latest['total_spend'], 97.0)

            # Persisted quota retrieval must also reflect 48.5%
            persisted = self.pool.get_persisted_account_quota(account_id=9999, email=test_email)
            self.assertIsNotNone(persisted)
            self.assertAlmostEqual(persisted['usage_percent'], 48.5)
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (test_email,))
            con.commit()
            con.close()

    def test_09_auto_switch_orders_by_true_persisted_quota_and_rejects_stale_zero_usage(self):
        email_stale_low = 'stale_low_snapshot_high@example.com'
        email_real_low = 'real_low_usage@example.com'

        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute('DELETE FROM accounts WHERE email IN (?, ?)', (email_stale_low, email_real_low))
        cur.execute('DELETE FROM quota_snapshots WHERE email IN (?, ?)', (email_stale_low, email_real_low))

        # Stale accounts table has -90.0% for email_stale_low and -80.0% for email_real_low
        cur.execute('''
            INSERT INTO accounts (file_name, email, access_token, usage_percent, total_spend, status)
            VALUES ('stale.txt', ?, 'tok_stale', -90.0, 0.0, 'READY')
        ''', (email_stale_low,))
        id_stale = cur.lastrowid

        cur.execute('''
            INSERT INTO accounts (file_name, email, access_token, usage_percent, total_spend, status)
            VALUES ('real.txt', ?, 'tok_real', -80.0, 20.0, 'READY')
        ''', (email_real_low,))
        id_real = cur.lastrowid
        con.commit()
        con.close()

        try:
            # Record a snapshot showing email_stale_low actually has 45.0% usage!
            self.pool.record_quota_snapshot(
                account_id=id_stale,
                email=email_stale_low,
                usage_percent=45.0,
                total_spend=90.0,
                status='READY',
                source='force'
            )
            # Re-set accounts table usage_percent back to -90.0 to simulate stale DB accounts row
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            con.cursor().execute('UPDATE accounts SET usage_percent = -90.0 WHERE id = ?', (id_stale,))
            con.commit()
            con.close()

            with patch.object(self.pool.storage, 'get_active_account', return_value={'email': 'active_user@example.com'}), \
                 patch.object(self.pool, 'switch_to_account', return_value=True):
                chosen = self.pool.auto_switch_best_account(notify=False)
                self.assertIsNotNone(chosen)
                # Must choose email_real_low (-80%) over email_stale_low (which has persisted 45%)
                self.assertEqual(chosen['email'], email_real_low)

        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('DELETE FROM accounts WHERE email IN (?, ?)', (email_stale_low, email_real_low))
            cur.execute('DELETE FROM quota_snapshots WHERE email IN (?, ?)', (email_stale_low, email_real_low))
            con.commit()
            con.close()

    def test_10_refresh_account_quota_preserves_persisted_quota_on_api_error(self):
        refresh_email = 'refresh_preservation_test@example.com'
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute('DELETE FROM accounts WHERE email = ?', (refresh_email,))
        cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (refresh_email,))
        cur.execute('''
            INSERT INTO accounts (file_name, email, access_token, cookie_full, usage_percent, total_spend, status, last_checked)
            VALUES ('refresh_test.txt', ?, 'tok_ref_abc', 'dummy_cookie', 38.0, 76.0, 'READY', ?)
        ''', (refresh_email, int(time.time())))
        acc_id = cur.lastrowid
        con.commit()
        con.close()

        try:
            self.pool.record_quota_snapshot(
                account_id=acc_id,
                email=refresh_email,
                usage_percent=38.0,
                total_spend=76.0,
                status='READY',
                source='force'
            )

            # Mock API returning GetMe (email) but GetCurrentPeriodUsage failed (usage_fetched: False)
            fake_failed_profile = {
                'email': refresh_email,
                'displayName': 'Preserved Tester',
                'authId': 'test_auth_ref',
                'usagePercent': 0,
                'totalSpend': 0,
                'usage_fetched': False,
                'http_status': 429
            }
            with patch.object(self.pool.storage, 'fetch_profile_from_api', return_value=fake_failed_profile):
                refreshed = self.pool.refresh_account_quota(acc_id)
                self.assertIsNotNone(refreshed)
                # Quota must NOT be wiped to 0
                self.assertAlmostEqual(refreshed['usagePercent'], 38.0)
                self.assertAlmostEqual(refreshed['totalSpend'], 76.0)

            # Database row must also retain 38.0%
            persisted = self.pool.get_persisted_account_quota(account_id=acc_id)
            self.assertIsNotNone(persisted)
            self.assertAlmostEqual(persisted['usage_percent'], 38.0)
            self.assertAlmostEqual(persisted['total_spend'], 76.0)
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('DELETE FROM accounts WHERE email = ?', (refresh_email,))
            cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (refresh_email,))
            con.commit()
            con.close()

    def test_11_snapshot_deduplication_prevents_db_bloat(self):
        dedup_email = 'dedup_test@example.com'
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (dedup_email,))
        con.commit()
        con.close()

        try:
            # First recording
            snap_1 = self.pool.record_quota_snapshot(
                account_id=9876,
                email=dedup_email,
                usage_percent=20.0,
                total_spend=40.0,
                status='READY',
                source='sync'
            )
            # Immediate second recording with exact same values (should deduplicate)
            snap_2 = self.pool.record_quota_snapshot(
                account_id=9876,
                email=dedup_email,
                usage_percent=20.0,
                total_spend=40.0,
                status='READY',
                source='sync'
            )
            self.assertEqual(snap_1, snap_2)

            # Check snapshot count in DB: exactly 1 row
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('SELECT count(*) FROM quota_snapshots WHERE email = ?', (dedup_email,))
            count = cur.fetchone()[0]
            con.close()
            self.assertEqual(count, 1)

            # Now record with changed usage: must insert a new snapshot
            snap_3 = self.pool.record_quota_snapshot(
                account_id=9876,
                email=dedup_email,
                usage_percent=25.0,
                total_spend=50.0,
                status='READY',
                source='sync'
            )
            self.assertNotEqual(snap_1, snap_3)

            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('SELECT count(*) FROM quota_snapshots WHERE email = ?', (dedup_email,))
            count = cur.fetchone()[0]
            con.close()
            self.assertEqual(count, 2)
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute('DELETE FROM quota_snapshots WHERE email = ?', (dedup_email,))
            con.commit()
            con.close()

if __name__ == '__main__':
    unittest.main()

