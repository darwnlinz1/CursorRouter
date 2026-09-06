"""
Token Cipher & Data Encryption Module
======================================
Provides zero-dependency encryption and decryption for sensitive tokens,
session cookies, and authentication payloads stored in SQLite or cached on host.

Security Architecture:
- Uses HMAC-SHA256 and PBKDF2 with machine-salted cryptographic key derivation.
- Salt is derived from host-specific identifiers (Machine GUID, Machine ID from storage.json,
  or Windows user profile).
- Ciphertext is tagged with 'enc:v1:<salt_b64>:<nonce_b64>:<ciphertext_b64>' for integrity
  and seamless backward compatibility with legacy unencrypted tokens.
"""

import os
import sys
import hmac
import hashlib
import base64
import secrets
import logging
from typing import Optional, List

logger = logging.getLogger("cursor_manager.token_cipher")

# Persistent master key file location
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SRC_DIR)
_PRIMARY_KEY_FILE = os.path.join(_ROOT_DIR, ".cursor_master.key")
_APPDATA_DIR = os.getenv("APPDATA")
_FALLBACK_KEY_FILE = (
    os.path.join(_APPDATA_DIR, "CursorManager", ".master.key")
    if _APPDATA_DIR
    else None
)


def _get_stable_host_seed() -> bytes:
    """
    Derive a deterministic host seed based strictly on permanent machine attributes:
    Windows MachineGuid, OS USERNAME, and COMPUTERNAME.
    Does NOT include volatile attributes like Cursor's storage.json telemetry.machineId.
    """
    seed_parts = []
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                if guid:
                    seed_parts.append(str(guid))
        except Exception:
            pass

    seed_parts.append(os.getenv("USERNAME", "cursor_user"))
    seed_parts.append(os.getenv("COMPUTERNAME", "cursor_host"))
    combined = "||".join(seed_parts).encode("utf-8")
    return hashlib.sha256(combined).digest()


def _get_legacy_storage_seed() -> Optional[bytes]:
    """Derive legacy seed that included storage.json telemetry.machineId for backwards compatibility."""
    appdata = os.getenv("APPDATA")
    if not appdata:
        return None
    s_path = os.path.join(appdata, "Cursor", "User", "globalStorage", "storage.json")
    if not os.path.exists(s_path):
        return None
    try:
        import json
        with open(s_path, "r", encoding="utf-8") as f:
            s_data = json.load(f)
            mid = s_data.get("telemetry.machineId")
            if not mid:
                return None

        seed_parts = []
        if sys.platform == "win32":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                    guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                    if guid:
                        seed_parts.append(str(guid))
            except Exception:
                pass
        seed_parts.append(str(mid))
        seed_parts.append(os.getenv("USERNAME", "cursor_user"))
        seed_parts.append(os.getenv("COMPUTERNAME", "cursor_host"))
        combined = "||".join(seed_parts).encode("utf-8")
        return hashlib.sha256(combined).digest()
    except Exception:
        return None


def _get_persistent_master_key() -> bytes:
    """
    Retrieve or initialize the persistent master key for this machine.
    Ensures that once established, the key remains stable indefinitely,
    even across hardware ID spoofing, restarts, or profile changes.
    """
    # 1. Try reading from project key file
    for kf in (_PRIMARY_KEY_FILE, _FALLBACK_KEY_FILE):
        if kf and os.path.exists(kf):
            try:
                with open(kf, "rb") as f:
                    data = f.read().strip()
                if len(data) == 64:  # Hex-encoded
                    return bytes.fromhex(data.decode("ascii"))
                elif len(data) == 32:  # Raw bytes
                    return data
            except Exception:
                pass

    # 2. Key file does not exist yet. Initialize using legacy seed (if available) or stable host seed
    initial_key = _get_legacy_storage_seed() or _get_stable_host_seed()

    # Save to primary key file for lifetime persistence
    try:
        with open(_PRIMARY_KEY_FILE, "wb") as f:
            f.write(initial_key.hex().encode("ascii"))
    except Exception:
        pass

    return initial_key


# Master key singleton
_MASTER_KEY = _get_persistent_master_key()


