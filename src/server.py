import os
import sys

# Dam bao encoding tren Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SRC_DIR)
for _p in (_SRC_DIR, _ROOT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import time
import sqlite3
import threading

from flask import Flask, render_template, jsonify, request
from account_pool import AccountPoolManager, DB_LOCK, DB_FILE
from rotating_proxy import RotatingCursorProxy
from cursor_settings import CursorSettingsManager
import cursor_reloader
from cursor_reloader import (
    get_cursor_status,
    launch_cursor_if_needed,
    send_continue_prompt,
    trigger_cursor_reload,
    hard_restart_cursor,
    terminate_cursor,
    launch_cursor_app,
    release_all_modifier_keys
)

# Concurrency & Anti-Spam Rate-Limiting Locks
_SCAN_LOCK = threading.Lock()
_LAST_SCAN_TIME = 0.0
_SWITCH_LOCK = threading.Lock()
_LAST_CONTINUE_TIME = 0.0
_CHECK_ALL_LOCK = threading.Lock()
_LAST_CHECK_ALL_TIME = 0.0
_RELOAD_LOCK = threading.Lock()
_LAST_RELOAD_TIME = 0.0
_RESTART_LOCK = threading.Lock()
_LAST_RESTART_TIME = 0.0
_SPOOF_LOCK = threading.Lock()
_LAST_SPOOF_TIME = 0.0

# System Event Audit Log (In-memory ring buffer)
_SYSTEM_EVENTS = []
_SYSTEM_EVENTS_LOCK = threading.Lock()

def _record_system_event(event_type: str, details: str):
    """Ghi nhan event bao mat va tu dong hoa vao ring buffer."""
    try:
        with _SYSTEM_EVENTS_LOCK:
            _SYSTEM_EVENTS.append({
                "timestamp": int(time.time()),
                "time_iso": time.strftime("%H:%M:%S", time.localtime()),
                "type": event_type,
                "details": details
            })
            if len(_SYSTEM_EVENTS) > 100:
                _SYSTEM_EVENTS.pop(0)
    except Exception:
        pass

# Startup safety: Giai phong toan bo modifier keys neu he thong truoc do bi ket phim
try:
    release_all_modifier_keys()
    _record_system_event("startup", "Server started, modifier keys unlatched")
except Exception:
    pass

# Keyboard Unstick Safety Watchdog: Tu dong kiem tra va go ket phim ngam
_WATCHDOG_RUNNING = True
def _keyboard_safety_watchdog():
    consecutive_stuck = 0
    while _WATCHDOG_RUNNING:
        time.sleep(4.0)
        try:
            if hasattr(cursor_reloader, "get_modifier_keys_status"):
                st = cursor_reloader.get_modifier_keys_status()
                if st.get("any_stuck"):
                    consecutive_stuck += 1
                    if consecutive_stuck >= 2:
                        cursor_reloader.release_all_modifier_keys()
                        _record_system_event("watchdog_unstick", "Watchdog auto-released stuck modifier keys (Alt/Ctrl/Win)")
                        consecutive_stuck = 0
                else:
                    consecutive_stuck = 0
        except Exception:
            pass

if "unittest" not in sys.modules and "pytest" not in sys.modules:
    threading.Thread(target=_keyboard_safety_watchdog, daemon=True).start()

app = Flask(__name__, template_folder=os.path.join(_ROOT_DIR, "templates"))
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

@app.after_request
def add_cache_busting_headers(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

pool = AccountPoolManager(cookies_dir=os.path.join(_ROOT_DIR, "Cookies"))
settings_manager = CursorSettingsManager(workspace_dir=_ROOT_DIR)
proxy_instance = RotatingCursorProxy(
    proxy_host=os.getenv("CURSOR_PROXY_HOST", "127.0.0.1"),
    proxy_port=int(os.getenv("CURSOR_PROXY_PORT", "8999")),
    target_backend_url=os.getenv("CURSOR_BACKEND_URL", "https://api2.cursor.sh")
)


# Khi khoi dong server, tu dong dong bo tai khoan dang active tu Cursor va kich hoat Auto-Rotate Watcher
try:
    pool.sync_active_from_cursor()
    if "unittest" not in sys.modules and "pytest" not in sys.modules:
        pool.start_auto_rotate_watcher(interval_seconds=5)
        print("[*] Da kich hoat Auto-Rotate Watcher ngam (chu ky 5s)!")
        pool.start_startup_quota_sync(max_workers=5)
        print("[*] Da kich hoat Startup Quota Sync ngam (5 luong kiem tra quota)!")
        try:
            proxy_instance.start_in_thread()
            print(f"[*] Da tu dong khoi dong Rotating Proxy tren port {proxy_instance.proxy_port}!")
        except Exception as pe:
            print(f"[-] Loi khoi dong proxy: {pe}")
except Exception as e:
    print(f"[*] Khoi tao sync active / watcher / startup sync: {e}")

SERVER_START_TIME = time.time()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/time")
def api_time():
    """Lay thoi gian thuc va uptime cua he thong phuc vu real-time HUD va native app."""
    now_ts = int(time.time())
    return jsonify({
        "success": True,
        "server_time": now_ts,
        "server_time_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
        "uptime_seconds": int(now_ts - SERVER_START_TIME)
    })

@app.route("/api/quota/snapshots")
def api_quota_snapshots():
    """Lay danh sach snapshot quota gan nhat cua active account hoac email."""
    try:
        limit = min(int(request.args.get("limit", 20)), 100)
        email = request.args.get("email")
        con = sqlite3.connect(DB_FILE, timeout=10.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        if email:
            cur.execute("""
                SELECT * FROM quota_snapshots 
                WHERE LOWER(email) = LOWER(?) 
                ORDER BY recorded_at DESC, id DESC LIMIT ?
            """, (email.strip().lower(), limit))
        else:
            cur.execute("""
                SELECT * FROM quota_snapshots 
                ORDER BY recorded_at DESC, id DESC LIMIT ?
            """, (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        con.close()
        return jsonify({"success": True, "snapshots": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/status")
def api_status():
    """Lay trang thai toan dien: Cursor app running, window, active account, model, auto-switch config va persisted quota."""
    try:
        c_status = get_cursor_status()
        active = pool.sync_active_from_cursor(force_fetch_api=False, allow_remote_fetch=False)
        if not active:
            active = pool.storage.get_active_account()

        if isinstance(active, dict):
            active["cursor_running"] = c_status.get("is_running", False)
            active["cursor_window"] = c_status.get("has_window", False)
            active["cursor_title"] = c_status.get("window_title", "")
            active["cursor_exe"] = c_status.get("exe_path", "")
            
            c_settings = settings_manager.get_settings()
            active["cursor_model"] = c_settings.get("default_model", "default")
            active["is_proxy_routed"] = c_settings.get("is_proxy_routed", False)
            active["auto_switch_config"] = settings_manager.get_auto_switch_config()

            # Persisted Quota Guarantee:
            # Truy xuat quota da duoc persist tu DB neu active thieu hoac bi 0 (khi remote API chua tra ve live usage_fetched)
            email = (active.get("email") or "").strip().lower()
            token = active.get("access_token")
            if not active.get("usage_fetched"):
                persisted = pool.get_persisted_account_quota(email=email, token=token)
                if persisted:
                    if active.get("usagePercent") is None or (active.get("usagePercent") == 0 and persisted.get("usage_percent", 0) > 0):
                        active["usagePercent"] = persisted["usage_percent"]
                    if active.get("totalSpend") is None or (active.get("totalSpend") == 0 and persisted.get("total_spend", 0) > 0):
                        active["totalSpend"] = persisted["total_spend"]
                    if not active.get("displayMessage") and persisted.get("display_message"):
                        active["displayMessage"] = persisted["display_message"]
                    if not active.get("status") or active.get("status") == "PENDING":
                        active["status"] = persisted.get("status", "READY")
                    if persisted.get("last_checked"):
                        active["last_checked"] = persisted["last_checked"]

            # Dong bo ca 2 format snake_case va camelCase
            active["usage_percent"] = active.get("usagePercent", 0.0) or 0.0
            active["total_spend"] = active.get("totalSpend", 0.0) or 0.0

            # Real-time synchronization metrics
            now_ts = int(time.time())
            active["server_time"] = now_ts
            active["server_time_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts))
            active["uptime_seconds"] = int(now_ts - SERVER_START_TIME)
            active["last_sync"] = active.get("last_checked") or now_ts
        else:
            now_ts = int(time.time())
            active = {
                "cursor_running": c_status.get("is_running", False),
                "cursor_window": c_status.get("has_window", False),
                "cursor_title": c_status.get("window_title", ""),
                "cursor_exe": c_status.get("exe_path", ""),
                "email": None,
                "usage_percent": 0.0,
                "usagePercent": 0.0,
                "total_spend": 0.0,
                "totalSpend": 0.0,
                "server_time": now_ts,
                "server_time_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
                "uptime_seconds": int(now_ts - SERVER_START_TIME),
                "last_sync": now_ts
            }

        active["startup_sync"] = pool.get_startup_sync_progress()
        return jsonify(active)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/accounts")
def api_accounts():
    """Lay toan bo danh sach tai khoan trong pool voi masked_token an toan."""
    try:
        accounts = pool.get_all_accounts()
        for a in accounts:
            tok = a.get("access_token")
            if tok and isinstance(tok, str) and len(tok) > 12:
                a["masked_token"] = f"{tok[:6]}...{tok[-4:]}"
            else:
                a["masked_token"] = "None"
            # Strip dangerous raw full cookies and refresh tokens from public account list
            a.pop("cookie_full", None)
            a.pop("refresh_token", None)
        return jsonify(accounts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/accounts/<int:account_id>/token", methods=["GET", "POST"])
def api_account_token(account_id):
    """Lay access token cua tai khoan phuc vu nut Copy Clipboard an toan."""
    try:
        with DB_LOCK:
            con = sqlite3.connect(DB_FILE, timeout=10.0)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("SELECT access_token, email FROM accounts WHERE id = ?", (account_id,))
            row = cur.fetchone()
            con.close()
        if not row or not row["access_token"]:
            return jsonify({"success": False, "error": "Không tìm thấy token"}), 404
        from token_cipher import decrypt_token
        plain = decrypt_token(row["access_token"])
        return jsonify({
            "success": True,
            "account_id": account_id,
            "email": row["email"],
            "token": plain
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/unstick-keys", methods=["GET", "POST"])
@app.route("/api/keyboard/unstick", methods=["GET", "POST"])
def api_unstick_keys():
    """Giai phong toan bo cac modifier keys (Alt, Ctrl, Shift, Win) bi ket tren Windows."""
    try:
        before = cursor_reloader.get_modifier_keys_status() if hasattr(cursor_reloader, "get_modifier_keys_status") else {}
        ok = cursor_reloader.release_all_modifier_keys()
        after = cursor_reloader.get_modifier_keys_status() if hasattr(cursor_reloader, "get_modifier_keys_status") else {}
        _record_system_event("keyboard_unstick", "Manual key release executed")
        return jsonify({
            "success": ok,
            "message": "Đã giải phóng thành công toàn bộ phím kẹt (Alt, Ctrl, Shift, Win)!",
            "before_status": before,
            "current_status": after
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/keyboard/status", methods=["GET"])
def api_keyboard_status():
    """Lay trang thai de/ket phim modifier thuc te tren he dieu hanh Windows."""
    try:
        st = cursor_reloader.get_modifier_keys_status() if hasattr(cursor_reloader, "get_modifier_keys_status") else {}
        typing_active, idle_sec = cursor_reloader.is_user_actively_typing() if hasattr(cursor_reloader, "is_user_actively_typing") else (False, 999.0)
        return jsonify({
            "success": True,
            "status": st,
            "user_typing_active": typing_active,
            "idle_seconds": round(idle_sec, 2)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/debug/system-info", methods=["GET"])
def api_debug_system_info():
    """Tong hop toan dien thong tin debug, chan doan he thong, tien trinh va bao mat."""
    try:
        c_status = get_cursor_status()
        kb_status = cursor_reloader.get_modifier_keys_status() if hasattr(cursor_reloader, "get_modifier_keys_status") else {}
        typing_active, idle_sec = cursor_reloader.is_user_actively_typing() if hasattr(cursor_reloader, "is_user_actively_typing") else (False, 999.0)
        active = pool.storage.get_active_account()
        
        proc_mem = None
        proc_pid = None
        if c_status.get("is_running"):
            try:
                import psutil
                for p in psutil.process_iter(['pid', 'name', 'memory_info']):
                    if p.info['name'] and p.info['name'].lower() == 'cursor.exe':
                        proc_pid = p.info['pid']
                        mem = p.info.get('memory_info')
                        if mem:
                            proc_mem = round(mem.rss / (1024 * 1024), 1)
                        break
            except Exception:
                pass

        accounts = pool.get_all_accounts()
        total_accounts = len(accounts)
        ready_count = sum(1 for a in accounts if (a.get("usage_percent") or 0) < pool.QUOTA_EXHAUSTION_THRESHOLD and a.get("status") in ("READY", "HIGH_USAGE"))
        exhausted_count = sum(1 for a in accounts if (a.get("usage_percent") or 0) >= pool.QUOTA_EXHAUSTION_THRESHOLD or a.get("status") == "EXHAUSTED")

        return jsonify({
            "success": True,
            "timestamp": int(time.time()),
            "uptime_seconds": int(time.time() - SERVER_START_TIME),
            "keyboard": {
                "modifiers": kb_status,
                "is_stuck": kb_status.get("any_stuck", False),
                "user_actively_typing": typing_active,
                "user_idle_seconds": round(idle_sec, 2),
                "watchdog_active": _WATCHDOG_RUNNING
            },
            "cursor": {
                "is_running": c_status.get("is_running", False),
                "has_window": c_status.get("has_window", False),
                "hwnd": c_status.get("hwnd"),
                "window_title": c_status.get("window_title", ""),
                "exe_path": c_status.get("exe_path", ""),
                "pid": proc_pid,
                "memory_mb": proc_mem
            },
            "chat_lock": {
                "active_email": active.get("email"),
                "has_token": bool(active.get("has_token")),
                "is_expired": bool(active.get("is_expired")),
                "token_exp": active.get("token_exp"),
                "auto_rotate_enabled": getattr(pool, "auto_rotate_enabled", False),
                "threshold": pool.QUOTA_EXHAUSTION_THRESHOLD
            },
            "pool_stats": {
                "total": total_accounts,
                "ready": ready_count,
                "exhausted": exhausted_count
            },
            "security": {
                "tokens_encrypted_at_rest": True,
                "token_masking": True,
                "anti_spam_locks": True,
                "state_vscdb_readonly_uri": True
            },
            "recent_events": _SYSTEM_EVENTS[-15:]
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/stats")
def api_stats():
    """Lay thong ke dashboard: Tong so, San sang (<100%), Het Quota / Can doi (>=100%), Pending, Two-Tier & Blacklist."""
    try:
        accounts = pool.get_all_accounts()
        threshold = pool.QUOTA_EXHAUSTION_THRESHOLD
        tier1_th = pool.TIER1_THRESHOLD
        total = len(accounts)
        ready = sum(1 for a in accounts if a.get("status") in ("READY", "HIGH_USAGE") and (a.get("usage_percent") or 0) < threshold)
        tier1 = sum(1 for a in accounts if a.get("status") in ("READY", "HIGH_USAGE") and (a.get("usage_percent") or 0) < tier1_th and not a.get("in_cooldown"))
        tier2 = sum(1 for a in accounts if a.get("status") in ("READY", "HIGH_USAGE") and tier1_th <= (a.get("usage_percent") or 0) < threshold and not a.get("in_cooldown"))
        blacklist = sum(1 for a in accounts if a.get("in_cooldown") or (a.get("status") not in ("EXPIRED", "PENDING") and (a.get("status") == "EXHAUSTED" or (a.get("usage_percent") or 0) >= threshold)))
        exhausted = sum(1 for a in accounts if a.get("status") not in ("EXPIRED", "PENDING") and (a.get("status") == "EXHAUSTED" or (a.get("usage_percent") or 0) >= threshold))
        pending = sum(1 for a in accounts if a.get("status") == "PENDING")
        expired = sum(1 for a in accounts if a.get("status") == "EXPIRED")
        return jsonify({
            "total": total,
            "ready": ready,
            "tier1": tier1,
            "tier1_count": tier1,
            "tier2": tier2,
            "tier2_count": tier2,
            "blacklist": blacklist,
            "blacklist_count": blacklist,
            "cooldown_count": blacklist,
            "exhausted": exhausted,
            "pending": pending,
            "expired": expired,
            "threshold": threshold,
            "tier1_threshold": tier1_th
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/cookies/open-folder", methods=["GET", "POST"])
def api_cookies_open_folder():
    """Mo thu muc Cookies trong Windows Explorer hoac file manager he thong."""
    try:
        cdir = pool.cookies_dir
        os.makedirs(cdir, exist_ok=True)
        abs_path = os.path.abspath(cdir)
        
        opened = False
        if sys.platform == "win32" or os.name == "nt":
            try:
                os.startfile(abs_path)
                opened = True
            except Exception:
                import subprocess
                subprocess.Popen(["explorer.exe", abs_path])
                opened = True
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", abs_path])
            opened = True
        else:
            import subprocess
            subprocess.Popen(["xdg-open", abs_path])
            opened = True

        _record_system_event("cookies_open_folder", f"Opened cookies directory: {abs_path}")
        return jsonify({
            "success": True,
            "path": abs_path,
            "opened": opened,
            "message": f"Đã mở thư mục Cookies: {abs_path}"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cookies/info", methods=["GET"])
def api_cookies_info():
    """Lay thong tin thu muc Cookies (duong dan, so luong file, tong so accounts va trang thai quota)."""
    try:
        cdir = pool.cookies_dir
        if not os.path.exists(cdir):
            os.makedirs(cdir, exist_ok=True)
        files = [f for f in os.listdir(cdir) if (f.lower().endswith(".txt") or f.lower().endswith(".cookie")) and os.path.isfile(os.path.join(cdir, f))]
        all_accounts = pool.get_all_accounts()
        valid_tokens = sum(1 for a in all_accounts if a.get("has_token") or a.get("access_token"))
        
        # Quota status breakdown
        ready_count = sum(1 for a in all_accounts if (a.get("status") in ("READY", "HIGH_USAGE")) and (a.get("usage_percent", 0) or 0) < 100)
        tier1_count = sum(1 for a in all_accounts if (a.get("status") in ("READY", "HIGH_USAGE")) and (a.get("usage_percent", 0) or 0) < 50)
        tier2_count = sum(1 for a in all_accounts if (a.get("status") in ("READY", "HIGH_USAGE")) and 50 <= (a.get("usage_percent", 0) or 0) < 100)
        exhausted_count = sum(1 for a in all_accounts if a.get("status") == "EXHAUSTED" or (a.get("usage_percent", 0) or 0) >= 100)
        pending_count = sum(1 for a in all_accounts if a.get("status") == "PENDING")
        arch_dir = os.path.join(cdir, "Archive_Corrupted")
        archived_count = len([f for f in os.listdir(arch_dir) if os.path.isfile(os.path.join(arch_dir, f))]) if os.path.exists(arch_dir) else 0

        return jsonify({
            "success": True,
            "path": os.path.abspath(cdir),
            "count": len(files),
            "total_accounts": len(all_accounts),
            "valid_tokens": valid_tokens,
            "ready_count": ready_count,
            "tier1_count": tier1_count,
            "tier2_count": tier2_count,
            "exhausted_count": exhausted_count,
            "pending_count": pending_count,
            "archived_count": archived_count,
            "files": files[:30]
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cookies/quarantine", methods=["POST"])
def api_cookies_quarantine():
    """Tu dong cach ly cac file cookie hong / 0 byte / expired vao Cookies/Archive_Corrupted."""
    try:
        count = pool.quarantine_corrupted_cookies()
        _record_system_event("cookies_quarantine", f"Quarantined {count} corrupted/expired cookies into Archive_Corrupted")
        return jsonify({
            "success": True,
            "quarantined": count,
            "message": f"Đã cách ly {count} file cookie hỏng vào Cookies/Archive_Corrupted!"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/ai/predict-quota", methods=["GET", "POST"])
def api_ai_predict_quota():
    """Du doan toc do dot quota (EMA burn velocity) va thoi gian can quota TTE cho tai khoan active hoac chi dinh."""
    try:
        acc_id = request.args.get("id") or (request.get_json(silent=True) or {}).get("id")
        email = request.args.get("email") or (request.get_json(silent=True) or {}).get("email")
        if not acc_id and not email:
            active = pool.storage.get_active_account()
            email = active.get("email")
        
        pred = pool.predict_account_exhaustion(account_id=int(acc_id) if acc_id else None, email=email)
        return jsonify({
            "success": True,
            "target": email or acc_id or "active",
            "prediction": pred
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cookies/convert", methods=["POST"])
def api_cookies_convert():
    """1-Click Convert Cookies to Tokens: Quet va exchange toan bo sang Tokens voi 12 luong song song."""
    try:
        scanned = pool.scan_cookies_folder()
        started = pool.start_startup_quota_sync(max_workers=12, force=True)
        _record_system_event("cookie_convert", f"1-Click convert initiated. Scanned: {scanned}, workers: 12")
        all_accounts = pool.get_all_accounts()
        return jsonify({
            "success": True,
            "scanned": scanned,
            "sync_started": started,
            "total_accounts": len(all_accounts),
            "message": f"Đã quét {scanned} cookies và kích hoạt tiến trình nạp token với 12 luồng song song!"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cookies/upload", methods=["POST"])
def api_cookies_upload():
    """Nhan file cookie tu giao dien Dropzone, luu vao thu muc Cookies va tu dong convert."""
    try:
        if "file" not in request.files and "files" not in request.files:
            return jsonify({"success": False, "error": "Không tìm thấy file tải lên"}), 400
        
        uploaded_files = request.files.getlist("file") or request.files.getlist("files")
        saved_count = 0
        cdir = pool.cookies_dir
        os.makedirs(cdir, exist_ok=True)
        
        for f in uploaded_files:
            fname = os.path.basename(f.filename)
            if not fname:
                continue
            if not fname.endswith(".txt"):
                fname = f"{fname}.txt"
            target_path = os.path.join(cdir, fname)
            f.save(target_path)
            saved_count += 1
            
        scanned = pool.scan_cookies_folder()
        pool.start_startup_quota_sync(max_workers=12, force=True)
        return jsonify({
            "success": True,
            "saved": saved_count,
            "scanned": scanned,
            "message": f"Đã lưu {saved_count} file và khởi chạy tiến trình nạp token 12 luồng!"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Quet thu muc Cookies de nap them tai khoan voi tinh nang chong spam va debounce an toan."""
    global _LAST_SCAN_TIME
    now = time.time()
    if now - _LAST_SCAN_TIME < 1.5:
        return jsonify({"success": True, "added": 0, "throttled": True, "message": "Quét đang được xử lý hoặc vừa hoàn tất"})

    if not _SCAN_LOCK.acquire(blocking=False):
        return jsonify({"success": True, "added": 0, "throttled": True, "message": "Tiến trình quét đang chạy ngầm"})

    try:
        count = pool.scan_cookies_folder()
        proxy_instance.pool.load_cache_from_db()
        _LAST_SCAN_TIME = time.time()
        return jsonify({"success": True, "added": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _SCAN_LOCK.release()

@app.route("/api/refresh/<int:account_id>", methods=["POST"])
def api_refresh(account_id):
    """Cap nhat quota va profile cho 1 tai khoan tu API Cursor."""
    try:
        profile = pool.refresh_account_quota(account_id)
        if profile:
            return jsonify({"success": True, "profile": profile})
        else:
            return jsonify({"success": False, "error": "Khong the cap nhat (Token hoac Cookie khong hop le)"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/switch/<int:account_id>", methods=["POST"])
def api_switch(account_id):
    """Nap tai khoan account_id vao Cursor state.vscdb, reload/restart app va ho tro auto continue."""
    acquired = _SWITCH_LOCK.acquire(timeout=10.0)
    if not acquired:
        return jsonify({"success": False, "error": "Hệ thống đang bận chuyển đổi tài khoản khác. Vui lòng thử lại sau giây lát."}), 429
    try:
        data = request.get_json(silent=True) or {}
        auto_reload = request.args.get("reload", "true").lower() in ("true", "1")
        if "reload" in data:
            auto_reload = bool(data["reload"])
        auto_cont = None
        if "auto_continue" in request.args:
            auto_cont = request.args.get("auto_continue").lower() in ("true", "1")
        if "auto_continue" in data:
            auto_cont = bool(data["auto_continue"])
        reset_mode = request.args.get("reset_mode") or data.get("reset_mode")

        success = pool.switch_to_account(account_id, auto_reload=auto_reload, auto_continue=auto_cont, reset_mode=reset_mode)
        if success:
            active = pool.sync_active_from_cursor() or pool.storage.get_active_account()
            return jsonify({"success": True, "active": active})
        else:
            return jsonify({"success": False, "error": "Loi khi nap vao Cursor SQLite"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _SWITCH_LOCK.release()

@app.route("/api/auto-switch", methods=["POST"])
def api_auto_switch():
    """Tu dong tim tai khoan co quota tot nhat va nap vao Cursor."""
    acquired = _SWITCH_LOCK.acquire(timeout=10.0)
    if not acquired:
        return jsonify({"success": False, "error": "Hệ thống đang bận chuyển đổi tài khoản khác. Vui lòng thử lại sau giây lát."}), 429
    try:
        data = request.get_json(silent=True) or {}
        auto_cont = data.get("auto_continue")
        if auto_cont is None and "auto_continue" in request.args:
            auto_cont = request.args.get("auto_continue").lower() in ("true", "1")
        reset_mode = request.args.get("reset_mode") or data.get("reset_mode")

        best = pool.auto_switch_best_account(auto_continue=auto_cont, reset_mode=reset_mode)
        if best:
            active = pool.sync_active_from_cursor() or pool.storage.get_active_account()
            return jsonify({"success": True, "account": best, "active": active})
        else:
            return jsonify({"success": False, "error": "Khong con tai khoan kha dung nao con quota"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _SWITCH_LOCK.release()

@app.route("/api/rotate", methods=["POST"])
def api_rotate():
    """Xoay tua ngay sang tai khoan tiep theo. Neu can, danh dau tai khoan hien tai la EXHAUSTED va spoof Hardware ID."""
    acquired = _SWITCH_LOCK.acquire(timeout=10.0)
    if not acquired:
        return jsonify({"success": False, "error": "Hệ thống đang bận chuyển đổi tài khoản khác. Vui lòng thử lại sau giây lát."}), 429
    try:
        data = request.get_json(silent=True) or {}
        mark_current = data.get("mark_current_exhausted", True)
        reset_mode = request.args.get("reset_mode") or data.get("reset_mode")
        
        current_active = pool.storage.get_active_account()
        current_email = (current_active.get("email") or "").strip()
        
        if mark_current and current_email:
            with DB_LOCK:
                con = sqlite3.connect(DB_FILE, timeout=10.0)
                cur = con.cursor()
                cur.execute("UPDATE accounts SET status = 'EXHAUSTED', last_checked = ? WHERE LOWER(email) = LOWER(?)", (int(time.time()), current_email))
                con.commit()
                con.close()
            print(f"[FORCE-ROTATE] Da danh dau {current_email} la EXHAUSTED")

        # Tu dong spoof Hardware ID de tranh rate limit cap may tu Cursor
        try:
            settings_manager.spoof_storage_ids()
        except Exception as sp_err:
            print(f"[-] Loi spoof hardware IDs: {sp_err}")

        best = pool.auto_switch_best_account(notify=True, prev_email=current_email, reset_mode=reset_mode)
        if best:
            active = pool.sync_active_from_cursor() or pool.storage.get_active_account()
            return jsonify({"success": True, "account": best, "active": active})
        else:
            return jsonify({"success": False, "error": "Khong con tai khoan kha dung nao con quota"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _SWITCH_LOCK.release()

@app.route("/api/auto-switch/immediate", methods=["POST"])
def api_auto_switch_immediate():
    """Kich hoat auto-switch ngay lap tuc voi zero delay khi quota can hoac chat lock."""
    acquired = _SWITCH_LOCK.acquire(timeout=10.0)
    if not acquired:
        return jsonify({"success": False, "error": "Hệ thống đang bận chuyển đổi tài khoản khác. Vui lòng thử lại sau giây lát."}), 429
    try:
        data = request.get_json(silent=True) or {}
        reason = data.get("reason", "Triggered via API /api/auto-switch/immediate")
        auto_cont = data.get("auto_continue")
        if auto_cont is None and "auto_continue" in request.args:
            auto_cont = request.args.get("auto_continue").lower() in ("true", "1")
        spoof_hw = data.get("spoof_hw", True)
        reset_mode = request.args.get("reset_mode") or data.get("reset_mode")
        best = pool.trigger_immediate_auto_switch(reason=reason, auto_continue=auto_cont, spoof_hw=spoof_hw, reset_mode=reset_mode)
        if best:
            active = pool.sync_active_from_cursor() or pool.storage.get_active_account()
            return jsonify({"success": True, "account": best, "active": active})
        else:
            return jsonify({"success": False, "error": "Khong con tai khoan kha dung nao con quota"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _SWITCH_LOCK.release()

@app.route("/api/task/filter-status", methods=["GET"])
def api_task_filter_status():
    """Lay trang thai hien tai cua Smart Task Completion Filter."""
    try:
        from smart_task_filter import SmartTaskCompletionFilter
        f = SmartTaskCompletionFilter()
        status = f.get_latest_task_status()
        should_cont, reason = f.should_auto_continue()
        return jsonify({
            "success": True,
            "status": status,
            "should_auto_continue": should_cont,
            "decision_reason": reason
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/task/filter-mark", methods=["POST"])
def api_task_filter_mark():
    """Danh dau trang thai gian doan hoac hoan thanh cho Smart Task Completion Filter."""
    try:
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        from smart_task_filter import mark_task_interrupted, mark_task_completed, TaskStateTracker
        if action == "interrupted":
            reason = data.get("reason", "Marked via API")
            req_id = data.get("request_id")
            mark_task_interrupted(reason=reason, request_id=req_id, source="api")
            return jsonify({"success": True, "action": "interrupted", "reason": reason})
        elif action == "completed":
            req_id = data.get("request_id")
            mark_task_completed(request_id=req_id, source="api")
            return jsonify({"success": True, "action": "completed"})
        elif action == "reset":
            TaskStateTracker().reset()
            return jsonify({"success": True, "action": "reset"})
        else:
            return jsonify({"success": False, "error": "Invalid action. Use 'interrupted', 'completed', or 'reset'."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auto-rotate/status", methods=["GET"])
def api_auto_rotate_status():
    """Lay trang thai che do Auto-Rotate theo doi tu dong."""
    return jsonify({
        "enabled": getattr(pool, "auto_rotate_enabled", False),
        "last_event": getattr(pool, "last_rotation_event", None),
        "threshold": pool.QUOTA_EXHAUSTION_THRESHOLD
    })

@app.route("/api/auto-rotate/toggle", methods=["POST"])
def api_auto_rotate_toggle():
    """Bat / Tat che do tu dong xoay tua khi tai khoan can quota."""
    current = getattr(pool, "auto_rotate_enabled", False)
    if current:
        pool.stop_auto_rotate_watcher()
    else:
        pool.start_auto_rotate_watcher(interval_seconds=5)
    return jsonify({"enabled": getattr(pool, "auto_rotate_enabled", False)})

@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Gui thong bao test toi Windows notification banner."""
    try:
        from windows_notifier import send_windows_notification
        data = request.get_json() or {}
        title = data.get("title", "Cursor Manager")
        msg = data.get("message", "Thử nghiệm thông báo Windows Native thành công! 🚀")
        ok = send_windows_notification(title, msg, async_exec=False)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/proxy/status", methods=["GET"])
def api_proxy_status():
    """Lay trang thai Rotating Proxy server."""
    try:
        running = proxy_instance.is_running()
        stats = proxy_instance.pool.get_stats()
        return jsonify({
            "running": running,
            "host": proxy_instance.proxy_host,
            "port": proxy_instance.proxy_port,
            "target_backend_url": proxy_instance.target_backend_url,
            "lookup_strategy": proxy_instance.lookup_strategy,
            "midstream_strategy": proxy_instance.midstream_strategy,
            "max_swap_retries": proxy_instance.max_swap_retries,
            "pool_stats": stats
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/proxy/toggle", methods=["POST"])
def api_proxy_toggle():
    """Bat / Tat Rotating Proxy server."""
    try:
        if proxy_instance.is_running():
            proxy_instance.stop_thread()
            return jsonify({
                "success": True,
                "running": False,
                "message": f"Da dung Rotating Proxy tren cong {proxy_instance.proxy_port}"
            })
        else:
            started = proxy_instance.start_in_thread()
            return jsonify({
                "success": started,
                "running": proxy_instance.is_running(),
                "port": proxy_instance.proxy_port,
                "message": f"Da khoi dong Rotating Proxy tren http://{proxy_instance.proxy_host}:{proxy_instance.proxy_port}"
            })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/proxy/config", methods=["POST"])
def api_proxy_config():
    """Cap nhat cau hinh Rotating Proxy."""
    try:
        data = request.get_json() or {}
        if "lookup_strategy" in data:
            proxy_instance.set_lookup_strategy(data["lookup_strategy"])
        if "midstream_strategy" in data:
            proxy_instance.set_midstream_strategy(data["midstream_strategy"])
        if "max_swap_retries" in data:
            proxy_instance.max_swap_retries = int(data["max_swap_retries"])
        return jsonify({
            "success": True,
            "lookup_strategy": proxy_instance.lookup_strategy,
            "midstream_strategy": proxy_instance.midstream_strategy,
            "max_swap_retries": proxy_instance.max_swap_retries
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/proxy/reload", methods=["POST"])
def api_proxy_reload():
    """Tai lai cache tai khoan cua Rotating Proxy tu SQLite cursor_accounts.db."""
    try:
        count = proxy_instance.pool.load_cache_from_db()
        return jsonify({
            "success": True,
            "cached_accounts": count,
            "pool_stats": proxy_instance.pool.get_stats()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/proxy/metrics", methods=["GET"])
def api_proxy_metrics():
    """Lay telemetry metrics gan nhat cua Rotating Proxy."""
    try:
        return jsonify({
            "metrics": proxy_instance.metrics_log[-100:],
            "count": len(proxy_instance.metrics_log)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/reload-window", methods=["POST"])
def api_reload_window():
    """Tu dong kich hoat va reload hoac reset cua so Cursor IDE, dong thoi focus lai chat."""
    global _LAST_RELOAD_TIME
    now = time.time()
    if now - _LAST_RELOAD_TIME < 1.5:
        return jsonify({"success": False, "error": "Vui lòng chờ ít nhất 1.5s giữa các lần reload cửa sổ", "throttled": True}), 429
    _LAST_RELOAD_TIME = now

    acquired = _RELOAD_LOCK.acquire(timeout=5.0)
    if not acquired:
        return jsonify({"success": False, "error": "Tiến trình reload đang bận", "throttled": True}), 429
    try:
        data = request.get_json(silent=True) or {}
        mode = request.args.get("mode") or data.get("mode")
        if mode == "hard_restart":
            res = cursor_reloader.hard_restart_cursor(wait_for_window=True)
            _record_system_event("hard_restart", "Hard reset Cursor IDE requested")
        else:
            res = cursor_reloader.trigger_cursor_reload(auto_focus=True)
            _record_system_event("reload_window", "Window reload requested")
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        _RELOAD_LOCK.release()

@app.route("/api/check-all", methods=["POST"])
def api_check_all():
    """Khoi dong tien trinh kiem tra quota toan bo tai khoan trong pool co chong spam."""
    global _LAST_CHECK_ALL_TIME
    now = time.time()
    if now - _LAST_CHECK_ALL_TIME < 2.5:
        return jsonify({"success": False, "message": "Thao tác quá nhanh. Vui lòng chờ 2.5s.", "throttled": True}), 429
    _LAST_CHECK_ALL_TIME = now

    if not _CHECK_ALL_LOCK.acquire(blocking=False):
        return jsonify({"success": False, "message": "Tiến trình kiểm tra đang chạy", "throttled": True}), 429
    try:
        started = pool.start_batch_check()
        if started:
            _record_system_event("check_all_start", "Batch check all accounts started")
            return jsonify({"success": True, "message": "Da khoi dong kiem tra toan bo tai khoan"})
        else:
            return jsonify({"success": False, "message": "Tien trinh dang chay roi"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _CHECK_ALL_LOCK.release()

@app.route("/api/check-all/progress", methods=["GET"])
def api_check_all_progress():
    """Lay tien do kiem tra toan bo tai khoan."""
    try:
        progress = pool.get_batch_progress()
        return jsonify(progress)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/check-all/stop", methods=["POST"])
def api_check_all_stop():
    """Dung tien trinh kiem tra toan bo tai khoan."""
    try:
        stopped = pool.stop_batch_check()
        return jsonify({"success": stopped})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/startup-sync/status", methods=["GET"])
def api_startup_sync_status():
    """Lay tien do va thong tin luong kiem tra quota khoi dong 5 workers."""
    try:
        progress = pool.get_startup_sync_progress()
        return jsonify({"success": True, "startup_sync": progress})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/startup-sync/start", methods=["POST"])
def api_startup_sync_start():
    """Kich hoat thu cong tien trinh dong bo quota 5 workers."""
    try:
        data = request.get_json(silent=True) or {}
        max_workers = int(data.get("max_workers", 5))
        force = bool(data.get("force", False))
        started = pool.start_startup_quota_sync(max_workers=max_workers, force=force)
        return jsonify({
            "success": started,
            "message": "Đã bắt đầu kiểm tra quota khởi động (5 luồng)!" if started else "Tiến trình đồng bộ đang chạy!",
            "startup_sync": pool.get_startup_sync_progress()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/accounts/clean-invalid", methods=["POST"])
def api_clean_invalid():
    """Xoa toan bo cac tai khoan co status EXPIRED khoi pool."""
    try:
        deleted = pool.delete_invalid_accounts()
        proxy_instance.pool.load_cache_from_db()
        return jsonify({"success": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/accounts/clean-high-usage", methods=["POST"])
@app.route("/api/accounts/clean-exhausted", methods=["POST"])
def api_clean_high_usage():
    """Xoa toan bo cac tai khoan co quota da dung >= QUOTA_EXHAUSTION_THRESHOLD (50%) hoac het quota."""
    try:
        deleted = pool.delete_exhausted_accounts(threshold=pool.QUOTA_EXHAUSTION_THRESHOLD)
        proxy_instance.pool.load_cache_from_db()
        return jsonify({"success": True, "deleted": deleted, "threshold": pool.QUOTA_EXHAUSTION_THRESHOLD})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    """Xoa 1 tai khoan va file cookie tuong ung khoi thu muc Cookies."""
    try:
        ok = pool.delete_single_account(account_id)
        proxy_instance.pool.load_cache_from_db()
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================================================================
# Cursor Settings, Hardware Fingerprint & .cursorrules Endpoints
# =========================================================================

@app.route("/api/cursor/settings", methods=["GET", "POST"])
def api_cursor_settings():
    """Doc hoac cap nhat settings.json cua Cursor (model preference, telemetry, privacy)."""
    try:
        if request.method == "POST":
            updates = request.get_json() or {}
            res = settings_manager.update_settings(updates)
            return jsonify({"success": True, "settings": res})
        else:
            res = settings_manager.get_settings()
            res["success"] = True
            return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/storage-ids", methods=["GET"])
def api_cursor_storage_ids():
    """Doc Hardware IDs / Fingerprint tu storage.json."""
    try:
        res = settings_manager.get_storage_ids()
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/cursor/storage-ids/spoof", methods=["POST"])
def api_cursor_storage_ids_spoof():
    """Randomize / Spoof toan bo Hardware Machine IDs trong storage.json co chong spam."""
    global _LAST_SPOOF_TIME
    now = time.time()
    if now - _LAST_SPOOF_TIME < 2.0:
        return jsonify({"success": False, "error": "Vui lòng chờ giữa các lần spoof ID", "throttled": True}), 429
    _LAST_SPOOF_TIME = now

    acquired = _SPOOF_LOCK.acquire(timeout=5.0)
    if not acquired:
        return jsonify({"success": False, "error": "Tiến trình spoof đang bận", "throttled": True}), 429
    try:
        res = settings_manager.spoof_storage_ids()
        _record_system_event("spoof_hw", "Hardware IDs randomized")
        return jsonify({"success": True, "storage_ids": res})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _SPOOF_LOCK.release()

@app.route("/api/cursor/storage-ids/restore", methods=["POST"])
def api_cursor_storage_ids_restore():
    """Khoi phuc Hardware IDs tu file backup storage.json.bak."""
    try:
        ok = settings_manager.restore_storage_ids()
        if ok:
            res = settings_manager.get_storage_ids()
            return jsonify({"success": True, "storage_ids": res})
        else:
            return jsonify({"success": False, "error": "Khong tim thay file backup storage.json.bak"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================================================================
# Anti-Detect Fingerprint Isolation APIs
# =========================================================================
@app.route("/api/antidetect/config", methods=["GET", "POST"])
def api_antidetect_config():
    """Doc hoac cap nhat che do Anti-Detect Fingerprint Isolation."""
    try:
        if request.method == "POST":
            data = request.get_json() or {}
            mode = data.get("antidetect_mode")
            if mode not in ("account_locked", "stealth_randomize", "native_standard"):
                return jsonify({"success": False, "error": "Invalid antidetect_mode. Allowed: account_locked, stealth_randomize, native_standard"}), 400
            updated = settings_manager.update_auto_switch_config({"antidetect_mode": mode})
            _record_system_event("antidetect_config", f"Anti-detect mode set to: {mode}")
            return jsonify({
                "success": True,
                "antidetect_mode": updated.get("antidetect_mode", "account_locked"),
                "config": updated
            })
        else:
            cfg = settings_manager.get_auto_switch_config()
            storage_ids = settings_manager.get_storage_ids()
            return jsonify({
                "success": True,
                "antidetect_mode": cfg.get("antidetect_mode", "account_locked"),
                "allowed_modes": [
                    {"id": "account_locked", "name": "Khóa Vân Tay Theo Acc (Anti-Detect Browser)", "description": "Mỗi tài khoản sở hữu 1 Hardware Fingerprint vĩnh viễn riêng biệt, khôi phục khi switch"},
                    {"id": "stealth_randomize", "name": "Ngẫu Nhiên Liên Tục (Stealth Randomize)", "description": "Randomize toàn bộ Machine ID mỗi lần đổi tài khoản để tránh liên kết"},
                    {"id": "native_standard", "name": "Mặc Định Máy Thật (Native Standard)", "description": "Giữ nguyên Hardware Fingerprint gốc của máy tính"}
                ],
                "current_fingerprint": storage_ids
            })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/antidetect/account/<int:account_id>", methods=["GET"])
def api_antidetect_get_account(account_id: int):
    """Lay Hardware Fingerprint profile cua 1 tai khoan cu the."""
    try:
        fp = pool.get_account_fingerprint(account_id)
        return jsonify({"success": True, "account_id": account_id, "fingerprint": fp})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/antidetect/regenerate/<int:account_id>", methods=["POST"])
def api_antidetect_regenerate(account_id: int):
    """Tao bo Hardware Fingerprint hoan toan moi cho 1 tai khoan."""
    try:
        fp = pool.regenerate_account_fingerprint(account_id)
        _record_system_event("antidetect_regen", f"Regenerated fingerprint for account #{account_id}")
        return jsonify({"success": True, "account_id": account_id, "fingerprint": fp})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/antidetect/apply/<int:account_id>", methods=["POST"])
def api_antidetect_apply(account_id: int):
    """Ap dung ngay Hardware Fingerprint cua tai khoan vao Cursor storage.json."""
    try:
        fp = pool.get_account_fingerprint(account_id)
        if not fp:
            return jsonify({"success": False, "error": "Khong tim thay fingerprint cho account"}), 404
        ok = settings_manager.apply_fingerprint(fp)
        if ok:
            _record_system_event("antidetect_apply", f"Applied fingerprint for account #{account_id}")
            return jsonify({"success": True, "account_id": account_id, "fingerprint": fp})
        return jsonify({"success": False, "error": "Khong the ghi storage.json"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/cursorrules", methods=["GET", "POST"])
def api_cursor_cursorrules():
    """Doc hoac luu file .cursorrules truc tiep tu dashboard."""
    try:
        if request.method == "POST":
            data = request.get_json() or {}
            content = data.get("content", "")
            path = data.get("path")
            res = settings_manager.save_cursorrules(content, custom_path=path)
            return jsonify(res)
        else:
            path = request.args.get("path")
            res = settings_manager.get_cursorrules(custom_path=path)
            return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/cursor/network-test", methods=["GET"])
def api_cursor_network_test():
    """Kiem tra ket noi mang toi upstream Cursor API (https://api2.cursor.sh) va proxy :8999."""
    import time
    backend_url = os.getenv("CURSOR_BACKEND_URL", "https://api2.cursor.sh")
    start_t = time.time()
    upstream_ok = False
    upstream_status = None
    upstream_latency_ms = None
    error_msg = None
    try:
        r = requests.get(backend_url, timeout=5)
        upstream_status = r.status_code
        upstream_latency_ms = round((time.time() - start_t) * 1000, 1)
        upstream_ok = True
    except Exception as e:
        error_msg = str(e)
        upstream_latency_ms = round((time.time() - start_t) * 1000, 1)

    proxy_running = bool(proxy_instance.is_running if hasattr(proxy_instance, 'is_running') else True)

    return jsonify({
        "success": True,
        "backend_url": backend_url,
        "upstream_ok": upstream_ok,
        "upstream_status": upstream_status,
        "latency_ms": upstream_latency_ms,
        "proxy_running": proxy_running,
        "proxy_port": 8999,
        "error": error_msg
    })

# =========================================================================
# Config Profiles & Multi-Account Preset Endpoints
# =========================================================================

@app.route("/api/cursor/profiles", methods=["GET"])
def api_cursor_profiles():
    """Lay toan bo danh sach ho so cau hinh, profile active va global default."""
    try:
        data = settings_manager.get_all_profiles()
        # Thong ke so luong tai khoan dang su dung tung profile
        accounts = pool.get_all_accounts()
        profile_counts = {}
        for acc in accounts:
            p = acc.get("config_profile") or "default"
            profile_counts[p] = profile_counts.get(p, 0) + 1
        data["profile_counts"] = profile_counts
        data["total_accounts"] = len(accounts)
        return jsonify({
            "success": True,
            "data": data,
            "profiles": data.get("profiles", {}),
            "active_profile": data.get("active_profile", "default"),
            "global_default_profile": data.get("global_default_profile", "default"),
            "accounts_distribution": profile_counts,
            "total_accounts": len(accounts)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/profiles/save", methods=["POST"])
def api_cursor_profiles_save():
    """Tao moi hoac cap nhat mot profile cau hinh."""
    try:
        body = request.get_json() or {}
        profile_id = body.get("profile_id") or body.get("id") or body.get("name", "").lower().replace(" ", "_")
        res = settings_manager.save_profile(profile_id, body)
        return jsonify({"success": True, "profile": res})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/profiles/delete", methods=["POST"])
def api_cursor_profiles_delete():
    """Xoa mot profile cau hinh (khong cho phep xoa 'default')."""
    try:
        body = request.get_json() or {}
        profile_id = body.get("profile_id") or body.get("id")
        if not profile_id or profile_id in ("default", "agent_turbo", "cost_saver", "privacy_stealth"):
            return jsonify({"success": False, "error": "Không thể xóa hồ sơ mặc định của hệ thống"}), 400
        ok = settings_manager.delete_profile(profile_id)
        return jsonify({"success": ok, "profile_id": profile_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/profiles/apply-current", methods=["POST"])
def api_cursor_profiles_apply_current():
    """Ap dung mot profile vao Cursor hien tai."""
    try:
        body = request.get_json() or {}
        profile_id = body.get("profile_id") or body.get("id") or "default"
        res = settings_manager.apply_profile_to_cursor(profile_id)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/profiles/apply-all", methods=["POST"])
def api_cursor_profiles_apply_all():
    """Ap dung mot profile cho TOAN BO tai khoan trong pool va Cursor hien tai."""
    try:
        body = request.get_json() or {}
        profile_id = body.get("profile_id") or body.get("id") or "default"
        updated_count = pool.apply_profile_to_all_accounts(profile_id)
        return jsonify({
            "success": True,
            "profile_id": profile_id,
            "updated_count": updated_count,
            "message": f"Đã áp dụng thành công hồ sơ '{profile_id}' cho toàn bộ {updated_count} tài khoản!"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/profiles/assign-account", methods=["POST"])
def api_cursor_profiles_assign_account():
    """Gan profile cho mot tai khoan cu the theo ID."""
    try:
        body = request.get_json() or {}
        account_id = body.get("account_id")
        profile_id = body.get("profile_id") or body.get("id") or "default"
        if not account_id:
            return jsonify({"success": False, "error": "Thiếu account_id"}), 400
        ok = pool.assign_profile_to_account(int(account_id), profile_id)
        return jsonify({"success": ok, "account_id": account_id, "profile_id": profile_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/profiles/export", methods=["GET"])
def api_cursor_profiles_export():
    """Xuat danh sach profiles ra JSON."""
    try:
        data = settings_manager.export_profiles()
        return jsonify({"success": True, "export": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/profiles/import", methods=["POST"])
def api_cursor_profiles_import():
    """Nhap danh sach profiles tu JSON."""
    try:
        body = request.get_json() or {}
        res = settings_manager.import_profiles(body)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/app-status", methods=["GET"])
def api_cursor_app_status():
    """Kiem tra trang thai tien trinh va cua so ung dung Cursor."""
    try:
        from cursor_reloader import get_cursor_status
        status = get_cursor_status()
        active = pool.storage.get_active_account()
        settings = settings_manager.get_settings()
        return jsonify({
            "success": True,
            "cursor": status,
            "active_account": active,
            "default_model": settings.get("default_model", "default"),
            "is_proxy_routed": settings.get("is_proxy_routed", False)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/launch", methods=["POST"])
def api_cursor_launch():
    """Khoi dong Cursor.exe neu chua bat."""
    try:
        from cursor_reloader import launch_cursor_if_needed, get_cursor_status
        launched = launch_cursor_if_needed()
        status = get_cursor_status()
        return jsonify({
            "success": launched or status["is_running"],
            "status": status,
            "message": "Đã khởi động Cursor thành công" if launched else ("Cursor đang chạy rồi" if status["is_running"] else "Không thể khởi động Cursor")
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/hard-restart", methods=["POST"])
@app.route("/api/cursor/restart", methods=["POST"])
def api_cursor_hard_restart():
    """Tat hoan toan va khoi dong lai sach se ung dung Cursor (Hard App Reset)."""
    global _LAST_RESTART_TIME
    now = time.time()
    if now - _LAST_RESTART_TIME < 2.5:
        return jsonify({
            "success": True,
            "throttled": True,
            "method": "hard_restart",
            "message": "Cursor vừa được khởi động lại gần đây (vui lòng chờ 2.5s giữa các lần reset)"
        }), 200
    _LAST_RESTART_TIME = now

    acquired = _RESTART_LOCK.acquire(timeout=8.0)
    if not acquired:
        return jsonify({"success": False, "error": "Tiến trình hard restart đang bận", "throttled": True}), 429
    try:
        _record_system_event("hard_restart", "Hard restart Cursor IDE initiated")
        res = cursor_reloader.hard_restart_cursor(wait_for_window=True)
        _record_system_event("hard_restart_complete", f"Hard restart finished (success={res.get('success')})")
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        _RESTART_LOCK.release()

@app.route("/api/cursor/send-continue", methods=["POST"])
def api_cursor_send_continue():
    """Tu dong gui prompt tiep tuc vao Composer/Chat cua Cursor (co chong spam rate-limiting)."""
    global _LAST_CONTINUE_TIME
    now = time.time()
    if now - _LAST_CONTINUE_TIME < 1.0:
        return jsonify({"success": False, "error": "Vui lòng chờ ít nhất 1 giây giữa các lần gửi tiếp tục", "rate_limited": True, "throttled": True}), 429
    _LAST_CONTINUE_TIME = now
    try:
        from cursor_reloader import send_continue_prompt
        data = request.get_json(silent=True) or {}
        cfg = settings_manager.get_auto_switch_config()
        prompt = data.get("prompt") or cfg.get("continue_prompt", "Tiếp tục")
        target_mode = data.get("target_mode") or cfg.get("target_mode", "composer")
        res = send_continue_prompt(prompt_text=prompt, target_composer=(target_mode == "composer"))
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/auto-switch-config", methods=["GET", "POST"])
def api_cursor_auto_switch_config():
    """Doc hoac cap nhat cau hinh tu dong chuyen doi va gui tin nhan tiep tuc."""
    try:
        if request.method == "POST":
            updates = request.get_json(silent=True) or {}
            cfg = settings_manager.update_auto_switch_config(updates)
            # Dong bo trang thai watcher neu co yeu cau
            if "auto_rotate_enabled" in updates:
                if updates["auto_rotate_enabled"]:
                    pool.start_auto_rotate_watcher(interval_seconds=5)
                else:
                    pool.stop_auto_rotate_watcher()
            return jsonify({"success": True, "config": cfg})
        else:
            cfg = settings_manager.get_auto_switch_config()
            cfg["watcher_running"] = getattr(pool, "auto_rotate_enabled", False)
            return jsonify({"success": True, "config": cfg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# =========================================================================
# AUTO-UPDATER API ENDPOINTS
# =========================================================================

@app.route("/api/updater/status", methods=["GET"])
def api_updater_status():
    """Lay trang thai hien tai cua he thong auto-updater."""
    try:
        from updater import get_updater
        updater = get_updater()
        return jsonify({"success": True, **updater.get_status()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/updater/check", methods=["POST"])
def api_updater_check():
    """Kiem tra xem co ban cap nhat moi tu remote repository hay khong."""
    try:
        from updater import get_updater
        updater = get_updater()
        info = updater.check_for_updates()
        status = updater.get_status()
        return jsonify({
            "success": True,
            "has_update": bool(info and info.update_available),
            **status
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/updater/download", methods=["POST"])
def api_updater_download():
    """Tai goi cap nhat va kiem tra toan ven SHA-256."""
    try:
        from updater import get_updater
        updater = get_updater()
        info = updater.last_info
        if not info or not info.update_available:
            info = updater.check_for_updates()
        if not info or not info.update_available:
            return jsonify({"success": False, "error": "Khong co ban cap nhat nao de tai"}), 400

        staged_path = updater.download_update(info)
        status = updater.get_status()
        if staged_path:
            return jsonify({"success": True, "staged_path": staged_path, **status})
        else:
            return jsonify({"success": False, "error": updater.last_error, **status}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/updater/apply", methods=["POST"])
def api_updater_apply():
    """Khoi dong tien trinh cap nhat ngam va khoi dong lai ung dung an toan."""
    try:
        from updater import get_updater
        updater = get_updater()
        res = updater.apply_update()
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



if __name__ == "__main__":
    port = 7860
    print(f"\n========================================================")
    print(f"  🚀 CURSOR MANAGER HOST DANG CHAY TAI:")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
