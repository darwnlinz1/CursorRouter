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
import json
import queue
import atexit
import sqlite3
import threading
import requests

from flask import Flask, render_template, jsonify, request, Response
from app_paths import get_templates_dir, get_static_dir, get_data_dir
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

# Lightweight in-process SSE EventBroker for sub-50ms UI pushes
class _EventBroker:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list = []

    def subscribe(self) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue(maxsize=32)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[str]") -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event_type: str, data: dict) -> None:
        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass


event_broker = _EventBroker()

# Keyboard Unstick Safety Watchdog state (started explicitly via start_background_services)
_WATCHDOG_RUNNING = False
_SERVICES_LOCK = threading.Lock()
_SERVICES_STARTED = False
_PROXY_RELOAD_TIMER = None


def _keyboard_safety_watchdog():
    consecutive_stuck = 0
    while _WATCHDOG_RUNNING:
        time.sleep(4.0)
        if not _WATCHDOG_RUNNING:
            break
        try:
            if hasattr(cursor_reloader, "get_modifier_keys_status"):
                st = cursor_reloader.get_modifier_keys_status()
                if st.get("any_stuck"):
                    consecutive_stuck += 1
                    if consecutive_stuck >= 2:
                        cursor_reloader.release_all_modifier_keys()
                        _record_system_event("watchdog_unstick", "Watchdog auto-released stuck modifier keys (Alt/Ctrl/Win)")
                        event_broker.publish("keyboard_alert", {"stuck_released": True})
                        consecutive_stuck = 0
                else:
                    consecutive_stuck = 0
        except Exception:
            pass


app = Flask(
    __name__,
    template_folder=get_templates_dir(),
    static_folder=get_static_dir(),
    static_url_path="/static",
)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

@app.after_request
def add_cache_busting_headers(response):
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=300"
    else:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

pool = AccountPoolManager()
settings_manager = CursorSettingsManager(workspace_dir=get_data_dir())
proxy_instance = RotatingCursorProxy(
    proxy_host=os.getenv("CURSOR_PROXY_HOST", "127.0.0.1"),
    proxy_port=int(os.getenv("CURSOR_PROXY_PORT", "8999")),
    target_backend_url=os.getenv("CURSOR_BACKEND_URL", "https://api2.cursor.sh")
)


def _schedule_proxy_cache_reload():
    global _PROXY_RELOAD_TIMER
    try:
        if _PROXY_RELOAD_TIMER is not None:
            _PROXY_RELOAD_TIMER.cancel()
        t = threading.Timer(0.25, proxy_instance.pool.load_cache_from_db)
        t.daemon = True
        _PROXY_RELOAD_TIMER = t
        t.start()
    except Exception:
        pass


def _on_pool_changed(event_type: str, data: dict):
    _schedule_proxy_cache_reload()
    event_broker.publish(event_type, data)


pool.add_change_listener(_on_pool_changed)
proxy_instance.pool.on_token_exhausted = lambda info: event_broker.publish("proxy_exhausted", info)


def start_background_services(run_startup_sync: bool = True) -> None:
    """Explicitly start background daemon services (idempotent; zero import-time side effects)."""
    global _SERVICES_STARTED, _WATCHDOG_RUNNING
    with _SERVICES_LOCK:
        if _SERVICES_STARTED:
            return
        _SERVICES_STARTED = True
        _WATCHDOG_RUNNING = True

    try:
        release_all_modifier_keys()
        _record_system_event("startup", "CursorRouter v3.0 started, modifier keys unlatched")
    except Exception:
        pass

    threading.Thread(target=_keyboard_safety_watchdog, daemon=True, name="KeyboardSafetyWatchdog").start()

    try:
        pool.sync_active_from_cursor(force_fetch_api=False, allow_remote_fetch=False)
        cfg = settings_manager.get_auto_switch_config()
        if cfg.get("auto_rotate_enabled", True):
            pool.start_auto_rotate_watcher(interval_seconds=5)
            print("[*] Da kich hoat Auto-Rotate Watcher ngam (chu ky 5s)!")
        if run_startup_sync:
            pool.start_startup_quota_sync(max_workers=12)
            print("[*] Da kich hoat Startup Quota Sync ngam (HTTP/2 Async, 12 concurrency)!")
        try:
            proxy_instance.start_in_thread()
            print(f"[*] Da tu dong khoi dong Rotating Proxy tren port {proxy_instance.proxy_port}!")
        except Exception as pe:
            print(f"[-] Loi khoi dong proxy: {pe}")
    except Exception as e:
        print(f"[*] Khoi tao sync active / watcher / startup sync: {e}")