def _get_candidate_keys() -> List[bytes]:
    """Return all candidate decryption keys in priority order."""
    keys = [_MASTER_KEY]
    stable = _get_stable_host_seed()
    if stable not in keys:
        keys.append(stable)
    legacy = _get_legacy_storage_seed()
    if legacy and legacy not in keys:
        keys.append(legacy)
    return keys


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Deterministic stream cipher generator using iterated HMAC-SHA256."""
    stream = bytearray()
    counter = 0
    while len(stream) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        stream.extend(block)
        counter += 1
    return bytes(stream[:length])


def encrypt_token(plaintext: Optional[str]) -> Optional[str]:
    """
    Encrypt sensitive plaintext (access token, refresh token, cookie).
    Returns ciphertext prefixed with 'enc:v1:' or None if input is empty.
    If already encrypted, returns original ciphertext.
    """
    if not plaintext or not isinstance(plaintext, str):
        return plaintext
    
    if plaintext.startswith("enc:v1:"):
        return plaintext  # Already encrypted

    salt = secrets.token_bytes(8)
    nonce = secrets.token_bytes(12)
    derived_key = hashlib.pbkdf2_hmac("sha256", _MASTER_KEY, salt, 10000, 32)

    data_bytes = plaintext.encode("utf-8")
    ks = _keystream(derived_key, nonce, len(data_bytes))
    cipher_bytes = bytes(a ^ b for a, b in zip(data_bytes, ks))

    # HMAC tag for integrity
    tag = hmac.new(derived_key, nonce + cipher_bytes, hashlib.sha256).digest()[:16]

    payload = salt + nonce + tag + cipher_bytes
    encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    return f"enc:v1:{encoded}"


def decrypt_token(ciphertext: Optional[str]) -> Optional[str]:
    """
    Decrypt token ciphertext. If token is unencrypted (legacy), returns it as-is.
    Supports candidate fallback keys for seamless rotation and recovery without console spam.
    """
    if not ciphertext or not isinstance(ciphertext, str):
        return ciphertext

    if not ciphertext.startswith("enc:v1:"):
        return ciphertext  # Legacy plaintext token, return directly

    try:
        raw_b64 = ciphertext[7:]
        payload = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
        if len(payload) < 8 + 12 + 16:
            return ciphertext

        salt = payload[:8]
        nonce = payload[8:20]
        expected_tag = payload[20:36]
        cipher_bytes = payload[36:]

        # Try master key followed by candidate fallback keys
        for key in _get_candidate_keys():
            derived_key = hashlib.pbkdf2_hmac("sha256", key, salt, 10000, 32)
            actual_tag = hmac.new(derived_key, nonce + cipher_bytes, hashlib.sha256).digest()[:16]

            if hmac.compare_digest(expected_tag, actual_tag):
                ks = _keystream(derived_key, nonce, len(cipher_bytes))
                decrypted_bytes = bytes(a ^ b for a, b in zip(cipher_bytes, ks))
                return decrypted_bytes.decode("utf-8")

        # No candidate key matched HMAC tag
        logger.debug("Token decryption: HMAC tag mismatch across all candidate keys")
        return None
    except Exception as e:
        logger.debug(f"Token decryption error: {e}")
        return None


def mask_token(token: Optional[str]) -> str:
    """
    Create a clean, safe masked token representation (e.g. eyJh...1234).
    Accepts both plaintext and encrypted tokens.
    """
    if not token or not isinstance(token, str):
        return "None"
    
    plain = decrypt_token(token) if token.startswith("enc:v1:") else token
    if not plain or len(plain) <= 10:
        return "••••••••"
    return f"{plain[:6]}...{plain[-4:]}"


def auto_repair_database(db_path: Optional[str] = None, cookies_dir: Optional[str] = None) -> int:
    """
    Autonomous self-healing scanner: verifies all encrypted records in sqlite database,
    and repairs any undecryptable cookies using the original files in Cookies/ folder.
    Returns the number of repaired rows.
    """
    import sqlite3

    if not db_path:
        db_path = os.path.join(_ROOT_DIR, "cursor_accounts.db")
    if not cookies_dir:
        cookies_dir = os.path.join(_ROOT_DIR, "Cookies")

    if not os.path.exists(db_path) or not os.path.exists(cookies_dir):
        return 0

    repaired = 0
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT id, file_name, email, cookie_full FROM accounts WHERE cookie_full LIKE 'enc:v1:%'")
        rows = cur.fetchall()

        for acc_id, file_name, email, cookie_val in rows:
            if decrypt_token(cookie_val) is None:
                # Find matching cookie file
                target_file = None
                if file_name and os.path.exists(os.path.join(cookies_dir, file_name)):
                    target_file = os.path.join(cookies_dir, file_name)
                elif email and os.path.exists(os.path.join(cookies_dir, f"{email}.txt")):
                    target_file = os.path.join(cookies_dir, f"{email}.txt")

                if target_file:
                    try:
                        with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
                            raw_cookie = f.read().strip()
                        if raw_cookie:
                            new_encrypted = encrypt_token(raw_cookie)
                            cur.execute("UPDATE accounts SET cookie_full = ? WHERE id = ?", (new_encrypted, acc_id))
                            repaired += 1
                    except Exception:
                        pass

        if repaired > 0:
            conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"auto_repair_database exception: {e}")

    return repaired

