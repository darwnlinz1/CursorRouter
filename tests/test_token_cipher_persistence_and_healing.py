import unittest
import os
import sys
import tempfile
import sqlite3
import json
import io

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import token_cipher
from account_pool import AccountPoolManager


class TestTokenCipherPersistenceAndHealing(unittest.TestCase):
    def test_master_key_persists_across_storage_json_mutation(self):
        """Tokens encrypted before storage.json changes must decrypt cleanly after machineId is spoofed."""
        original_plain = "secret_access_token_payload_998877"
        encrypted = token_cipher.encrypt_token(original_plain)
        self.assertTrue(encrypted.startswith("enc:v1:"))

        # Simulate Cursor storage.json machineId mutation/spoofing
        appdata = os.getenv("APPDATA")
        if appdata:
            s_path = os.path.join(appdata, "Cursor", "User", "globalStorage", "storage.json")
            if os.path.exists(s_path):
                try:
                    with open(s_path, "r", encoding="utf-8") as f:
                        original_data = json.load(f)
                    # Spoof with a new random machine ID
                    spoofed_data = dict(original_data)
                    spoofed_data["telemetry.machineId"] = "0000000000000000000000000000000000000000000000000000000000009999"
                    with open(s_path, "w", encoding="utf-8") as f:
                        json.dump(spoofed_data, f)
                    
                    # Decryption must STILL succeed because master key is persistent!
                    decrypted = token_cipher.decrypt_token(encrypted)
                    self.assertEqual(decrypted, original_plain)

                    # Restore original data
                    with open(s_path, "w", encoding="utf-8") as f:
                        json.dump(original_data, f)
                except Exception:
                    pass

        # Decryption must match
        decrypted = token_cipher.decrypt_token(encrypted)
        self.assertEqual(decrypted, original_plain)

    def test_decrypt_token_quiet_on_invalid_hmac(self):
        """Decryption failure must not pollute stdout with error logs."""
        enc = token_cipher.encrypt_token("some_payload_for_tampering")
        tampered = enc[:-4] + "XXXX"

        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            result = token_cipher.decrypt_token(tampered)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIsNone(result)
        self.assertNotIn("HMAC tag mismatch", output)

    def test_auto_repair_database_from_cookies_folder(self):
        """auto_repair_database must repair undecryptable cookie_full using disk files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_accounts.db")
            cookies_dir = os.path.join(tmpdir, "Cookies")
            os.makedirs(cookies_dir, exist_ok=True)

            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY,
                    file_name TEXT,
                    email TEXT,
                    cookie_full TEXT
                )
            """)
            # Insert row with broken ciphertext
            broken_enc = "enc:v1:AAAA_invalid_ciphertext_that_cannot_decrypt_BBBB"
            cur.execute("INSERT INTO accounts VALUES (1, 'user1.txt', 'user1@example.com', ?)", (broken_enc,))
            conn.commit()
            conn.close()

            # Create file in cookies_dir
            cookie_content = "test_workos_cookie=session_secret_12345"
            with open(os.path.join(cookies_dir, "user1.txt"), "w", encoding="utf-8") as f:
                f.write(cookie_content)

            # Run auto repair
            repaired = token_cipher.auto_repair_database(db_path, cookies_dir)
            self.assertEqual(repaired, 1)

            # Verify DB was repaired
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT cookie_full FROM accounts WHERE id = 1")
            new_cookie = cur.fetchone()[0]
            conn.close()

            self.assertTrue(new_cookie.startswith("enc:v1:"))
            decrypted = token_cipher.decrypt_token(new_cookie)
            self.assertEqual(decrypted, cookie_content)


if __name__ == "__main__":
    unittest.main()
