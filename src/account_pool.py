import os
import sqlite3
import time
import json
from typing import List, Dict, Any, Optional
from pkce_auth import CursorAuthClient, parse_cookie_from_file
from cursor_storage import CursorStorageManager, decode_jwt_payload
from chat_lock_detector import is_chat_locked
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import requests
from token_cipher import encrypt_token, decrypt_token, mask_token, auto_repair_database

# Lock dung chung de tranh xung dot khi 20 luong dong thoi ghi vao SQLite
DB_LOCK = threading.Lock()
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_FILE = os.getenv("CURSOR_ACCOUNTS_DB", os.path.join(ROOT_DIR, "cursor_accounts.db"))

# Nguong khoa tai khoan cua Cursor: Tai khoan Free co the su dung tiep tuc den 100% (50% chi la moc soft limit 5 gio).
# Do do nguong EXHAUSTED dung nhat la 100.0% hoac khi bi chan chat that su.
QUOTA_EXHAUSTION_THRESHOLD = float(os.getenv("CURSOR_QUOTA_THRESHOLD", "100.0"))
QUOTA_WARNING_THRESHOLD = float(os.getenv("CURSOR_QUOTA_WARNING_THRESHOLD", "50.0"))

class AccountPoolManager:
    QUOTA_EXHAUSTION_THRESHOLD = QUOTA_EXHAUSTION_THRESHOLD
    QUOTA_WARNING_THRESHOLD = QUOTA_WARNING_THRESHOLD
    TIER1_THRESHOLD = 50.0
    TIER2_THRESHOLD = 100.0
    BLACKLIST_COOLDOWN_SECONDS = 7 * 86400  # 7-Day Blacklist Cooldown (604,800s)

    def __init__(self, cookies_dir: str = "Cookies"):
        if not os.path.isabs(cookies_dir):
            self.cookies_dir = os.path.abspath(os.path.join(ROOT_DIR, cookies_dir))
        else:
            self.cookies_dir = os.path.abspath(cookies_dir)
        os.makedirs(self.cookies_dir, exist_ok=True)
        self.storage = CursorStorageManager()
        self.auth_client = CursorAuthClient()
        self.manual_active_account_id: Optional[int] = None
        self.last_manual_switch_time: float = 0.0
        self._init_db()
        # Autonomous self-healing for encrypted records using Cookies directory
        auto_repair_database(DB_FILE, self.cookies_dir)


    def _init_db(self):
        """Khoi tao bang luu tru tai khoan va quota snapshots neu chua ton tai va chuan hoa trang thai."""
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        try:
            cur = con.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_name TEXT UNIQUE,
                    email TEXT,
                    display_name TEXT,
                    auth_id TEXT,
                    cookie_snippet TEXT,
                    cookie_full TEXT,
                    access_token TEXT,
                    refresh_token TEXT,
                    usage_percent REAL DEFAULT 0.0,
                    total_spend REAL DEFAULT 0.0,
                    display_message TEXT,
                    status TEXT DEFAULT 'PENDING',
                    last_checked INTEGER DEFAULT 0,
                    config_profile TEXT DEFAULT 'default',
                    cooldown_until INTEGER DEFAULT 0,
                    hardware_fingerprint TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quota_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    email TEXT,
                    usage_percent REAL DEFAULT 0.0,
                    total_spend REAL DEFAULT 0.0,
                    display_message TEXT,
                    status TEXT,
                    source TEXT DEFAULT 'sync',
                    recorded_at INTEGER DEFAULT 0
                )
            """)
            # Ensure config_profile, cooldown_until and hardware_fingerprint columns exist in accounts table
            cur.execute("PRAGMA table_info(accounts);")
            cols = [r[1] for r in cur.fetchall()]
            if "config_profile" not in cols:
                try:
                    cur.execute("ALTER TABLE accounts ADD COLUMN config_profile TEXT DEFAULT 'default';")
                except Exception:
                    pass
            if "cooldown_until" not in cols:
                try:
                    cur.execute("ALTER TABLE accounts ADD COLUMN cooldown_until INTEGER DEFAULT 0;")
                except Exception:
                    pass
            if "hardware_fingerprint" not in cols:
                try:
                    cur.execute("ALTER TABLE accounts ADD COLUMN hardware_fingerprint TEXT;")
                except Exception:
                    pass

            cur.execute("CREATE INDEX IF NOT EXISTS idx_quota_snapshots_acc ON quota_snapshots(account_id, recorded_at DESC);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_quota_snapshots_email ON quota_snapshots(email, recorded_at DESC);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_accounts_cooldown ON accounts(cooldown_until);")

            # Dong bo trang thai EXHAUSTED cho toan bo tai khoan co usage_percent >= QUOTA_EXHAUSTION_THRESHOLD (100.0%)
            cur.execute("""
                UPDATE accounts 
                SET status = 'EXHAUSTED' 
                WHERE usage_percent >= ? AND status NOT IN ('EXPIRED')
            """, (QUOTA_EXHAUSTION_THRESHOLD,))

            # Hoan nguyen trang thai READY/HIGH_USAGE cho tai khoan co quota hop le neu bi danh dau nham thanh EXHAUSTED hoac con vuong cooldown
            cur.execute("""
                UPDATE accounts 
                SET status = 'READY', cooldown_until = 0 
                WHERE usage_percent < ? AND (status = 'EXHAUSTED' OR (cooldown_until IS NOT NULL AND cooldown_until > 0))
            """, (QUOTA_WARNING_THRESHOLD,))
            cur.execute("""
                UPDATE accounts 
                SET status = 'HIGH_USAGE', cooldown_until = 0 
                WHERE usage_percent >= ? AND usage_percent < ? AND (status = 'EXHAUSTED' OR (cooldown_until IS NOT NULL AND cooldown_until > 0))
            """, (QUOTA_WARNING_THRESHOLD, QUOTA_EXHAUSTION_THRESHOLD))

            # Nhan tra tai khoan blacklist 7 ngay da het han cooldown (cooldown_until <= now_ts)
            now_ts = int(time.time())
            cur.execute("""
                UPDATE accounts 
                SET status = CASE WHEN usage_percent >= ? THEN 'HIGH_USAGE' ELSE 'READY' END,
                    cooldown_until = 0
                WHERE cooldown_until > 0 AND cooldown_until <= ?
            """, (QUOTA_WARNING_THRESHOLD, now_ts))
            con.commit()
        finally:
            con.close()

    def _heal_cookie_from_disk(self, acc_id: Optional[int], file_name: Optional[str], email: Optional[str]) -> Optional[str]:
        """Tự động khôi phục và re-encrypt cookie từ file gốc trong thư mục Cookies nếu giải mã gặp lỗi."""
        target_path = None
        if file_name and os.path.exists(os.path.join(self.cookies_dir, file_name)):
            target_path = os.path.join(self.cookies_dir, file_name)
        elif email and os.path.exists(os.path.join(self.cookies_dir, f"{email}.txt")):
            target_path = os.path.join(self.cookies_dir, f"{email}.txt")

        if target_path:
            try:
                with open(target_path, "r", encoding="utf-8", errors="ignore") as f:
                    raw_cookie = f.read().strip()
                if raw_cookie:
                    new_enc = encrypt_token(raw_cookie)
                    if acc_id:
                        with DB_LOCK:
                            con = sqlite3.connect(DB_FILE, timeout=30.0)
                            try:
                                cur = con.cursor()
                                cur.execute("UPDATE accounts SET cookie_full = ? WHERE id = ?", (new_enc, acc_id))
                                con.commit()
                            finally:
                                con.close()
                    return raw_cookie
            except Exception:
                pass
        return None

    def record_quota_snapshot(
        self,
        account_id: Optional[int],
        email: Optional[str],
        usage_percent: float,
        total_spend: float,
        display_message: str = "",
        status: str = "READY",
        source: str = "sync",
        cooldown_until: Optional[int] = None
    ) -> int:
        """Luu snapshot lich su quota cua tai khoan va dong bo trang thai vao database."""
        recorded_at = int(time.time())
        clean_email = (email or "").strip().lower() if email else None

        # Deduplication: Neu snapshot gan nhat cung thong so va cach day chua toi 60s, tranh flood DB
        if source not in ("force", "test"):
            last_snap = self.get_latest_quota_snapshot(account_id=account_id, email=clean_email)
            if last_snap:
                last_usage = float(last_snap.get("usage_percent", 0.0) or 0.0)
                last_spend = float(last_snap.get("total_spend", 0.0) or 0.0)
                last_status = last_snap.get("status")
                same_usage = abs(last_usage - float(usage_percent)) < 0.001
                same_spend = abs(last_spend - float(total_spend)) < 0.001
                same_status = (last_status == status)
                elapsed = recorded_at - int(last_snap.get("recorded_at", 0) or 0)
                if same_usage and same_spend and same_status and elapsed < 60:
                    with DB_LOCK:
                        con = sqlite3.connect(DB_FILE, timeout=30.0)
                        try:
                            cur = con.cursor()
                            if account_id and clean_email:
                                cur.execute("UPDATE accounts SET last_checked = ? WHERE id = ? OR LOWER(email) = LOWER(?)", (recorded_at, account_id, clean_email))
                            elif account_id:
                                cur.execute("UPDATE accounts SET last_checked = ? WHERE id = ?", (recorded_at, account_id))
                            elif clean_email:
                                cur.execute("UPDATE accounts SET last_checked = ? WHERE LOWER(email) = LOWER(?)", (recorded_at, clean_email))
                            con.commit()
                        finally:
                            con.close()
                    return last_snap["id"]

        try:
            from ai_optimizer import get_quota_predictor, DynamicCooldownManager
            get_quota_predictor().record_usage(str(account_id or email or ""), usage_percent, total_spend)
            dynamic_cd = DynamicCooldownManager.calculate_cooldown(display_message or "")
        except Exception:
            dynamic_cd = self.BLACKLIST_COOLDOWN_SECONDS

        effective_cooldown = cooldown_until if cooldown_until is not None else (
            (recorded_at + dynamic_cd) if (status == "EXHAUSTED" or usage_percent >= self.QUOTA_EXHAUSTION_THRESHOLD) else 0
        )

        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                cur = con.cursor()
                cur.execute("""
                    INSERT INTO quota_snapshots (account_id, email, usage_percent, total_spend, display_message, status, source, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (account_id, email, usage_percent, total_spend, display_message, status, source, recorded_at))
                snap_id = cur.lastrowid
                
                # Check column existence dynamically for test or legacy database compatibility
                cur.execute("PRAGMA table_info(accounts);")
                cols = [r[1] for r in cur.fetchall()]
                has_cd = "cooldown_until" in cols

                if has_cd:
                    if account_id and clean_email:
                        cur.execute("""
                            UPDATE accounts 
                            SET usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?,
                                cooldown_until = CASE WHEN ? > 0 THEN ? ELSE cooldown_until END
                            WHERE id = ? OR LOWER(email) = LOWER(?)
                        """, (usage_percent, total_spend, display_message, status, recorded_at, effective_cooldown, effective_cooldown, account_id, clean_email))
                    elif account_id:
                        cur.execute("""
                            UPDATE accounts 
                            SET usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?,
                                cooldown_until = CASE WHEN ? > 0 THEN ? ELSE cooldown_until END
                            WHERE id = ?
                        """, (usage_percent, total_spend, display_message, status, recorded_at, effective_cooldown, effective_cooldown, account_id))
                    elif clean_email:
                        cur.execute("""
                            UPDATE accounts 
                            SET usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?,
                                cooldown_until = CASE WHEN ? > 0 THEN ? ELSE cooldown_until END
                            WHERE LOWER(email) = LOWER(?)
                        """, (usage_percent, total_spend, display_message, status, recorded_at, effective_cooldown, effective_cooldown, clean_email))
                else:
                    if account_id and clean_email:
                        cur.execute("""
                            UPDATE accounts 
                            SET usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?
                            WHERE id = ? OR LOWER(email) = LOWER(?)
                        """, (usage_percent, total_spend, display_message, status, recorded_at, account_id, clean_email))
                    elif account_id:
                        cur.execute("""
                            UPDATE accounts 
                            SET usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?
                            WHERE id = ?
                        """, (usage_percent, total_spend, display_message, status, recorded_at, account_id))
                    elif clean_email:
                        cur.execute("""
                            UPDATE accounts 
                            SET usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?
                            WHERE LOWER(email) = LOWER(?)
                        """, (usage_percent, total_spend, display_message, status, recorded_at, clean_email))
                con.commit()
            finally:
                con.close()

        # Feed intelligent quota predictor with EMA burn velocity
        try:
            from ai_optimizer import get_quota_predictor
            pred_key = str(account_id if account_id else (clean_email or "unknown"))
            get_quota_predictor().record_usage(pred_key, usage_percent, total_spend)
        except Exception:
            pass

        return snap_id

    def get_latest_quota_snapshot(
        self,
        account_id: Optional[int] = None,
        email: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Lay ban ghi quota snapshot gan nhat cho account_id hoac email (tim kiem ca 2 de khong bo sot)."""
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        try:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            row = None
            clean_email = (email or "").strip().lower() if email else None

            if account_id and clean_email:
                cur.execute("""
                    SELECT * FROM quota_snapshots 
                    WHERE account_id = ? OR LOWER(email) = LOWER(?) 
                    ORDER BY recorded_at DESC, id DESC LIMIT 1
                """, (account_id, clean_email))
                row = cur.fetchone()
            elif account_id:
                cur.execute("""
                    SELECT * FROM quota_snapshots 
                    WHERE account_id = ? 
                    ORDER BY recorded_at DESC, id DESC LIMIT 1
                """, (account_id,))
                row = cur.fetchone()
            elif clean_email:
                cur.execute("""
                    SELECT * FROM quota_snapshots 
                    WHERE LOWER(email) = LOWER(?) 
                    ORDER BY recorded_at DESC, id DESC LIMIT 1
                """, (clean_email,))
                row = cur.fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def get_persisted_account_quota(
        self,
        account_id: Optional[int] = None,
        email: Optional[str] = None,
        token: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Truy xuat quota da duoc persist trong DB (accounts va quota_snapshots).
        Dam bao khong bao gio tra ve 0 neu tai khoan da tung co du lieu quota ghi nhan.
        """
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        try:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            row = None
            clean_email = (email or "").strip().lower() if email else None

            if account_id:
                cur.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
                row = cur.fetchone()
            if not row and clean_email:
                cur.execute("SELECT * FROM accounts WHERE LOWER(email) = LOWER(?)", (clean_email,))
                row = cur.fetchone()
            if not row and token:
                cur.execute("SELECT * FROM accounts WHERE access_token = ?", (token,))
                row = cur.fetchone()
        finally:
            con.close()

        if row:
            d = dict(row)
            usage = d.get("usage_percent", 0.0) or 0.0
            spend = d.get("total_spend", 0.0) or 0.0
            status = d.get("status", "READY")
            msg = d.get("display_message", "")
            last_checked = d.get("last_checked", 0)

            # Luon kiem tra snapshot moi nhat
            snap = self.get_latest_quota_snapshot(account_id=d.get("id"), email=d.get("email") or clean_email)
            if snap:
                snap_usage = float(snap.get("usage_percent", 0.0) or 0.0)
                snap_spend = float(snap.get("total_spend", 0.0) or 0.0)
                snap_rec = int(snap.get("recorded_at", 0) or 0)
                # Neu accounts co usage <= 0 ma snapshot > 0, hoac snapshot moi hon/bang last_checked, hoac snap_usage cao hon:
                if (usage <= 0.0 and (snap_usage > 0.0 or snap_spend > 0.0)) or (snap_rec >= last_checked) or (snap_usage > usage):
                    usage = snap_usage
                    spend = snap_spend
                    msg = snap.get("display_message", msg)
                    status = snap.get("status", status)
                    if snap_rec > 0:
                        last_checked = snap_rec

            return {
                "id": d.get("id"),
                "email": d.get("email") or clean_email,
                "usage_percent": usage,
                "usagePercent": usage,
                "total_spend": spend,
                "totalSpend": spend,
                "display_message": msg,
                "displayMessage": msg,
                "status": status,
                "last_checked": last_checked
            }
        
        # Neu khong co row trong accounts nhung co account_id hoac email, thu tim trong quota_snapshots
        snap = self.get_latest_quota_snapshot(account_id=account_id, email=clean_email)
        if snap:
            return {
                "id": snap.get("account_id"),
                "email": snap.get("email") or clean_email,
                "usage_percent": snap.get("usage_percent", 0.0) or 0.0,
                "usagePercent": snap.get("usage_percent", 0.0) or 0.0,
                "total_spend": snap.get("total_spend", 0.0) or 0.0,
                "totalSpend": snap.get("total_spend", 0.0) or 0.0,
                "display_message": snap.get("display_message", ""),
                "displayMessage": snap.get("display_message", ""),
                "status": snap.get("status", "READY"),
                "last_checked": snap.get("recorded_at", 0)
            }
        return None

    def predict_account_exhaustion(self, account_id: Optional[int] = None, email: Optional[str] = None) -> Dict[str, Any]:
        """Du doan toc do dot quota (EMA burn velocity) va thoi gian can quota TTE cho tai khoan."""
        try:
            from ai_optimizer import get_quota_predictor
            persisted = self.get_persisted_account_quota(account_id=account_id, email=email)
            usage = float(persisted.get("usage_percent", 0.0) or 0.0) if persisted else 0.0
            key = str(account_id if account_id else (email or "unknown"))
            return get_quota_predictor().predict_exhaustion(key, usage)
        except Exception as e:
            return {"error": str(e), "should_proactively_swap": False, "is_critical": False}

    def scan_cookies_folder(self) -> int:
        """Quet thu muc Cookies va them cac file moi vao pool."""
        os.makedirs(self.cookies_dir, exist_ok=True)
        if not os.path.exists(self.cookies_dir):
            return 0

        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                cur = con.cursor()
                added_count = 0
                for f in os.listdir(self.cookies_dir):
                    f_lower = f.lower()
                    if f_lower == "archive_corrupted" or f.startswith("."):
                        continue
                    if f_lower.endswith(".txt") or f_lower.endswith(".cookie"):
                        file_path = os.path.join(self.cookies_dir, f)
                        if not os.path.isfile(file_path):
                            continue
                        
                        base_name = f[:-4] if f_lower.endswith(".txt") else (f[:-7] if f_lower.endswith(".cookie") else f)
                        tentative_email = base_name if "@" in base_name else None

                        # Kiem tra xem da co trong DB chua (theo file_name hoac email khong phan biet chu hoa/thuong)
                        cur.execute("SELECT id, access_token, status, email FROM accounts WHERE file_name = ? COLLATE NOCASE", (f,))
                        row = cur.fetchone()
                        if not row and tentative_email:
                            cur.execute("SELECT id, access_token, status, email FROM accounts WHERE LOWER(email) = LOWER(?)", (tentative_email,))
                            row = cur.fetchone()

                        if not row:
                            try:
                                cookie_val = parse_cookie_from_file(file_path)
                                if cookie_val:
                                    snippet = cookie_val[:20] + "..." if len(cookie_val) > 20 else cookie_val
                                    cur.execute("""
                                        INSERT INTO accounts (file_name, email, cookie_snippet, cookie_full, status)
                                        VALUES (?, ?, ?, ?, 'PENDING')
                                    """, (f, tentative_email, snippet, encrypt_token(cookie_val)))
                                    added_count += 1
                            except Exception as e:
                                print(f"[-] Loi doc file {f}: {e}")
                        else:
                            # Neu account da ton tai nhung chua co access_token hoac bi expired/exhausted, cap nhat lai cookie tu file
                            acc_id, acc_token, acc_status = row[0], row[1], row[2]
                            if not acc_token or acc_status in ("PENDING", "EXPIRED", "EXHAUSTED"):
                                try:
                                    cookie_val = parse_cookie_from_file(file_path)
                                    if cookie_val:
                                        snippet = cookie_val[:20] + "..." if len(cookie_val) > 20 else cookie_val
                                        cur.execute("""
                                            UPDATE accounts
                                            SET file_name = ?, cookie_full = ?, cookie_snippet = ?, status = 'PENDING'
                                            WHERE id = ?
                                        """, (f, encrypt_token(cookie_val), snippet, acc_id))
                                except Exception as e:
                                    print(f"[-] Loi cap nhat cookie {f}: {e}")

                con.commit()
            finally:
                con.close()
        return added_count

    def get_all_accounts(self) -> List[Dict[str, Any]]:
        """Lay toan bo danh sach tai khoan va danh dau tai khoan dang active trong Cursor."""
        active_acc = self.storage.get_active_account()
        active_token = active_acc.get("access_token")
        active_email = (active_acc.get("email") or "").strip().lower()
        active_auth_id = active_acc.get("authId")

        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM accounts ORDER BY usage_percent ASC, id ASC")
                rows = cur.fetchall()
            finally:
                con.close()

        # Decrypt tokens in memory for accurate matching and masked representation
        decrypted_rows = []
        for r in rows:
            d = dict(r)
            if d.get("access_token"):
                d["access_token"] = decrypt_token(d["access_token"])
            if d.get("refresh_token"):
                d["refresh_token"] = decrypt_token(d["refresh_token"])
            if d.get("cookie_full"):
                dec_c = decrypt_token(d["cookie_full"])
                if dec_c is None and d["cookie_full"].startswith("enc:v1:"):
                    dec_c = self._heal_cookie_from_disk(d.get("id"), d.get("file_name"), d.get("email"))
                d["cookie_full"] = dec_c
            tok = d.get("access_token")
            d["masked_token"] = mask_token(tok)
            decrypted_rows.append(d)

        # Tim chinh xac duy nhat 1 ID tai khoan dang active (uu tien token -> auth_id -> email)
        matched_active_id = None
        if self.manual_active_account_id:
            matched_active_id = self.manual_active_account_id
        elif active_token:
            for d in decrypted_rows:
                if d.get("access_token") == active_token:
                    matched_active_id = d["id"]
                    break
        if matched_active_id is None and active_auth_id:
            for d in decrypted_rows:
                if d.get("auth_id") and d["auth_id"] == active_auth_id:
                    matched_active_id = d["id"]
                    break
        if matched_active_id is None and active_email:
            for d in decrypted_rows:
                em = (d.get("email") or "").strip().lower()
                if em and em == active_email:
                    matched_active_id = d["id"]
                    break

        # Anti-Detect Hardware Fingerprint Profiles
        missing_fps = []
        for d in decrypted_rows:
            fp_raw = d.get("hardware_fingerprint")
            fp_obj = None
            if fp_raw:
                try:
                    fp_obj = json.loads(fp_raw)
                except Exception:
                    pass
            if not fp_obj or not isinstance(fp_obj, dict):
                try:
                    from cursor_settings import CursorSettingsManager
                    fp_obj = CursorSettingsManager().generate_fingerprint()
                    missing_fps.append((json.dumps(fp_obj), d["id"]))
                except Exception:
                    fp_obj = {}
            d["fingerprint"] = fp_obj
            d["fingerprint_id"] = fp_obj.get("fingerprint_id") or (fp_obj.get("machineId") or "")[:8] or f"FP-{d['id']:04d}"

        if missing_fps:
            with DB_LOCK:
                con_fps = sqlite3.connect(DB_FILE, timeout=30.0)
                try:
                    con_fps.executemany("UPDATE accounts SET hardware_fingerprint = ? WHERE id = ?", missing_fps)
                    con_fps.commit()
                except Exception:
                    pass
                finally:
                    con_fps.close()

        now_ts = int(time.time())
        results = []
        for d in decrypted_rows:
            d["is_active"] = (d["id"] == matched_active_id) if matched_active_id is not None else False
            c_until = int(d.get("cooldown_until") or 0)
            in_cd = (c_until > now_ts)
            d["cooldown_until"] = c_until
            d["in_cooldown"] = in_cd
            d["cooldown_remaining_seconds"] = max(0, c_until - now_ts) if in_cd else 0
            
            usage = float(d.get("usage_percent", 0.0) or 0.0)
            status = d.get("status", "READY")
            if status == "EXPIRED":
                d["tier"] = "expired"
                d["tier_name"] = "Expired"
            elif in_cd or usage >= self.QUOTA_EXHAUSTION_THRESHOLD or status == "EXHAUSTED":
                d["tier"] = "blacklist"
                d["tier_name"] = "Blacklist (7d)"
            elif usage < self.TIER1_THRESHOLD:
                d["tier"] = "tier1"
                d["tier_name"] = "Tier 1 (<50%)"
            else:
                d["tier"] = "tier2"
                d["tier_name"] = "Tier 2 (50%-99%)"

            results.append(d)
        # Luon uu tien dua tai khoan dang active len vi tri dau tien (#1 row)
        results.sort(
            key=lambda x: (
                not x.get("is_active", False),
                x.get("usage_percent", 0.0) if x.get("usage_percent") is not None else 999.0,
                x.get("id", 0)
            )
        )
        return results


    def refresh_account_quota(self, account_id: int) -> Optional[Dict[str, Any]]:
        """Kiem tra va cap nhat thong tin quota tu API cho 1 tai khoan."""
        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                cur.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
                acc = cur.fetchone()
            finally:
                con.close()

        if not acc:
            return None

        access_token = decrypt_token(acc["access_token"])
        refresh_token = decrypt_token(acc["refresh_token"])
        cookie_full = decrypt_token(acc["cookie_full"])
        if not cookie_full and acc.get("cookie_full") and str(acc["cookie_full"]).startswith("enc:v1:"):
            cookie_full = self._heal_cookie_from_disk(acc["id"], acc.get("file_name"), acc.get("email"))

        # 1. Neu chua co access_token, thuc hien PKCE exchange voi cookie
        if not access_token:
            if not cookie_full:
                return None
            tokens = self.auth_client.exchange_cookie_to_tokens(cookie_full)
            if tokens and "accessToken" in tokens:
                access_token = tokens["accessToken"]
                refresh_token = tokens.get("refreshToken")
            else:
                self._update_status(account_id, status="EXPIRED")
                return None

        # 2. Goi API lay thong tin GetMe va Quota
        profile = self.storage.fetch_profile_from_api(access_token)
        if not profile or not profile.get("email"):
            # Token co the het han, thu exchange lai neu co cookie
            if cookie_full:
                tokens = self.auth_client.exchange_cookie_to_tokens(cookie_full)
                if tokens and "accessToken" in tokens:
                    access_token = tokens["accessToken"]
                    refresh_token = tokens.get("refreshToken")
                    profile = self.storage.fetch_profile_from_api(access_token)
            
            if not profile or not profile.get("email"):
                self._update_status(account_id, status="EXPIRED")
                return None

        # 3. Danh gia trang thai dua tren chat lockout / quota detector
        if profile.get("usage_fetched"):
            usage = float(profile.get("usagePercent", 0.0) or 0.0)
            spend = float(profile.get("totalSpend", 0.0) or 0.0)
            msg = profile.get("displayMessage", "")
        else:
            # API fetch that bai hoac bi rate limit -> Giu nguyen persisted quota tu DB / snapshot
            persisted = self.get_persisted_account_quota(account_id=account_id, email=profile.get("email"))
            if persisted:
                usage = float(persisted.get("usage_percent", 0.0) or 0.0)
                spend = float(persisted.get("total_spend", 0.0) or 0.0)
                msg = persisted.get("display_message", "")
            else:
                usage = float(acc["usage_percent"] or 0.0)
                spend = float(acc["total_spend"] or 0.0)
                msg = acc["display_message"] or ""

        locked, lock_reason = is_chat_locked(usage_data=profile or {"usagePercent": usage, "totalSpend": spend}, threshold=QUOTA_EXHAUSTION_THRESHOLD)
        now_ts = int(time.time())
        cooldown_until = 0
        if locked or usage >= QUOTA_EXHAUSTION_THRESHOLD:
            status = "EXHAUSTED"
            try:
                from ai_optimizer import DynamicCooldownManager
                dynamic_cd = DynamicCooldownManager.calculate_cooldown(lock_reason or msg or "")
            except Exception:
                dynamic_cd = self.BLACKLIST_COOLDOWN_SECONDS
            cooldown_until = now_ts + dynamic_cd
        elif usage >= QUOTA_WARNING_THRESHOLD:
            status = "HIGH_USAGE"
        else:
            status = "READY"

        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                cur = con.cursor()
                cur.execute("""
                    UPDATE accounts 
                    SET email = ?, display_name = ?, auth_id = ?, access_token = ?, refresh_token = ?,
                        usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?,
                        cooldown_until = CASE WHEN ? > 0 THEN ? ELSE cooldown_until END
                    WHERE id = ?
                """, (
                    profile.get("email"),
                    profile.get("displayName"),
                    profile.get("authId"),
                    encrypt_token(access_token),
                    encrypt_token(refresh_token),
                    usage,
                    spend,
                    msg,
                    status,
                    now_ts,
                    cooldown_until,
                    cooldown_until,
                    account_id
                ))
                con.commit()
            finally:
                con.close()

        profile["status"] = status
        profile["usagePercent"] = usage
        profile["usage_percent"] = usage
        profile["totalSpend"] = spend
        profile["total_spend"] = spend
        profile["displayMessage"] = msg
        profile["cooldown_until"] = cooldown_until
        try:
            self.record_quota_snapshot(
                account_id=account_id,
                email=profile.get("email"),
                usage_percent=usage,
                total_spend=spend,
                display_message=msg,
                status=status,
                source="refresh",
                cooldown_until=cooldown_until
            )
        except Exception as snap_err:
            print(f"[-] Loi ghi snapshot trong refresh: {snap_err}")
        return profile

    def sync_active_from_cursor(self, force_fetch_api: bool = False, allow_remote_fetch: bool = True) -> Optional[Dict[str, Any]]:
        """
        Dong bo thong tin tai khoan dang active tu Cursor sang database pool.
        DAM BAO: Quota luon duoc persist, khong bao gio bi reset ve 0 khi nguoi dung F5 / reload trang.
        Khi allow_remote_fetch=False, ham chi doc tu state.vscdb va SQLite noi bo (<5ms) de khong block web request.
        """
        active = self.storage.get_active_account()
        token = active.get("access_token")
        email = active.get("email")
        auth_id = active.get("authId")
        display_name = active.get("displayName")
        
        # 1. Neu chua co email ma co token, decode JWT
        if not email and token:
            jwt_data = decode_jwt_payload(token)
            if jwt_data.get("email"):
                email = jwt_data.get("email")
            if not auth_id and jwt_data.get("sub"):
                auth_id = jwt_data.get("sub")

        # 2. Doc thong tin da duoc persist trong DB de lam can cu chuan truoc
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        
        row = None
        if email or auth_id or token:
            cur.execute("""
                SELECT * FROM accounts 
                WHERE (email IS NOT NULL AND LOWER(email) = LOWER(?)) 
                   OR (auth_id IS NOT NULL AND auth_id = ?) 
                   OR (access_token IS NOT NULL AND access_token = ?)
            """, (email or "", auth_id or "", token or ""))
            row = cur.fetchone()

        persisted_usage = float(row["usage_percent"]) if (row and row["usage_percent"] is not None) else 0.0
        persisted_spend = float(row["total_spend"]) if (row and row["total_spend"] is not None) else 0.0
        persisted_msg = row["display_message"] if (row and row["display_message"]) else ""
        persisted_status = row["status"] if (row and row["status"]) else "READY"
        last_checked_time = row["last_checked"] if (row and row["last_checked"]) else 0
                
        # 3. Thu fetch profile tu API neu can thiet (khi force_fetch_api hoac qua han 60s hoac chua co du lieu)
        profile = None
        should_fetch = allow_remote_fetch and (force_fetch_api or (last_checked_time == 0) or ((int(time.time()) - last_checked_time) > 60))
        if token and not active.get("is_expired", False) and should_fetch:
            try:
                profile = self.storage.fetch_profile_from_api(token)
            except Exception as e:
                print(f"[*] Fetch profile trong sync_active: {e}")
                
        if profile and profile.get("email"):
            email = profile.get("email")
            if profile.get("displayName"):
                display_name = profile.get("displayName")
            if profile.get("authId"):
                auth_id = profile.get("authId")
                
        if not email:
            con.close()
            # Neu khong the tim thay email, tra ve active thong thuong neu co token
            return active if active.get("has_token") else None

        # 4. QUOTA PERSISTENCE LOGIC:
        # Neu profile fetch duoc tu API voi usage_fetched = True thi cap nhat gia tri moi tu API
        if profile and profile.get("usage_fetched"):
            usage = float(profile.get("usagePercent", 0.0) or 0.0)
            spend = float(profile.get("totalSpend", 0.0) or 0.0)
            msg = profile.get("displayMessage", "")
        else:
            # Dung nguyen gia tri da persist de tranh bi F5 reset ve 0%
            usage = persisted_usage
            spend = persisted_spend
            msg = persisted_msg
            
            # Neu van bang 0.0, kiem tra snapshot cuoi cung xem co tung co quota hop le khong
            if usage == 0.0 and spend == 0.0:
                snap = self.get_latest_quota_snapshot(account_id=row["id"] if row else None, email=email)
                if snap and ((snap.get("usage_percent") or 0.0) > 0.0 or (snap.get("total_spend") or 0.0) > 0.0):
                    usage = float(snap.get("usage_percent", 0.0) or 0.0)
                    spend = float(snap.get("total_spend", 0.0) or 0.0)
                    msg = snap.get("display_message", msg)
        
        disp_name = display_name or (row["display_name"] if row else email.split("@")[0])
        
        locked, lock_reason = is_chat_locked(usage_data=profile or {"usagePercent": usage, "totalSpend": spend}, threshold=QUOTA_EXHAUSTION_THRESHOLD)
        status = "EXHAUSTED" if locked else ("HIGH_USAGE" if usage >= QUOTA_WARNING_THRESHOLD else "READY")
        now_ts = int(time.time())

        if row:
            acc_id = row["id"]
            cur.execute("""
                UPDATE accounts 
                SET email = ?, display_name = ?, auth_id = ?, 
                    access_token = COALESCE(?, access_token), 
                    refresh_token = COALESCE(?, refresh_token),
                    usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?
                WHERE id = ?
            """, (
                email, disp_name, auth_id or row["auth_id"],
                token, active.get("refresh_token"),
                usage, spend, msg,
                status, now_ts, acc_id
            ))
        else:
            cur.execute("""
                INSERT INTO accounts (file_name, email, display_name, auth_id, access_token, refresh_token,
                                      usage_percent, total_spend, display_message, status, last_checked)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"{email}.txt", email, disp_name, auth_id,
                token, active.get("refresh_token"),
                usage, spend, msg,
                status, now_ts
            ))
            acc_id = cur.lastrowid
        con.commit()
        con.close()

        # Luu quota snapshot lich su va dong bo toan bo duplicates neu co
        try:
            self.record_quota_snapshot(
                account_id=acc_id,
                email=email,
                usage_percent=usage,
                total_spend=spend,
                display_message=msg,
                status=status,
                source="sync"
            )
        except Exception as snap_err:
            print(f"[-] Loi ghi snapshot trong sync_active: {snap_err}")
            
        res = {
            "id": acc_id,
            "email": email,
            "displayName": disp_name,
            "authId": auth_id,
            "usagePercent": usage,
            "usage_percent": usage,
            "totalSpend": spend,
            "total_spend": spend,
            "displayMessage": msg,
            "status": status,
            "has_token": bool(token),
            "usage_fetched": bool(profile.get("usage_fetched", False)) if profile else False,
            "membershipType": active.get("membership_type", "free"),
            "signUpType": active.get("signUpType", "Google"),
            "is_active": True,
            "last_checked": now_ts,
            "last_sync": now_ts
        }
        return res

    def _update_status(self, account_id: int, status: str):
        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute("UPDATE accounts SET status = ?, last_checked = ? WHERE id = ?", (status, int(time.time()), account_id))
            con.commit()
            con.close()

    def switch_to_account(
        self,
        account_id: int,
        auto_reload: bool = True,
        auto_continue: Optional[bool] = None,
        spoof_hw: bool = True,
        verify: bool = True,
        reset_mode: Optional[str] = None
    ) -> bool:
        """Nap tai khoan co account_id vao Cursor state.vscdb, xac thuc 100%, spoof hardware IDs va dong bo ngay lap tuc."""
        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            acc = cur.fetchone()
            con.close()

        if not acc:
            return False

        access_token = decrypt_token(acc["access_token"])
        refresh_token = decrypt_token(acc["refresh_token"])
        
        # Kiem tra xem token co hop le khong hay can refresh
        need_refresh = not access_token
        if access_token:
            jwt_data = decode_jwt_payload(access_token)
            exp = jwt_data.get("exp")
            if exp and time.time() >= (int(exp) - 60):
                need_refresh = True

        if need_refresh:
            res = self.refresh_account_quota(account_id)
            if res:
                # Lay lai data vua cap nhat
                with DB_LOCK:
                    con = sqlite3.connect(DB_FILE, timeout=30.0)
                    con.row_factory = sqlite3.Row
                    cur = con.cursor()
                    cur.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
                    acc = cur.fetchone()
                    con.close()
                if acc:
                    access_token = decrypt_token(acc["access_token"])
                    refresh_token = decrypt_token(acc["refresh_token"])

        if not access_token:
            return False

        profile = {
            "email": acc["email"],
            "displayName": acc["display_name"],
            "authId": acc["auth_id"],
            "signUpType": "Google",
            "membershipType": "free"
        }

        # Xac dinh reset_mode tu tham so hoac auto_switch_config.json (Default: hard_restart)
        try:
            from cursor_settings import CursorSettingsManager
            cfg = CursorSettingsManager().get_auto_switch_config()
        except Exception:
            cfg = {"reset_mode": "hard_restart", "auto_resend_action": "manual", "continue_prompt": "Tiếp tục", "target_mode": "composer"}

        effective_reset_mode = reset_mode or cfg.get("reset_mode", "hard_restart")
        cursor_was_running = False

        # 1. HARD APP RESET: Neu reset_mode la hard_restart va auto_reload duoc bat, tat hoan toan Cursor.exe truoc khi ghi state!
        if auto_reload and effective_reset_mode == "hard_restart":
            try:
                from cursor_reloader import is_cursor_running, terminate_cursor
                if is_cursor_running():
                    cursor_was_running = True
                    print("[HARD-RESET] Tat hoan toan Cursor.exe truoc khi ghi nạp tokens de nha file locks...")
                    terminate_cursor(timeout_sec=8.0)
            except Exception as term_err:
                print(f"[-] Loi terminate_cursor truoc switch: {term_err}")

        success = self.storage.inject_full_profile(access_token, refresh_token, profile)
        if success:
            # 1. Xac thuc 100% tai khoan active trong state.vscdb
            if verify:
                verified = self.storage.get_active_account()
                target_email = (acc["email"] or "").strip().lower()
                verified_email = (verified.get("email") or "").strip().lower()
                has_token = bool(verified.get("has_token"))
                if not verified or verified_email != target_email or not has_token:
                    print(f"[-] Xac thuc state.vscdb khong khop ({verified_email} != {target_email}, has_token={has_token}). Thu ghi lai...")
                    self.storage.inject_full_profile(access_token, refresh_token, profile)
                    verified = self.storage.get_active_account()
                    verified_email = (verified.get("email") or "").strip().lower()
                    has_token = bool(verified.get("has_token"))
                    if not verified or verified_email != target_email or not has_token:
                        print(f"[-] Xac thuc active account trong state.vscdb that bai hoan toan!")
                        return False
                print(f"[+] Da xac thuc 100% tai khoan active trong state.vscdb: {verified.get('email')}")

            # 2. Anti-Detect Hardware Fingerprint Isolation (storage.json)
            if spoof_hw:
                try:
                    from cursor_settings import CursorSettingsManager
                    csm = CursorSettingsManager()
                    cfg_switch = csm.get_auto_switch_config()
                    antidetect_mode = cfg_switch.get("antidetect_mode", "account_locked")

                    if antidetect_mode == "account_locked":
                        acc_fp = self.get_account_fingerprint(account_id)
                        if acc_fp:
                            csm.apply_fingerprint(acc_fp)
                        else:
                            csm.spoof_storage_ids()
                    elif antidetect_mode == "stealth_randomize":
                        csm.spoof_storage_ids()
                    elif antidetect_mode == "native_standard":
                        pass
                    else:
                        csm.spoof_storage_ids()
                except Exception as hw_err:
                    print(f"[-] Canh bao: Khong the ap dung anti-detect hardware fingerprint: {hw_err}")

            # 3. Tu dong ap dung Config Profile cua tai khoan vao Cursor
            try:
                from cursor_settings import CursorSettingsManager
                assigned_profile = "default"
                if "config_profile" in acc.keys() and acc["config_profile"]:
                    assigned_profile = acc["config_profile"]
                acc_email = acc["email"] if "email" in acc.keys() else "unknown"
                print(f"[CONFIG-PROFILE] Tu dong ap dung Profile '{assigned_profile}' cho account {acc_email}...")
                CursorSettingsManager().apply_profile_to_cursor(assigned_profile)
            except Exception as prof_err:
                print(f"[-] Canh bao ap dung config profile khi switch: {prof_err}")

            self.manual_active_account_id = account_id
            self.last_manual_switch_time = time.time()
            
            with DB_LOCK:
                con = sqlite3.connect(DB_FILE, timeout=30.0)
                cur = con.cursor()
                cur.execute("UPDATE accounts SET last_checked = ? WHERE id = ?", (int(time.time()), account_id))
                con.commit()
                con.close()
                
            if auto_reload:
                # App restart / reload theo reset_mode
                if effective_reset_mode == "hard_restart":
                    try:
                        from cursor_reloader import launch_cursor_app
                        print("[HARD-RESET] Khoi dong lai Cursor.exe voi account moi va thiet bi moi...")
                        launch_cursor_app(wait_for_window=True, timeout_sec=15.0)
                    except Exception as re_err:
                        print(f"[-] Loi relaunch Cursor.exe sau switch_to_account: {re_err}")
                else:
                    try:
                        from cursor_reloader import trigger_cursor_reload
                        trigger_cursor_reload(auto_focus=True, reset_mode="soft_reload")
                    except Exception as re_err:
                        print(f"[-] Loi soft reload sau switch_to_account: {re_err}")

                # 3. Smart Task Completion Filter: Chi tiep tuc neu task thuc su bi gian doan boi quota/lockout
                try:
                    from smart_task_filter import SmartTaskCompletionFilter
                    should_continue, filter_reason = SmartTaskCompletionFilter().should_auto_continue(
                        auto_continue=auto_continue,
                        auto_resend_cfg=cfg.get("auto_resend_action", "manual"),
                        consume=True
                    )
                except Exception as filter_err:
                    print(f"[-] SmartTaskCompletionFilter loi: {filter_err}")
                    should_continue = False
                    filter_reason = str(filter_err)

                print(f"[SMART-FILTER] Auto-continue decision: {should_continue} ({filter_reason})")

                if should_continue:
                    prompt_txt = cfg.get("continue_prompt", "Tiếp tục")
                    target_comp = (cfg.get("target_mode") != "chat")

                    def _run_delayed_continue():
                        # Doi cua so khoi dong on dinh va render Composer/Chat
                        time.sleep(2.5)
                        try:
                            from cursor_reloader import send_continue_prompt
                            send_continue_prompt(prompt_text=prompt_txt, target_composer=target_comp, fresh_session=True)
                        except Exception as ce:
                            print(f"[-] Loi auto-send continue prompt: {ce}")

                    threading.Thread(target=_run_delayed_continue, daemon=True).start()
                else:
                    print(f"[SMART-FILTER] Prompt-resend suppressed ({filter_reason}). State cleanly reset.")
                    
        return success


    def auto_switch_best_account(
        self,
        notify: bool = True,
        prev_email: Optional[str] = None,
        auto_continue: Optional[bool] = None,
        spoof_hw: bool = True,
        reset_mode: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Tu dong tim tai khoan con quota tot nhat (< QUOTA_EXHAUSTION_THRESHOLD) va nap vao Cursor.
        Tier 1 (< 50%) duoc uu tien toi da. Neu het Tier 1 moi fallback sang Tier 2 (50% - 99%).
        Tai khoan >= 100% hoac bi chat-locked se bi dua vao Blacklist 7 ngay (cooldown_until = now + 7*86400).
        Neu khong con trong ca 2 tier, thu sang cac tai khoan PENDING: refresh quota truoc, neu < 100% thi moi nap.
        """
        if not prev_email:
            prev_active = self.storage.get_active_account()
            prev_email = (prev_active.get("email") or "").strip()
        else:
            prev_email = prev_email.strip()

        now_ts = int(time.time())
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        try:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("PRAGMA table_info(accounts);")
            cols = [r[1] for r in cur.fetchall()]
            has_cd = "cooldown_until" in cols

            # 1. Danh sach cac tai khoan READY hoac HIGH_USAGE (< QUOTA_EXHAUSTION_THRESHOLD) va khong bi cooldown
            if has_cd:
                cur.execute("""
                    SELECT id, email, usage_percent, total_spend, status, cooldown_until FROM accounts 
                    WHERE status IN ('READY', 'HIGH_USAGE') 
                      AND (usage_percent IS NULL OR usage_percent < ?)
                      AND (cooldown_until IS NULL OR cooldown_until <= ?)
                    ORDER BY usage_percent ASC, id ASC
                """, (QUOTA_EXHAUSTION_THRESHOLD, now_ts))
            else:
                cur.execute("""
                    SELECT id, email, usage_percent, total_spend, status FROM accounts 
                    WHERE status IN ('READY', 'HIGH_USAGE') 
                      AND (usage_percent IS NULL OR usage_percent < ?)
                    ORDER BY usage_percent ASC, id ASC
                """, (QUOTA_EXHAUSTION_THRESHOLD,))
            ready_candidates = [dict(r) for r in cur.fetchall()]

            # 2. Danh sach cac tai khoan PENDING du phong (khong bi cooldown)
            if has_cd:
                cur.execute("""
                    SELECT id, email, file_name, usage_percent, total_spend, status, cooldown_until FROM accounts 
                    WHERE status = 'PENDING'
                      AND (cooldown_until IS NULL OR cooldown_until <= ?)
                    ORDER BY id ASC
                """, (now_ts,))
            else:
                cur.execute("""
                    SELECT id, email, file_name, usage_percent, total_spend, status FROM accounts 
                    WHERE status = 'PENDING'
                    ORDER BY id ASC
                """)
            pending_candidates = [dict(r) for r in cur.fetchall()]
        finally:
            con.close()

        # Loai bo tai khoan dang su dung (prev_email) de dam bao luon xoay tua sang tai khoan moi
        if prev_email:
            p_em = prev_email.strip().lower()
            ready_candidates = [c for c in ready_candidates if (c.get("email") or "").strip().lower() != p_em]
            pending_candidates = [c for c in pending_candidates if (c.get("email") or "").strip().lower() != p_em]

        def _notify_and_return(updated_row: sqlite3.Row) -> Dict[str, Any]:
            res = dict(updated_row)
            new_email = (res.get("email") or "").strip()
            new_usage = res.get("usage_percent", 0.0) or 0.0
            if not prev_email or prev_email.lower() != new_email.lower():
                self.last_rotation_event = {
                    "time": int(time.time()),
                    "from": prev_email,
                    "to": new_email,
                    "usage": new_usage
                }
                if notify:
                    try:
                        from windows_notifier import send_windows_notification
                        send_windows_notification(
                            title="Cursor Account Rotated",
                            message=f"Switched from {prev_email or 'None'} to {new_email} ({new_usage:.1f}%)"
                        )
                    except Exception as notif_err:
                        print(f"[-] Loi gui notification: {notif_err}")
            return res

        # Reconcile toan bo candidates voi persisted quota (bao gom snapshot moi nhat)
        # Dam bao khong bao gio danh gia nham tai khoan da dung quota thanh 0%
        valid_ready_candidates = []
        for cand in ready_candidates:
            persisted = self.get_persisted_account_quota(account_id=cand["id"], email=cand.get("email"))
            if persisted:
                eff_usage = float(persisted.get("usage_percent", cand.get("usage_percent", 0.0)) or 0.0)
                eff_spend = float(persisted.get("total_spend", cand.get("total_spend", 0.0)) or 0.0)
                eff_status = persisted.get("status", cand.get("status", "READY"))
            else:
                eff_usage = float(cand.get("usage_percent", 0.0) or 0.0)
                eff_spend = float(cand.get("total_spend", 0.0) or 0.0)
                eff_status = cand.get("status", "READY")

            # Neu persisted snapshot cho thay da can quota (>= 100%) hoac EXHAUSTED: dua vao Blacklist 7 ngay va bo qua
            if eff_usage >= QUOTA_EXHAUSTION_THRESHOLD or eff_status == "EXHAUSTED":
                cd_until = now_ts + self.BLACKLIST_COOLDOWN_SECONDS
                with DB_LOCK:
                    con_up = sqlite3.connect(DB_FILE, timeout=10.0)
                    try:
                        cur_up = con_up.cursor()
                        if has_cd:
                            cur_up.execute("""
                                UPDATE accounts 
                                SET usage_percent = ?, total_spend = ?, status = 'EXHAUSTED', cooldown_until = ? 
                                WHERE id = ?
                            """, (eff_usage, eff_spend, cd_until, cand["id"]))
                        else:
                            cur_up.execute("""
                                UPDATE accounts 
                                SET usage_percent = ?, total_spend = ?, status = 'EXHAUSTED' 
                                WHERE id = ?
                            """, (eff_usage, eff_spend, cand["id"]))
                        con_up.commit()
                    finally:
                        con_up.close()
                continue

            if (cand.get("cooldown_until") or 0) > now_ts:
                continue

            cand["effective_usage"] = eff_usage
            cand["effective_spend"] = eff_spend
            valid_ready_candidates.append(cand)

        # Phan tach Two-Tier Priority Pool:
        # Tier 1 (< 50%): Uu tien tuyet doi cho fast requests
        # Tier 2 (50% - 99%): Fallback chi dung khi Tier 1 het sach
        tier1_candidates = [c for c in valid_ready_candidates if c["effective_usage"] < self.TIER1_THRESHOLD]
        tier2_candidates = [c for c in valid_ready_candidates if c["effective_usage"] >= self.TIER1_THRESHOLD]

        tier1_candidates.sort(key=lambda c: (c["effective_usage"], c.get("effective_spend", 0.0), c["id"]))
        tier2_candidates.sort(key=lambda c: (c["effective_usage"], c.get("effective_spend", 0.0), c["id"]))

        ordered_candidates = tier1_candidates + tier2_candidates

        # Thu lan luot cac tai khoan hop le (Tier 1 truoc, Tier 2 sau)
        for cand in ordered_candidates:
            sw_kwargs = {}
            if auto_continue is not None:
                sw_kwargs["auto_continue"] = auto_continue
            if not spoof_hw:
                sw_kwargs["spoof_hw"] = spoof_hw
            if reset_mode is not None:
                sw_kwargs["reset_mode"] = reset_mode

            ok = self.switch_to_account(cand["id"], **sw_kwargs) if sw_kwargs else self.switch_to_account(cand["id"])
            if ok:
                con = sqlite3.connect(DB_FILE, timeout=30.0)
                try:
                    con.row_factory = sqlite3.Row
                    cur = con.cursor()
                    cur.execute("SELECT id, email, usage_percent, total_spend, status FROM accounts WHERE id = ?", (cand["id"],))
                    updated = cur.fetchone()
                finally:
                    con.close()

                if (
                    updated
                    and (updated["usage_percent"] or 0) < QUOTA_EXHAUSTION_THRESHOLD
                    and updated["status"] != "EXHAUSTED"
                ):
                    try:
                        self.record_quota_snapshot(
                            account_id=updated["id"],
                            email=updated["email"],
                            usage_percent=float(updated["usage_percent"] or 0.0),
                            total_spend=float(updated["total_spend"] or 0.0),
                            status=updated["status"],
                            source="auto_switch"
                        )
                    except Exception:
                        pass
                    return _notify_and_return(updated)

        # Fallback: Kiem tra tung tai khoan PENDING, refresh quota tu API
        for cand in pending_candidates:
            if (cand.get("cooldown_until") or 0) > now_ts:
                continue

            refreshed = self.refresh_account_quota(cand["id"])
            if not refreshed or not refreshed.get("email"):
                continue  # Token/cookie loi hoac expired, thu tiep

            locked, _ = is_chat_locked(usage_data=refreshed, threshold=QUOTA_EXHAUSTION_THRESHOLD)
            if locked or float(refreshed.get("usagePercent", 0.0) or 0.0) >= QUOTA_EXHAUSTION_THRESHOLD:
                # Da het quota (>= 100%) hoac bi khoa chat, set cooldown 7 ngay va bo qua
                cd_until = now_ts + self.BLACKLIST_COOLDOWN_SECONDS
                with DB_LOCK:
                    con_up = sqlite3.connect(DB_FILE, timeout=10.0)
                    try:
                        if has_cd:
                            con_up.execute("UPDATE accounts SET status = 'EXHAUSTED', cooldown_until = ? WHERE id = ?", (cd_until, cand["id"]))
                        else:
                            con_up.execute("UPDATE accounts SET status = 'EXHAUSTED' WHERE id = ?", (cand["id"],))
                        con_up.commit()
                    finally:
                        con_up.close()
                continue

            sw_kwargs = {}
            if auto_continue is not None:
                sw_kwargs["auto_continue"] = auto_continue
            if not spoof_hw:
                sw_kwargs["spoof_hw"] = spoof_hw
            if reset_mode is not None:
                sw_kwargs["reset_mode"] = reset_mode

            ok = self.switch_to_account(cand["id"], **sw_kwargs) if sw_kwargs else self.switch_to_account(cand["id"])
            if ok:
                con = sqlite3.connect(DB_FILE, timeout=30.0)
                try:
                    con.row_factory = sqlite3.Row
                    cur = con.cursor()
                    cur.execute("SELECT id, email, usage_percent, total_spend, status FROM accounts WHERE id = ?", (cand["id"],))
                    updated = cur.fetchone()
                finally:
                    con.close()

                if (
                    updated
                    and (updated["usage_percent"] or 0) < QUOTA_EXHAUSTION_THRESHOLD
                    and updated["status"] != "EXHAUSTED"
                ):
                    try:
                        self.record_quota_snapshot(
                            account_id=updated["id"],
                            email=updated["email"],
                            usage_percent=float(updated["usage_percent"] or 0.0),
                            total_spend=float(updated["total_spend"] or 0.0),
                            status=updated["status"],
                            source="auto_switch_pending"
                        )
                    except Exception:
                        pass
                    return _notify_and_return(updated)

        return None

    def start_batch_check(self) -> bool:
        """Khoi dong tien trinh chay ngam kiem tra song song 20 luong."""
        if getattr(self, "batch_running", False):
            return False

        self.batch_running = True
        self.batch_stop_requested = False

        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute("SELECT id, email, file_name FROM accounts ORDER BY last_checked ASC, id ASC")
        accounts_to_check = cur.fetchall()
        con.close()

        self.batch_progress = {
            "running": True,
            "current": 0,
            "total": len(accounts_to_check),
            "current_email": "",
            "success_count": 0,
            "failed_count": 0
        }

        def _worker():
            progress_lock = threading.Lock()

            def _check_single(item):
                if self.batch_stop_requested:
                    return

                acc_id, email, fname = item
                target_name = email or fname

                with progress_lock:
                    self.batch_progress["current_email"] = target_name

                success = False
                try:
                    res = self.refresh_account_quota(acc_id)
                    if res and res.get("email"):
                        success = True
                except Exception as e:
                    print(f"[-] Loi check account {acc_id}: {e}")

                with progress_lock:
                    self.batch_progress["current"] += 1
                    if success:
                        self.batch_progress["success_count"] += 1
                    else:
                        self.batch_progress["failed_count"] += 1

            # Chay 20 workers dong thoi
            with ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(_check_single, item) for item in accounts_to_check]
                for future in as_completed(futures):
                    if self.batch_stop_requested:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

            self.batch_running = False
            self.batch_progress["running"] = False

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        return True

    def stop_batch_check(self) -> bool:
        """Dung tien trinh kiem tra hang loat."""
        if getattr(self, "batch_running", False):
            self.batch_stop_requested = True
            return True
        return False

    def get_batch_progress(self) -> Dict[str, Any]:
        """Lay tien do kiem tra hang loat."""
        if not hasattr(self, "batch_progress"):
            return {"running": False, "current": 0, "total": 0, "current_email": "", "success_count": 0, "failed_count": 0}
        return self.batch_progress

    def start_startup_quota_sync(self, max_workers: int = 12, force: bool = False, blocking: bool = False) -> bool:
        """
        Khoi dong luong kiem tra quota ngam voi 10-15 luong dong thoi (mac dinh max_workers=12) moi khi mo dashboard.
        Su dung truc tiep access_token da luu trong DB de truy van quota nhanh va cap nhat trang thai chinh xac.
        Phan loai hai tang Quota (Tier 1 < 50%, Tier 2 50%-99%) va Blacklist 7 ngay cho tai khoan can quota.
        """
        if getattr(self, "startup_sync_running", False) and not force:
            return False

        self.startup_sync_running = True
        self.startup_sync_stop_requested = False

        con = sqlite3.connect(DB_FILE, timeout=30.0)
        try:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("PRAGMA table_info(accounts);")
            cols = [r[1] for r in cur.fetchall()]
            has_cd = "cooldown_until" in cols
            cur.execute("SELECT * FROM accounts ORDER BY last_checked ASC, id ASC")
            accounts = [dict(r) for r in cur.fetchall()]
        finally:
            con.close()

        self.startup_sync_progress = {
            "running": True,
            "total": len(accounts),
            "current": 0,
            "ready_count": 0,
            "high_usage_count": 0,
            "exhausted_count": 0,
            "blacklist_count": 0,
            "tier1_count": 0,
            "tier2_count": 0,
            "expired_count": 0,
            "failed_count": 0,
            "current_account": "",
            "max_workers": max_workers,
            "started_at": time.time(),
            "finished_at": None,
            "duration_seconds": 0.0
        }

        def _worker():
            progress_lock = threading.Lock()

            def _sync_single(acc):
                if getattr(self, "startup_sync_stop_requested", False):
                    return

                acc_id = acc["id"]
                email = acc.get("email") or acc.get("file_name") or f"Account #{acc_id}"
                with progress_lock:
                    self.startup_sync_progress["current_account"] = email

                token = acc.get("access_token")
                cookie_full = acc.get("cookie_full")
                new_status = None
                success = False
                cur_usage = 0.0

                try:
                    # 1. Neu chua co token, thu exchange cookie neu co
                    if not token and cookie_full:
                        tokens = self.auth_client.exchange_cookie_to_tokens(cookie_full)
                        if tokens and tokens.get("accessToken"):
                            token = tokens["accessToken"]
                            acc["refresh_token"] = tokens.get("refreshToken")
                            acc["access_token"] = token
                            with DB_LOCK:
                                c = sqlite3.connect(DB_FILE, timeout=30.0)
                                try:
                                    c.execute("UPDATE accounts SET access_token = ?, refresh_token = ? WHERE id = ?", (token, acc["refresh_token"], acc_id))
                                    c.commit()
                                finally:
                                    c.close()
                        else:
                            new_status = "EXPIRED"

                    # 2. Goi truc tiep API GetCurrentPeriodUsage cua Cursor voi access_token da luu
                    if token and not new_status:
                        headers = {
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"
                        }
                        resp = requests.post(
                            "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
                            headers=headers,
                            json={},
                            timeout=6
                        )

                        if resp.status_code == 200:
                            ud = resp.json()
                            plan_usage = ud.get("planUsage", {})
                            usage = float(plan_usage.get("totalPercentUsed", 0.0) or 0.0)
                            auto_usage = float(plan_usage.get("autoPercentUsed", 0.0) or 0.0)
                            spend = float(plan_usage.get("totalSpend", 0.0) or 0.0)
                            msg = ud.get("displayMessage", "") or ""
                            cur_usage = usage

                            # Kiem tra chat locked / quota threshold
                            locked, _ = is_chat_locked(
                                usage_data={"usagePercent": usage, "autoPercentUsed": auto_usage, "totalSpend": spend},
                                threshold=self.QUOTA_EXHAUSTION_THRESHOLD
                            )
                            now_ts = int(time.time())
                            cd_until = 0
                            if locked or usage >= self.QUOTA_EXHAUSTION_THRESHOLD or spend >= 200.0:
                                new_status = "EXHAUSTED"
                                cd_until = now_ts + self.BLACKLIST_COOLDOWN_SECONDS
                            elif usage >= self.QUOTA_WARNING_THRESHOLD:
                                new_status = "HIGH_USAGE"
                            else:
                                new_status = "READY"

                            with DB_LOCK:
                                c = sqlite3.connect(DB_FILE, timeout=30.0)
                                try:
                                    if has_cd:
                                        c.execute("""
                                            UPDATE accounts 
                                            SET usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?,
                                                cooldown_until = CASE WHEN ? > 0 THEN ? ELSE cooldown_until END
                                            WHERE id = ?
                                        """, (usage, spend, msg, new_status, now_ts, cd_until, cd_until, acc_id))
                                    else:
                                        c.execute("""
                                            UPDATE accounts 
                                            SET usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?
                                            WHERE id = ?
                                        """, (usage, spend, msg, new_status, now_ts, acc_id))
                                    c.commit()
                                finally:
                                    c.close()

                            try:
                                self.record_quota_snapshot(
                                    account_id=acc_id,
                                    email=email,
                                    usage_percent=usage,
                                    total_spend=spend,
                                    display_message=msg,
                                    status=new_status,
                                    source="startup_sync",
                                    cooldown_until=cd_until
                                )
                            except Exception:
                                pass
                            success = True

                        elif resp.status_code in (401, 403):
                            # Token het han -> Thu refresh lai qua cookie
                            refreshed = False
                            if cookie_full:
                                tokens = self.auth_client.exchange_cookie_to_tokens(cookie_full)
                                if tokens and tokens.get("accessToken"):
                                    new_tok = tokens["accessToken"]
                                    new_ref = tokens.get("refreshToken")
                                    headers["Authorization"] = f"Bearer {new_tok}"
                                    r2 = requests.post(
                                        "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
                                        headers=headers,
                                        json={},
                                        timeout=10
                                    )
                                    if r2.status_code == 200:
                                        ud = r2.json()
                                        plan_usage = ud.get("planUsage", {})
                                        usage = float(plan_usage.get("totalPercentUsed", 0.0) or 0.0)
                                        auto_usage = float(plan_usage.get("autoPercentUsed", 0.0) or 0.0)
                                        spend = float(plan_usage.get("totalSpend", 0.0) or 0.0)
                                        msg = ud.get("displayMessage", "") or ""
                                        cur_usage = usage
                                        locked, _ = is_chat_locked(
                                            usage_data={"usagePercent": usage, "autoPercentUsed": auto_usage, "totalSpend": spend},
                                            threshold=self.QUOTA_EXHAUSTION_THRESHOLD
                                        )
                                        now_ts = int(time.time())
                                        cd_until = 0
                                        if locked or usage >= self.QUOTA_EXHAUSTION_THRESHOLD or spend >= 200.0:
                                            new_status = "EXHAUSTED"
                                            cd_until = now_ts + self.BLACKLIST_COOLDOWN_SECONDS
                                        elif usage >= self.QUOTA_WARNING_THRESHOLD:
                                            new_status = "HIGH_USAGE"
                                        else:
                                            new_status = "READY"

                                        with DB_LOCK:
                                            c = sqlite3.connect(DB_FILE, timeout=30.0)
                                            try:
                                                if has_cd:
                                                    c.execute("""
                                                        UPDATE accounts 
                                                        SET access_token = ?, refresh_token = ?, usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?,
                                                            cooldown_until = CASE WHEN ? > 0 THEN ? ELSE cooldown_until END
                                                        WHERE id = ?
                                                    """, (new_tok, new_ref, usage, spend, msg, new_status, now_ts, cd_until, cd_until, acc_id))
                                                else:
                                                    c.execute("""
                                                        UPDATE accounts 
                                                        SET access_token = ?, refresh_token = ?, usage_percent = ?, total_spend = ?, display_message = ?, status = ?, last_checked = ?
                                                        WHERE id = ?
                                                    """, (new_tok, new_ref, usage, spend, msg, new_status, now_ts, acc_id))
                                                c.commit()
                                            finally:
                                                c.close()

                                        refreshed = True
                                        success = True
                            if not refreshed:
                                new_status = "EXPIRED"
                                now_ts = int(time.time())
                                with DB_LOCK:
                                    c = sqlite3.connect(DB_FILE, timeout=30.0)
                                    try:
                                        c.execute("UPDATE accounts SET status = 'EXPIRED', last_checked = ? WHERE id = ?", (now_ts, acc_id))
                                        c.commit()
                                    finally:
                                        c.close()
                        else:
                            # Khong phai 200/401 (vi du 429 / 500) -> Giu nguyen trang thai
                            pass

                    elif not token and new_status == "EXPIRED":
                        now_ts = int(time.time())
                        with DB_LOCK:
                            c = sqlite3.connect(DB_FILE, timeout=30.0)
                            try:
                                c.execute("UPDATE accounts SET status = 'EXPIRED', last_checked = ? WHERE id = ?", (now_ts, acc_id))
                                c.commit()
                            finally:
                                c.close()

                except Exception as e:
                    if "timed out" in str(e).lower() or "timeout" in str(e).lower():
                        print(f"[!] Startup quota sync: Account #{acc_id} ({email}) timeout (bo qua, giu cache)")
                    else:
                        print(f"[-] Loi kiem tra startup quota cho account #{acc_id} ({email}): {e}")

                with progress_lock:
                    self.startup_sync_progress["current"] += 1
                    if success:
                        if new_status == "READY":
                            self.startup_sync_progress["ready_count"] += 1
                        elif new_status == "HIGH_USAGE":
                            self.startup_sync_progress["high_usage_count"] += 1
                        elif new_status == "EXHAUSTED":
                            self.startup_sync_progress["exhausted_count"] += 1
                            self.startup_sync_progress["blacklist_count"] += 1
                        
                        if cur_usage < self.TIER1_THRESHOLD:
                            self.startup_sync_progress["tier1_count"] += 1
                        elif cur_usage < self.TIER2_THRESHOLD:
                            self.startup_sync_progress["tier2_count"] += 1
                    elif new_status == "EXPIRED":
                        self.startup_sync_progress["expired_count"] += 1
                    else:
                        self.startup_sync_progress["failed_count"] += 1

            # Chay 10-15 worker threads dong thoi (mac dinh max_workers=12)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_sync_single, acc) for acc in accounts]
                for f in as_completed(futures):
                    if getattr(self, "startup_sync_stop_requested", False):
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

            self.startup_sync_running = False
            self.startup_sync_progress["running"] = False
            self.startup_sync_progress["finished_at"] = time.time()
            self.startup_sync_progress["duration_seconds"] = round(
                self.startup_sync_progress["finished_at"] - self.startup_sync_progress["started_at"], 2
            )
            print(f"[+] Hoan tat Startup Quota Sync ({len(accounts)} accounts, {max_workers} luong) trong {self.startup_sync_progress['duration_seconds']}s. Ready: {self.startup_sync_progress['ready_count']}, High: {self.startup_sync_progress['high_usage_count']}, Exhausted: {self.startup_sync_progress['exhausted_count']}, Expired: {self.startup_sync_progress['expired_count']}")

        if blocking:
            _worker()
            return True
        else:
            coordinator = threading.Thread(target=_worker, name="StartupQuotaSyncCoordinator", daemon=True)
            coordinator.start()
            return True

    def get_startup_sync_progress(self) -> Dict[str, Any]:
        """Lay thong tin tien do kiem tra quota khoi dong 5 luong."""
        if not hasattr(self, "startup_sync_progress"):
            return {
                "running": False,
                "total": 0,
                "current": 0,
                "ready_count": 0,
                "high_usage_count": 0,
                "exhausted_count": 0,
                "expired_count": 0,
                "failed_count": 0,
                "current_account": "",
                "max_workers": 5,
                "started_at": None,
                "finished_at": None,
                "duration_seconds": 0.0
            }
        return self.startup_sync_progress

    def stop_startup_quota_sync(self) -> bool:
        """Dung tien trinh kiem tra quota khoi dong neu dang chay."""
        if getattr(self, "startup_sync_running", False):
            self.startup_sync_stop_requested = True
            return True
        return False

    def _quarantine_file(self, fname: Optional[str], email: Optional[str] = None, reason: str = "corrupted") -> bool:
        """Chuyen file cookie loi hoac hong vao Cookies/Archive_Corrupted de tranh quet lap di lap lai."""
        archive_dir = os.path.join(self.cookies_dir, "Archive_Corrupted")
        os.makedirs(archive_dir, exist_ok=True)
        candidates = []
        if fname:
            candidates.append(os.path.join(self.cookies_dir, fname))
        if email:
            candidates.append(os.path.join(self.cookies_dir, f"{email}.txt"))
            candidates.append(os.path.join(self.cookies_dir, email))

        moved = False
        import shutil
        for path in candidates:
            if os.path.exists(path) and os.path.isfile(path):
                base = os.path.basename(path)
                dst = os.path.join(archive_dir, base)
                try:
                    shutil.move(path, dst)
                    moved = True
                    print(f"[QUARANTINE] Da cach ly cookie loi ({base} -> Archive_Corrupted, reason: {reason})")
                except Exception as e:
                    print(f"[-] Loi cach ly cookie {base}: {e}")
        return moved

    def quarantine_corrupted_cookies(self) -> int:
        """
        Quet va tu dong cach ly cac file cookie hong (0 bytes, unparseable, hoac da bi EXPIRED)
        vao thu muc Cookies/Archive_Corrupted.
        """
        archive_dir = os.path.join(self.cookies_dir, "Archive_Corrupted")
        os.makedirs(archive_dir, exist_ok=True)
        count = 0
        if not os.path.exists(self.cookies_dir):
            return 0

        # 1. Cach ly cac file co dung luong 0 bytes hoac noi dung rong
        for f in os.listdir(self.cookies_dir):
            if f.lower() == "archive_corrupted" or f.startswith("."):
                continue
            if f.lower().endswith(".txt") or f.lower().endswith(".cookie"):
                fp = os.path.join(self.cookies_dir, f)
                if os.path.isfile(fp):
                    try:
                        if os.path.getsize(fp) == 0:
                            if self._quarantine_file(f, reason="empty_file"):
                                count += 1
                        else:
                            with open(fp, "r", encoding="utf-8", errors="ignore") as rf:
                                content = rf.read().strip()
                            if not content:
                                if self._quarantine_file(f, reason="blank_file"):
                                    count += 1
                    except Exception:
                        pass

        # 2. Cach ly cac file gan lien voi account co status EXPIRED trong DB
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        try:
            cur = con.cursor()
            cur.execute("SELECT file_name, email FROM accounts WHERE status = 'EXPIRED'")
            rows = cur.fetchall()
            for fname, email in rows:
                if self._quarantine_file(fname, email, reason="account_expired"):
                    count += 1
        finally:
            con.close()

        return count

    def _remove_cookie_file(self, fname: Optional[str], email: Optional[str]) -> bool:
        """Xoa file cookie vat ly khoi thu muc Cookies."""
        candidates = []
        if fname:
            candidates.append(os.path.join(self.cookies_dir, fname))
        if email:
            candidates.append(os.path.join(self.cookies_dir, f"{email}.txt"))
            candidates.append(os.path.join(self.cookies_dir, email))

        deleted = False
        for path in candidates:
            if os.path.exists(path) and os.path.isfile(path):
                try:
                    os.remove(path)
                    deleted = True
                except Exception as e:
                    print(f"[-] Loi khi xoa file {path}: {e}")
        return deleted

    def delete_invalid_accounts(self, archive: bool = True) -> int:
        """Xoa toan bo cac tai khoan co trang thai EXPIRED khoi database pool va cach ly / xoa file cookie vat ly."""
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute("SELECT file_name, email FROM accounts WHERE status = 'EXPIRED'")
        rows = cur.fetchall()
        for fname, email in rows:
            if archive:
                self._quarantine_file(fname, email, reason="deleted_expired")
            else:
                self._remove_cookie_file(fname, email)

        cur.execute("DELETE FROM accounts WHERE status = 'EXPIRED'")
        deleted_count = cur.rowcount
        con.commit()
        con.close()
        return deleted_count

    def delete_exhausted_accounts(self, threshold: float = QUOTA_EXHAUSTION_THRESHOLD) -> int:
        """Xoa toan bo cac tai khoan co usage_percent >= threshold hoac status la EXHAUSTED va xoa luon file cookie vat ly."""
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute("SELECT file_name, email FROM accounts WHERE usage_percent >= ? OR status = 'EXHAUSTED'", (threshold,))
        rows = cur.fetchall()
        for fname, email in rows:
            self._remove_cookie_file(fname, email)

        cur.execute("DELETE FROM accounts WHERE usage_percent >= ? OR status = 'EXHAUSTED'", (threshold,))
        deleted_count = cur.rowcount
        con.commit()
        con.close()
        return deleted_count

    def delete_high_usage_accounts(self, threshold: float = QUOTA_EXHAUSTION_THRESHOLD) -> int:
        """Alias cho delete_exhausted_accounts de dam bao tuong thich nguoc."""
        return self.delete_exhausted_accounts(threshold=threshold)

    def delete_single_account(self, account_id: int) -> bool:
        """Xoa 1 tai khoan cu the va xoa file cookie vat ly cua no."""
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute("SELECT file_name, email FROM accounts WHERE id = ?", (account_id,))
        row = cur.fetchone()
        if row:
            fname, email = row
            self._remove_cookie_file(fname, email)
            cur.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            con.commit()
            con.close()
            return True
        con.close()
        return False

    def trigger_immediate_auto_switch(
        self,
        reason: Optional[str] = None,
        auto_continue: Optional[bool] = None,
        spoof_hw: bool = True,
        reset_mode: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Kich hoat xoay tua tai khoan ngay lap tuc voi zero delay:
        - Danh dau tai khoan hien tai la EXHAUSTED trong DB
        - Ghi nhan su kien gian doan vao SmartTaskCompletionFilter (neu auto_continue khong bi explicitly False)
        - Spoof hardware machine IDs
        - Chuyen ngay sang candidate tot nhat tiep theo (< 100% quota)
        - Danh thuc auto_rotate_watcher ngay lap tuc
        """
        print(f"[IMMEDIATE-SWITCH] Kich hoat auto-switch khong do tre (Reason: {reason})...")
        current_active = self.storage.get_active_account()
        current_email = (current_active.get("email") or "").strip()
        current_token = current_active.get("access_token") or ""

        if reason and auto_continue is not False:
            try:
                from smart_task_filter import mark_task_interrupted
                mark_task_interrupted(reason=reason, source="immediate_switch")
            except Exception:
                pass

        if current_email or current_token:
            with DB_LOCK:
                con = sqlite3.connect(DB_FILE, timeout=30.0)
                cur = con.cursor()
                if current_email:
                    cur.execute("UPDATE accounts SET status = 'EXHAUSTED', last_checked = ? WHERE LOWER(email) = LOWER(?)", (int(time.time()), current_email))
                if current_token:
                    cur.execute("UPDATE accounts SET status = 'EXHAUSTED', last_checked = ? WHERE access_token = ?", (int(time.time()), current_token))
                con.commit()
                con.close()

        best = self.auto_switch_best_account(notify=True, prev_email=current_email, auto_continue=auto_continue, spoof_hw=spoof_hw, reset_mode=reset_mode)

        # Wake up watcher loop immediately after switch completes to inspect the new active account
        if hasattr(self, "_immediate_wake_event"):
            self._immediate_wake_event.set()

        return best

    def start_auto_rotate_watcher(self, interval_seconds: int = 5):
        """Khoi dong thread theo doi tai khoan active (chu ky 5s), neu het quota (>= 100%) hoac bi khoa chat thi tu dong doi tai khoan moi."""
        if getattr(self, "auto_rotate_enabled", False):
            return
        
        self.auto_rotate_enabled = True
        self._rotate_stop_event = threading.Event()
        self._immediate_wake_event = threading.Event()
        self.last_rotation_event = None

        def _watcher():
            while getattr(self, "auto_rotate_enabled", False) and not self._rotate_stop_event.is_set():
                try:
                    active = self.storage.get_active_account()
                    token = active.get("access_token")
                    active_email = active.get("email")
                    
                    locked = False
                    lock_reason = ""
                    usage = 0
                    spend = 0

                    if active.get("is_expired"):
                        locked = True
                        lock_reason = "Token expired (JWT exp)"

                    # Neu chua co token trong state nhung co email, thu tim access_token trong database
                    if not token and active_email:
                        con_acc = None
                        try:
                            con_acc = sqlite3.connect(DB_FILE, timeout=10.0)
                            cur_acc = con_acc.cursor()
                            cur_acc.execute("SELECT access_token FROM accounts WHERE LOWER(email) = LOWER(?)", (active_email.strip().lower(),))
                            row_tok = cur_acc.fetchone()
                            if row_tok and row_tok[0]:
                                token = row_tok[0]
                        except Exception:
                            pass
                        finally:
                            if con_acc:
                                try:
                                    con_acc.close()
                                except Exception:
                                    pass

                    if token and not locked:
                        profile = self.storage.fetch_profile_from_api(token)
                        if profile and profile.get("usage_fetched"):
                            usage = profile.get("usagePercent", 0)
                            spend = profile.get("totalSpend", 0)
                            active_email = profile.get("email") or active_email
                            http_code = profile.get("http_status")
                            locked, lock_reason = is_chat_locked(status_code=http_code, usage_data=profile, threshold=QUOTA_EXHAUSTION_THRESHOLD)
                            
                            # Cap nhat quota moi nhat truc tiep vao database moi chu ky 5 giay
                            with DB_LOCK:
                                con_live = None
                                try:
                                    con_live = sqlite3.connect(DB_FILE, timeout=10.0)
                                    cur_live = con_live.cursor()
                                    if locked:
                                        cur_live.execute("""
                                            UPDATE accounts 
                                            SET usage_percent = ?, total_spend = ?, status = 'EXHAUSTED', last_checked = ?, display_message = ?
                                            WHERE access_token = ? OR (email IS NOT NULL AND LOWER(email) = LOWER(?))
                                        """, (usage, spend, int(time.time()), profile.get("displayMessage", ""), token, (active_email or "").strip().lower()))
                                    else:
                                        cur_live.execute("""
                                            UPDATE accounts 
                                            SET usage_percent = ?, total_spend = ?, 
                                                status = CASE WHEN ? >= ? THEN 'HIGH_USAGE' ELSE 'READY' END,
                                                last_checked = ?, display_message = ?
                                            WHERE (access_token = ? OR (email IS NOT NULL AND LOWER(email) = LOWER(?)))
                                              AND status != 'EXHAUSTED'
                                        """, (usage, spend, usage, QUOTA_WARNING_THRESHOLD, int(time.time()), profile.get("displayMessage", ""), token, (active_email or "").strip().lower()))
                                    con_live.commit()
                                except Exception:
                                    pass
                                finally:
                                    if con_live:
                                        try:
                                            con_live.close()
                                        except Exception:
                                            pass

                            try:
                                self.record_quota_snapshot(
                                    account_id=None,
                                    email=active_email,
                                    usage_percent=float(usage),
                                    total_spend=float(spend),
                                    display_message=profile.get("displayMessage", ""),
                                    status="EXHAUSTED" if locked else ("HIGH_USAGE" if usage >= QUOTA_WARNING_THRESHOLD else "READY"),
                                    source="watcher"
                                )
                            except Exception:
                                pass

                    # Fallback: Kiem tra database neu API offline hoac khong the fetch
                    if not locked and active_email:
                        con_chk = None
                        try:
                            con_chk = sqlite3.connect(DB_FILE, timeout=10.0)
                            con_chk.row_factory = sqlite3.Row
                            cur_chk = con_chk.cursor()
                            cur_chk.execute("SELECT usage_percent, total_spend, status FROM accounts WHERE LOWER(email) = LOWER(?)", (active_email.strip().lower(),))
                            db_row = cur_chk.fetchone()
                            if db_row:
                                db_usage = db_row["usage_percent"] or 0
                                db_spend = db_row["total_spend"] or 0.0
                                db_status = db_row["status"]
                                if db_status == "EXHAUSTED" or db_usage >= QUOTA_EXHAUSTION_THRESHOLD:
                                    locked = True
                                    lock_reason = f"DB Quota {db_usage}% (Status: {db_status})"
                                    usage = db_usage
                                    spend = db_spend
                        except Exception:
                            pass
                        finally:
                            if con_chk:
                                try:
                                    con_chk.close()
                                except Exception:
                                    pass

                    # Neu tai khoan KHONG bi khoa va nguoi dung vua switch thu cong trong 30s, cho phep cooldown
                    if not locked and self.manual_active_account_id and (time.time() - getattr(self, "last_manual_switch_time", 0)) < 30:
                        if hasattr(self, "_immediate_wake_event"):
                            if self._immediate_wake_event.wait(timeout=interval_seconds):
                                self._immediate_wake_event.clear()
                        else:
                            self._rotate_stop_event.wait(timeout=interval_seconds)
                        continue

                    # Neu phat hien tai khoan bi khoa chat / het quota: XOAY TUA NGAY LAP TUC!
                    if locked:
                        print(f"[AUTO-ROTATE] Phat hien {active_email} da can quota / bi khoa chat ({lock_reason}). Tien hanh xoay tua...")
                        self.manual_active_account_id = None

                        # Danh dau tai khoan nay la EXHAUSTED trong DB
                        with DB_LOCK:
                            con = sqlite3.connect(DB_FILE, timeout=30.0)
                            cur = con.cursor()
                            if active_email:
                                cur.execute("""
                                    UPDATE accounts 
                                    SET status = 'EXHAUSTED', usage_percent = ?, total_spend = ?, last_checked = ?
                                    WHERE LOWER(email) = LOWER(?) OR access_token = ?
                                """, (usage, spend, int(time.time()), active_email, token or ""))
                            elif token:
                                cur.execute("""
                                    UPDATE accounts 
                                    SET status = 'EXHAUSTED', usage_percent = ?, total_spend = ?, last_checked = ?
                                    WHERE access_token = ?
                                """, (usage, spend, int(time.time()), token))
                            con.commit()
                            con.close()

                        # Ghi nhan su kien gian doan task vao SmartTaskCompletionFilter de kich hoat auto-continue
                        try:
                            from smart_task_filter import mark_task_interrupted
                            mark_task_interrupted(
                                reason=f"Auto-rotate watcher quota exhaustion: {lock_reason}",
                                source="auto_rotate_watcher"
                            )
                        except Exception as it_err:
                            print(f"[-] Loi mark_task_interrupted trong watcher: {it_err}")

                        # Chuyen sang tai khoan tot nhat tiep theo (< 100%) va gui Windows notification
                        new_best = self.auto_switch_best_account(notify=True, prev_email=active_email, spoof_hw=True)
                        if new_best:
                            new_email = new_best.get("email")
                            print(f"[AUTO-ROTATE] Da tu dong chuyen sang: {new_email} (Usage: {new_best.get('usage_percent')}%)!")
                except Exception as e:
                    pass
                
                if hasattr(self, "_immediate_wake_event"):
                    if self._immediate_wake_event.wait(timeout=interval_seconds):
                        self._immediate_wake_event.clear()
                else:
                    self._rotate_stop_event.wait(timeout=interval_seconds)

        t = threading.Thread(target=_watcher, daemon=True)
        t.start()

    def stop_auto_rotate_watcher(self):
        self.auto_rotate_enabled = False
        if hasattr(self, "_rotate_stop_event"):
            self._rotate_stop_event.set()
        if hasattr(self, "_immediate_wake_event"):
            self._immediate_wake_event.set()

    def assign_profile_to_account(self, account_id: int, profile_id: str) -> bool:
        """Gan mot Config Profile cho 1 tai khoan cu the trong database."""
        try:
            with DB_LOCK:
                con = sqlite3.connect(DB_FILE, timeout=30.0)
                cur = con.cursor()
                cur.execute("UPDATE accounts SET config_profile = ? WHERE id = ?", (profile_id, account_id))
                con.commit()
                con.close()
            print(f"[ACCOUNT-POOL] Da gan profile '{profile_id}' cho account ID {account_id}")
            return True
        except Exception as e:
            print(f"[-] Loi assign_profile_to_account: {e}")
            return False

    def apply_profile_to_all_accounts(self, profile_id: str) -> int:
        """
        Ap dung mot Config Profile cho TOAN BO tat ca cac tai khoan trong pool,
        va dong thoi cap nhat ngay vao Cursor IDE va dat lam Global Default Profile.
        """
        updated_count = 0
        try:
            with DB_LOCK:
                con = sqlite3.connect(DB_FILE, timeout=30.0)
                cur = con.cursor()
                cur.execute("UPDATE accounts SET config_profile = ?", (profile_id,))
                updated_count = cur.rowcount
                con.commit()
                con.close()

            # Dat lam global default va ap dung ngay vao Cursor
            from cursor_settings import CursorSettingsManager
            sm = CursorSettingsManager()
            sm.set_global_default_profile(profile_id)
            sm.apply_profile_to_cursor(profile_id)
            print(f"[ACCOUNT-POOL] Da ap dung profile '{profile_id}' cho {updated_count} tai khoan va dong bo Cursor!")
            return updated_count
        except Exception as e:
            print(f"[-] Loi apply_profile_to_all_accounts: {e}")
            return updated_count

    def switch_to_next_account(self, *args, **kwargs) -> Optional[Dict[str, Any]]:
        """Alias for auto_switch_best_account to support direct next-account rotations."""
        return self.auto_switch_best_account(*args, **kwargs)

    def get_account_fingerprint(self, account_id: int) -> Dict[str, Any]:
        """Lay bo hardware fingerprint gan lien voi tai khoan (tao moi neu chua co)."""
        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                cur = con.cursor()
                cur.execute("SELECT hardware_fingerprint FROM accounts WHERE id = ?", (account_id,))
                row = cur.fetchone()
                if row and row[0]:
                    try:
                        fp = json.loads(row[0])
                        if isinstance(fp, dict) and fp.get("machineId"):
                            return fp
                    except Exception:
                        pass
                
                # Chua co hoac bi loi, tao moi va luu vao DB
                from cursor_settings import CursorSettingsManager
                fp = CursorSettingsManager().generate_fingerprint()
                cur.execute("UPDATE accounts SET hardware_fingerprint = ? WHERE id = ?", (json.dumps(fp), account_id))
                con.commit()
                return fp
            finally:
                con.close()

    def regenerate_account_fingerprint(self, account_id: int) -> Dict[str, Any]:
        """Tao va cap nhat bo hardware fingerprint hoan toan moi cho tai khoan (Anti-detect Profile Refresh)."""
        from cursor_settings import CursorSettingsManager
        fp = CursorSettingsManager().generate_fingerprint()
        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            try:
                cur = con.cursor()
                cur.execute("UPDATE accounts SET hardware_fingerprint = ? WHERE id = ?", (json.dumps(fp), account_id))
                con.commit()
            finally:
                con.close()
        return fp