def stop_background_services() -> None:
    """Cleanly stop all background threads and release modifier keys."""
    global _SERVICES_STARTED, _WATCHDOG_RUNNING
    with _SERVICES_LOCK:
        _WATCHDOG_RUNNING = False
        _SERVICES_STARTED = False
    try:
        pool.stop_auto_rotate_watcher()
    except Exception:
        pass
    try:
        pool.stop_startup_quota_sync()
    except Exception:
        pass
    try:
        proxy_instance.stop_thread()
    except Exception:
        pass
    try:
        release_all_modifier_keys()
    except Exception:
        pass


atexit.register(stop_background_services)

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

@app.route("/api/events")
def api_events():
    """Server-Sent Events (SSE) stream pushing real-time pool, sync, proxy, and keyboard deltas."""
    def _stream():
        q = event_broker.subscribe()
        try:
            yield f"event: connected\ndata: {json.dumps({'version': pool.state_version, 'server_time': int(time.time())})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15.0)
                    yield msg
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            event_broker.unsubscribe(q)

    return Response(
        _stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _build_status_payload() -> dict:
    c_status = get_cursor_status(use_cache=True)
    active = pool.sync_active_from_cursor(force_fetch_api=False, allow_remote_fetch=False)
    if not active:
        active = pool.storage.get_active_account()

    if isinstance(active, dict):
        active = dict(active)
        active["cursor_running"] = c_status.get("is_running", False)
        active["cursor_window"] = c_status.get("has_window", False)
        active["cursor_title"] = c_status.get("window_title", "")
        active["cursor_exe"] = c_status.get("exe_path", "")

        c_settings = settings_manager.get_settings()
        active["cursor_model"] = c_settings.get("default_model", "default")
        active["is_proxy_routed"] = c_settings.get("is_proxy_routed", False)
        active["auto_switch_config"] = settings_manager.get_auto_switch_config()

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

        active["usagePercent"] = active.get("usagePercent", 0.0) or 0.0
        active["totalSpend"] = active.get("totalSpend", 0.0) or 0.0
        active["usage_percent"] = active["usagePercent"]
        active["total_spend"] = active["totalSpend"]

        now_ts = int(time.time())
        active["server_time"] = now_ts
        active["server_time_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts))
        active["uptime_seconds"] = int(now_ts - SERVER_START_TIME)
        active["last_sync"] = active.get("last_checked") or now_ts
        active["has_token"] = bool(active.get("has_token") or token)
        # Never leak raw JWTs over status endpoint
        active.pop("access_token", None)
        active.pop("refresh_token", None)
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
            "last_sync": now_ts,
        }

    active["startup_sync"] = pool.get_startup_sync_progress()
    return active


@app.route("/api/dashboard/snapshot", methods=["GET"])
def api_dashboard_snapshot():
    """Single lightweight endpoint replacing multi-endpoint 2.5s polling."""
    try:
        since_ver = request.args.get("since_version", type=int)
        include_accounts = request.args.get("include_accounts", "0").lower() in ("1", "true", "yes")
        status_payload = _build_status_payload()
        kb_status = cursor_reloader.get_modifier_keys_status() if hasattr(cursor_reloader, "get_modifier_keys_status") else {}

        payload = {
            "success": True,
            "version": pool.state_version,
            "server_time": status_payload["server_time"],
            "server_time_iso": status_payload["server_time_iso"],
            "uptime_seconds": status_payload["uptime_seconds"],
            "status": status_payload,
            "stats": pool.get_stats_summary(),
            "auto_rotate": {
                "enabled": getattr(pool, "auto_rotate_enabled", False),
                "last_event": getattr(pool, "last_rotation_event", None),
                "threshold": pool.QUOTA_EXHAUSTION_THRESHOLD,
            },
            "proxy": {
                "running": bool(proxy_instance.is_running()),
                "host": proxy_instance.proxy_host,
                "port": proxy_instance.proxy_port,
                "lookup_strategy": proxy_instance.lookup_strategy,
                "midstream_strategy": proxy_instance.midstream_strategy,
                "pool_stats": proxy_instance.pool.get_stats(),
            },
            "keyboard": kb_status,
            "startup_sync": status_payload.get("startup_sync"),
            "batch_progress": pool.get_batch_progress(),
        }
        if include_accounts and (since_ver is None or since_ver != pool.state_version):
            payload["accounts"] = pool.get_all_accounts(include_secrets=False)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status")
def api_status():
    """Lay trang thai toan dien: Cursor app running, window, active account, model, auto-switch config va persisted quota."""
    try:
        return jsonify(_build_status_payload())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/accounts")
def api_accounts():
    """Lay toan bo danh sach tai khoan trong pool voi masked_token an toan (khong lo raw access_token)."""
    try:
        accounts = pool.get_all_accounts(include_secrets=False)
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

        accounts = pool.get_all_accounts(include_secrets=False)
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
    """Lay thong ke dashboard phan tach khong chong cheo (fast non-decrypting read)."""
    try:
        try:
            accounts = pool.get_all_accounts(include_secrets=False)
        except TypeError:
            accounts = pool.get_all_accounts()
        now_ts = int(time.time())
        threshold = pool.QUOTA_EXHAUSTION_THRESHOLD
        warn = pool.QUOTA_WARNING_THRESHOLD
        total = len(accounts)
        tier1 = tier2 = ready = exhausted = pending = expired = blacklist = 0
        for a in accounts:
            st = (a.get("status") or "PENDING").upper()
            usage = float(a.get("usage_percent") or 0.0)
            cd = int(a.get("cooldown_until") or 0)
            if st == "EXPIRED":
                expired += 1
            elif st == "PENDING":
                pending += 1
            elif st == "EXHAUSTED" or usage >= threshold or cd > now_ts:
                exhausted += 1
                blacklist += 1
            else:
                ready += 1
                if usage >= warn:
                    tier2 += 1
                else:
                    tier1 += 1
        return jsonify({
            "total": total,
            "ready": ready,
            "tier1": tier1,
            "tier2": tier2,
            "tier1_count": tier1,
            "tier2_count": tier2,
            "high_usage": exhausted,
            "exhausted": exhausted,
            "blacklist": blacklist,
            "blacklist_count": blacklist,
            "cooldown_count": blacklist,
            "pending": pending,
            "expired": expired,
            "threshold": threshold,
            "warning_threshold": warn,
            "tier1_threshold": warn,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/pool/summary", methods=["GET"])
def api_pool_summary():
    """Return complete OAuth session pool telemetry (tier breakdown, valid IDE sessions, status counts)."""
    try:
        all_accounts = pool.get_all_accounts(include_secrets=False)
        now_ts = int(time.time())
        valid_tokens = sum(1 for a in all_accounts if a.get("has_token"))
        session_tokens = sum(1 for a in all_accounts if a.get("has_token") and a.get("token_type") != "web")
        ready_count = sum(
            1 for a in all_accounts
            if a.get("status") in ("READY", "HIGH_USAGE")
            and (a.get("usage_percent", 0) or 0) < 100
            and int(a.get("cooldown_until") or 0) <= now_ts
        )
        tier1_count = sum(
            1 for a in all_accounts
            if a.get("status") in ("READY", "HIGH_USAGE")
            and (a.get("usage_percent", 0) or 0) < 50
            and int(a.get("cooldown_until") or 0) <= now_ts
        )
        tier2_count = sum(
            1 for a in all_accounts
            if a.get("status") in ("READY", "HIGH_USAGE")
            and 50 <= (a.get("usage_percent", 0) or 0) < 100
            and int(a.get("cooldown_until") or 0) <= now_ts
        )
        exhausted_count = sum(
            1 for a in all_accounts
            if a.get("status") == "EXHAUSTED"
            or (a.get("usage_percent", 0) or 0) >= 100
            or int(a.get("cooldown_until") or 0) > now_ts
        )
        pending_count = sum(1 for a in all_accounts if a.get("status") == "PENDING")
        expired_count = sum(1 for a in all_accounts if a.get("status") == "EXPIRED")

        return jsonify({
            "success": True,
            "auth_mode": "oauth_pkce",
            "total_accounts": len(all_accounts),
            "valid_tokens": valid_tokens,
            "session_tokens": session_tokens,
            "ready_count": ready_count,
            "tier1_count": tier1_count,
            "tier2_count": tier2_count,
            "exhausted_count": exhausted_count,
            "pending_count": pending_count,
            "expired_count": expired_count,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/accounts/sync-all", methods=["POST"])
def api_accounts_sync_all():
    """Trigger fast HTTP/2 multiplexed quota sync across all OAuth sessions in the pool."""
    try:
        started = pool.start_startup_quota_sync(max_workers=12, force=True)
        _record_system_event("oauth_sync_all", "Triggered HTTP/2 quota sync across OAuth pool")
        all_accounts = pool.get_all_accounts(include_secrets=False)
        return jsonify({
            "success": True,
            "sync_started": started,
            "total_accounts": len(all_accounts),
            "message": f"Đã kích hoạt đồng bộ HTTP/2 cho {len(all_accounts)} phiên OAuth!",
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


_PKCE_SESSIONS = {}
_PKCE_SESSIONS_LOCK = threading.Lock()


def _pkce_poll_worker(session_id: str, flow_uuid: str, verifier: str):
    """Background poller for official interactive Cursor OAuth PKCE desktop sign-in."""
    poll_url = f"https://api2.cursor.sh/auth/poll?uuid={flow_uuid}&verifier={verifier}"
    deadline = time.time() + 240.0  # 4 minutes
    while time.time() < deadline:
        with _PKCE_SESSIONS_LOCK:
            sess = _PKCE_SESSIONS.get(session_id)
            if not sess or sess.get("status") != "waiting":
                return
        try:
            resp = requests.get(poll_url, headers={"Content-Type": "application/json"}, timeout=8)
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                if isinstance(data, dict) and data.get("accessToken"):
                    saved = pool.upsert_session_account(
                        access_token=data["accessToken"],
                        refresh_token=data.get("refreshToken"),
                        source="oauth_pkce",
                    )
                    with _PKCE_SESSIONS_LOCK:
                        sess = _PKCE_SESSIONS.get(session_id)
                        if sess:
                            if saved.get("success"):
                                sess["status"] = "completed"
                                sess["account"] = saved
                                sess["message"] = saved.get("message")
                            else:
                                sess["status"] = "error"
                                sess["error"] = saved.get("error", "Không thể lưu tài khoản")
                    _record_system_event("oauth_pkce_success", f"Added IDE session via OAuth: {saved.get('email')}")
                    return
            elif resp.status_code == 403:
                with _PKCE_SESSIONS_LOCK:
                    sess = _PKCE_SESSIONS.get(session_id)
                    if sess:
                        sess["status"] = "error"
                        sess["error"] = "Phiên OAuth bị từ chối hoặc đã hết hạn (403)."
                return
        except Exception:
            pass
        time.sleep(1.5)

    with _PKCE_SESSIONS_LOCK:
        sess = _PKCE_SESSIONS.get(session_id)
        if sess and sess.get("status") == "waiting":
            sess["status"] = "expired"
            sess["error"] = "Hết thời gian chờ đăng nhập (4 phút). Vui lòng tạo link mới."


@app.route("/api/auth/pkce/start", methods=["POST"])
def api_pkce_start():
    """
    Start an official interactive Cursor Desktop OAuth PKCE flow.
    Opens/returns https://cursor.com/loginDeepControl?challenge=...&uuid=...&mode=login
    and polls api2.cursor.sh/auth/poll in background to obtain a genuine 60-day IDE `type: "session"` token.
    """
    try:
        from pkce_auth import generate_pkce_credentials
        import webbrowser

        data = request.get_json(silent=True) or {}
        open_browser = bool(data.get("open_browser", True))
        pkce = generate_pkce_credentials()
        session_id = pkce["uuid"]
        login_url = f"https://cursor.com/loginDeepControl?challenge={pkce['challenge']}&uuid={pkce['uuid']}&mode=login"

        with _PKCE_SESSIONS_LOCK:
            # Prune expired entries
            now = time.time()
            stale = [k for k, v in _PKCE_SESSIONS.items() if now - v.get("created_at", 0) > 600]
            for k in stale:
                _PKCE_SESSIONS.pop(k, None)
            _PKCE_SESSIONS[session_id] = {
                "session_id": session_id,
                "login_url": login_url,
                "status": "waiting",
                "created_at": now,
                "account": None,
                "error": None,
            }

        t = threading.Thread(
            target=_pkce_poll_worker,
            args=(session_id, pkce["uuid"], pkce["verifier"]),
            daemon=True,
            name=f"PKCEPoll-{session_id[:8]}",
        )
        t.start()

        opened = False
        if open_browser:
            try:
                webbrowser.open(login_url)
                opened = True
            except Exception:
                opened = False

        _record_system_event("oauth_pkce_start", f"Started interactive OAuth PKCE session {session_id[:8]}")
        return jsonify({
            "success": True,
            "session_id": session_id,
            "login_url": login_url,
            "opened_browser": opened,
            "status": "waiting",
            "message": "Đã tạo link đăng nhập Cursor OAuth! Hãy xác nhận trên trình duyệt (hoặc mở tab Ẩn danh để thêm tài khoản khác).",
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/auth/pkce/status/<session_id>", methods=["GET"])
def api_pkce_status(session_id):
    with _PKCE_SESSIONS_LOCK:
        sess = _PKCE_SESSIONS.get(session_id)
        if not sess:
            return jsonify({"success": False, "status": "not_found", "error": "Không tìm thấy phiên OAuth"}), 404
        return jsonify({"success": True, **sess})


@app.route("/api/auth/pkce/cancel/<session_id>", methods=["POST"])
def api_pkce_cancel(session_id):
    with _PKCE_SESSIONS_LOCK:
        sess = _PKCE_SESSIONS.get(session_id)
        if sess and sess.get("status") == "waiting":
            sess["status"] = "cancelled"
    return jsonify({"success": True, "status": "cancelled"})


@app.route("/api/accounts/capture-active", methods=["POST"])
def api_capture_active_session():
    """Capture the currently signed-in Cursor IDE desktop session token into the encrypted pool."""
    try:
        res = pool.capture_active_cursor_session()
        if res.get("success"):
            _record_system_event("capture_session", f"Captured {res.get('email')} ({res.get('token_type')})")
            return jsonify(res)
        return jsonify(res), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/auth/pkce/sessions", methods=["GET"])
def api_pkce_sessions():
    """List active or recent interactive OAuth PKCE sessions."""
    with _PKCE_SESSIONS_LOCK:
        items = list(_PKCE_SESSIONS.values())
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return jsonify({"success": True, "sessions": items[:10]})


@app.route("/api/accounts/import-token", methods=["POST"])
def api_import_oauth_token():
    """Import or restore a Cursor OAuth session token directly (for cross-machine backup restore)."""
    try:
        data = request.get_json(silent=True) or {}
        access_token = (data.get("access_token") or data.get("token") or "").strip()
        refresh_token = (data.get("refresh_token") or "").strip() or None
        if not access_token:
            return jsonify({"success": False, "error": "Thiếu OAuth access_token"}), 400
        res = pool.upsert_session_account(
            access_token=access_token,
            refresh_token=refresh_token,
            source="oauth_import",
        )
        if res.get("success"):
            return jsonify(res)
        return jsonify(res), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/refresh/<int:account_id>", methods=["POST"])
def api_refresh(account_id):
    """Cap nhat quota va profile cho 1 tai khoan tu API Cursor."""
    try:
        profile = pool.refresh_account_quota(account_id)
        if profile:
            return jsonify({"success": True, "profile": profile})
        else:
            return jsonify({"success": False, "error": "Phiên OAuth không hợp lệ hoặc đã hết hạn — vui lòng đăng nhập lại bằng OAuth"}), 400
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
            tok_type = (active or {}).get("token_type") or "session"
            warning = None
            if tok_type == "web":
                warning = (
                    "Phiên này không phải Desktop OAuth Session. Hãy đăng nhập lại bằng nút '+ Đăng Nhập Thêm Acc (OAuth)'."
                )
            return jsonify({"success": True, "active": active, "token_type": tok_type, "warning": warning})
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
            now_ts = int(time.time())
            cd_until = now_ts + pool.BLACKLIST_COOLDOWN_SECONDS
            with DB_LOCK:
                con = sqlite3.connect(DB_FILE, timeout=10.0)
                try:
                    cur = con.cursor()
                    cur.execute("PRAGMA table_info(accounts);")
                    cols = {r[1] for r in cur.fetchall()}
                    if "cooldown_until" in cols:
                        cur.execute("UPDATE accounts SET status = 'EXHAUSTED', cooldown_until = ?, last_checked = ? WHERE LOWER(email) = LOWER(?)", (cd_until, now_ts, current_email))
                    else:
                        cur.execute("UPDATE accounts SET status = 'EXHAUSTED', last_checked = ? WHERE LOWER(email) = LOWER(?)", (now_ts, current_email))
                    con.commit()
                finally:
                    con.close()
            print(f"[FORCE-ROTATE] Da danh dau {current_email} la EXHAUSTED")

        # Anti-detect fingerprint duoc xu ly thong nhat ben trong switch_to_account theo antidetect_mode
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
        data = request.get_json(silent=True) or {}
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
        data = request.get_json(silent=True) or {}
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
    """Xoa 1 phien tai khoan OAuth khoi SQLite pool."""
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
            updates = request.get_json(silent=True) or {}
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
            data = request.get_json(silent=True) or {}
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
            data = request.get_json(silent=True) or {}
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

    proxy_running = bool(proxy_instance.is_running() if callable(getattr(proxy_instance, 'is_running', None)) else False)

    return jsonify({
        "success": True,
        "backend_url": backend_url,
        "upstream_ok": upstream_ok,
        "upstream_status": upstream_status,
        "latency_ms": upstream_latency_ms,
        "proxy_running": proxy_running,
        "proxy_port": proxy_instance.proxy_port,
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
        accounts = pool.get_all_accounts(include_secrets=False)
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
        body = request.get_json(silent=True) or {}
        profile_id = body.get("profile_id") or body.get("id") or body.get("name", "").lower().replace(" ", "_")
        res = settings_manager.save_profile(profile_id, body)
        return jsonify({"success": True, "profile": res})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/profiles/delete", methods=["POST"])
def api_cursor_profiles_delete():
    """Xoa mot profile cau hinh (khong cho phep xoa 'default')."""
    try:
        body = request.get_json(silent=True) or {}
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
        body = request.get_json(silent=True) or {}
        profile_id = body.get("profile_id") or body.get("id") or "default"
        res = settings_manager.apply_profile_to_cursor(profile_id)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/profiles/apply-all", methods=["POST"])
def api_cursor_profiles_apply_all():
    """Ap dung mot profile cho TOAN BO tai khoan trong pool va Cursor hien tai."""
    try:
        body = request.get_json(silent=True) or {}
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
        body = request.get_json(silent=True) or {}
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
        body = request.get_json(silent=True) or {}
        res = settings_manager.import_profiles(body)
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/cursor/app-status", methods=["GET"])
def api_cursor_app_status():
    """Kiem tra trang thai tien trinh va cua so ung dung Cursor."""
    try:
        from cursor_reloader import get_cursor_status
        status = get_cursor_status(use_cache=True)
        active = dict(pool.storage.get_active_account() or {})
        active.pop("access_token", None)
        active.pop("refresh_token", None)
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
    host = os.getenv("CURSOR_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "7860"))
    start_background_services()
    print(f"\n========================================================")
    print(f"  [>] CURSORROUTER v3.0 HOST RUNNING AT:")
    print(f"  [>] http://{host}:{port}")
    print(f"========================================================\n")
    app.run(host=host, port=port, debug=False, threaded=True)
