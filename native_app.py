#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cursor Manager - Native Desktop Application Entry Point
======================================================
Chạy ứng dụng dưới dạng Native Desktop Window (Microsoft Edge WebView2)
thông qua pywebview, độc lập với trình duyệt web bên ngoài.

Tính năng:
1. Tự động kiểm tra trạng thái tiến trình Cursor.exe khi khởi động.
2. Khởi chạy Flask Server ngầm trên 127.0.0.1:7860 (hoặc port tự do).
3. Hiển thị cửa sổ Native Desktop với giao diện Boxy Minimalist tiêu chuẩn.
4. Tự động giải phóng phím cứng, tài nguyên và tiến trình khi đóng ứng dụng.
"""

import os
import sys
import time
import socket
import threading
import argparse
from typing import Optional

# UTF-8 Windows Console
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Thêm src/ và root vào sys.path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from cursor_reloader import is_cursor_running, release_all_modifier_keys

def find_free_port(start_port: int = 7860, max_attempts: int = 20) -> int:
    """Tìm cổng mạng khả dụng, ưu tiên 7860."""
    for p in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start_port

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0

class NativeAppController:
    def __init__(self, port: int = 7860, headless: bool = False):
        self.port = port
        self.headless = headless
        self.server_thread: Optional[threading.Thread] = None
        self.window = None
        self.running = False

    def start_background_server(self):
        """Khởi động Flask dashboard ngầm nếu cổng chưa được lắng nghe."""
        if is_port_in_use(self.port):
            print(f"[*] Port {self.port} da co tien trinh chay san. Ket noi truc tiep...")
            return

        def _run():
            try:
                from werkzeug.serving import run_simple
                from server import app, start_background_services
                start_background_services()
                run_simple("127.0.0.1", self.port, app, threaded=True, use_reloader=False)
            except Exception as e:
                print(f"[-] Loi khoi dong server ngam: {e}")

        self.server_thread = threading.Thread(target=_run, daemon=True, name="NativeAppServer")
        self.server_thread.start()

        # Cho server khoi dong
        for _ in range(30):
            if is_port_in_use(self.port):
                break
            time.sleep(0.1)

    def on_window_closed(self):
        """Dọn dẹp phím cứng và tài nguyên khi người dùng tắt cửa sổ Native."""
        print("[*] Dong cua so Native Desktop... Dang giai phong tai nguyen...")
        self.running = False
        try:
            release_all_modifier_keys()
        except Exception:
            pass
        try:
            from server import stop_background_services
            stop_background_services()
        except Exception:
            pass

    def launch(self):
        """Khởi chạy ứng dụng Native Desktop."""
        self.running = True
        from app_paths import get_cookies_dir
        os.makedirs(get_cookies_dir(), exist_ok=True)
        
        # 1. Phát hiện Cursor IDE
        cursor_online = is_cursor_running()
        if cursor_online:
            print("[+] Phat hien Cursor IDE dang hoat dong. Dong bo trang thai...")
        else:
            print("[!] Cursor IDE chua duoc bat. Giao dien se hien banner nhac nho nguoi dung khoi dong.")

        # 2. Bật server ngầm
        self.start_background_server()

        url = f"http://127.0.0.1:{self.port}"
        print(f"[+] Native Desktop App san sang: {url}")

        if self.headless:
            print("[*] Chay che do Headless/Test. Bo qua khoi tao GUI Window.")
            return True

        # 3. Tạo Native Window qua pywebview (Edge WebView2) hoặc fallback Edge/Chrome --app mode
        try:
            import webview
            self.window = webview.create_window(
                title="CursorRouter v3.0 — Cursor Manager",
                url=url,
                width=1440,
                height=920,
                min_size=(1024, 720),
                background_color="#0a0a0a",
                text_select=True,
                zoomable=True
            )
            self.window.events.closed += self.on_window_closed
            webview.start(debug=False)
            return True
        except Exception as e:
            is_missing_pkg = isinstance(e, ImportError) or "No module named 'webview'" in str(e)
            if is_missing_pkg:
                print("[*] Chua cai dat thu vien 'pywebview'. Dang tu dong chuyen sang Edge/Chrome Standalone App Mode...")
                print("    (Tuy chon: chay 'pip install pywebview' de dung cua so nhung WebView2)")
            else:
                print(f"[-] Loi khoi dong pywebview GUI: {e}. Dang chuyen sang che do Edge/Chrome App Mode...")
            return self._launch_fallback_app_window(url, str(e))

    def _launch_fallback_app_window(self, url: str, err_msg: str = "") -> bool:
        """Fallback mo cua so doc lap bang Edge/Chrome --app mode (khong can pywebview)."""
        import subprocess
        import webbrowser

        candidates = []
        if sys.platform == "win32":
            pf = os.getenv("ProgramFiles", r"C:\Program Files")
            pf86 = os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
            local = os.getenv("LOCALAPPDATA", "")
            candidates = [
                os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            ]

        exe = next((p for p in candidates if p and os.path.exists(p)), None)
        proc = None
        try:
            if exe:
                proc = subprocess.Popen([exe, f"--app={url}", "--window-size=1440,920"])
                print(f"[+] Da mo cua so Desktop App qua {os.path.basename(exe)} (--app={url})")
            else:
                webbrowser.open(url)
                print(f"[+] Da mo dashboard trong trinh duyet mac dinh: {url}")

            print("[*] Server dang chay tai", url, "— nhan Ctrl+C de thoat.")
            while self.running:
                if proc is not None and proc.poll() is not None:
                    break
                time.sleep(0.5)
            return True
        except KeyboardInterrupt:
            return True
        except Exception as fb_err:
            print(f"[-] Loi fallback mo trinh duyet: {fb_err} (goc: {err_msg})")
            return False
        finally:
            self.on_window_closed()

def main():
    parser = argparse.ArgumentParser(description="Cursor Manager - Native Desktop App")
    parser.add_argument("--port", type=int, default=7860, help="Port cho server ngam (mac dinh: 7860)")
    parser.add_argument("--headless", action="store_true", help="Chay kiem thu headless khong hien thi window GUI")
    parser.add_argument("--native", action="store_true", help="Khoi chay native desktop mode")
    parser.add_argument("--app", action="store_true", help="Khoi chay native desktop mode")
    args = parser.parse_args()

    app_controller = NativeAppController(port=args.port, headless=args.headless)
    app_controller.launch()

if __name__ == "__main__":
    main()
