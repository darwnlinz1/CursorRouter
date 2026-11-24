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
    def test_master_key_persists_independently_of_storage_json(self):
        """Tokens encrypted via token_cipher decrypt cleanly using persistent master key without touching host storage.json."""
        original_plain = "secret_access_token_payload_998877"
        encrypted = token_cipher.encrypt_token(original_plain)
        self.assertTrue(encrypted.startswith("enc:v1:"))

        # Clear LRU decrypt cache to verify genuine PBKDF2 + HMAC decryption
        token_cipher.clear_decrypt_cache()
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

    def test_auto_repair_database_marks_undecryptable_oauth_expired(self):
        """auto_repair_database must detect undecryptable OAuth access_token rows and flag them EXPIRED."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_accounts.db")

            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY,
                    file_name TEXT,
                    email TEXT,
                    access_token TEXT,
                    refresh_token TEXT,
                    status TEXT,
                    display_message TEXT
                )
            """)
            # Tamper a genuine encrypted OAuth token so HMAC verification fails cleanly
            valid_enc = token_cipher.encrypt_token("eyJhbGciOiJIUzI1NiJ9.eyJ0eXBlIjoic2Vzc2lvbiJ9.valid")
            broken_enc = valid_enc[:-4] + ("AAAA" if not valid_enc.endswith("AAAA") else "BBBB")
            cur.execute(
                "INSERT INTO accounts VALUES (1, 'oauth_user1.oauth', 'user1@example.com', ?, ?, 'READY', 'OK')",
                (broken_enc, valid_enc),
            )
            cur.execute(
                "INSERT INTO accounts VALUES (2, 'oauth_user2.oauth', 'user2@example.com', ?, ?, 'READY', 'OK')",
                (valid_enc, valid_enc),
            )
            conn.commit()
            conn.close()

            repaired = token_cipher.auto_repair_database(db_path)
            self.assertEqual(repaired, 1)

            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT id, status, display_message FROM accounts ORDER BY id ASC")
            rows = cur.fetchall()
            conn.close()

            self.assertEqual(rows[0][1], "EXPIRED")
            self.assertIn("OAuth", rows[0][2])
            self.assertEqual(rows[1][1], "READY")


if __name__ == "__main__":
    unittest.main()
