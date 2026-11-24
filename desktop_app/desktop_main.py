#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CursorRouter - Native Desktop Application (WebView2)
====================================================
Runs CursorRouter as a standalone native Windows desktop application
powered by Microsoft Edge WebView2 (pywebview).
"""

import os
import sys
import time
import socket
import threading
import argparse
from typing import Optional

# UTF-8 Windows Console
if sys.platform == 'win32':
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Ensure root and src are on sys.path
DESKTOP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(DESKTOP_DIR, '..'))
SRC_DIR = os.path.join(ROOT_DIR, 'src')
for p in (SRC_DIR, ROOT_DIR, DESKTOP_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from cursor_reloader import is_cursor_running, release_all_modifier_keys


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def find_free_port(start_port: int = 7860, max_attempts: int = 20) -> int:
    for p in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', p))
                return p
            except OSError:
                continue
    return start_port


class DesktopAppController:
    def __init__(self, port: int = 7860, headless: bool = False):
        self.port = port
        self.headless = headless
        self.server_thread: Optional[threading.Thread] = None
        self.window = None
        self.running = False

    def start_background_server(self):
        if is_port_in_use(self.port):
            print(f'[*] Port {self.port} already running background service. Connecting directly...')
            return

        def _run():
            try:
                from werkzeug.serving import run_simple
                from server import app, start_background_services
                start_background_services()
                run_simple('127.0.0.1', self.port, app, threaded=True, use_reloader=False)
            except Exception as e:
                print(f'[-] Background server error: {e}')

        self.server_thread = threading.Thread(target=_run, daemon=True, name='DesktopAppServer')
        self.server_thread.start()

        for _ in range(30):
            if is_port_in_use(self.port):
                break
            time.sleep(0.1)

    def on_window_closed(self):
        print('[*] Desktop window closed. Cleaning up background resources...')
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
        self.running = True
        from app_paths import get_data_dir
        os.makedirs(get_data_dir(), exist_ok=True)

        cursor_online = is_cursor_running()
        if cursor_online:
            print('[+] Cursor IDE is running. Syncing state...')
        else:
            print('[!] Cursor IDE is not running. Dashboard banner will prompt user to launch.')

        self.start_background_server()
        url = f'http://127.0.0.1:{self.port}'
        print(f'[+] Desktop Application ready at {url}')

        if self.headless:
            print('[*] Running in headless mode. Skipping GUI creation.')
            return True

        try:
            import webview
            self.window = webview.create_window(
                title='CursorRouter v3.0 — Intelligent Quota Manager',
                url=url,
                width=1440,
                height=920,
                min_size=(1024, 720),
                background_color='#0a0a0a',
                text_select=True,
                zoomable=True
            )
            self.window.events.closed += self.on_window_closed
            webview.start(debug=False)
            return True
        except Exception as e:
            is_missing_pkg = isinstance(e, ImportError) or "No module named 'webview'" in str(e)
            if is_missing_pkg:
                print("[*] 'pywebview' is not installed. Falling back to Edge/Chrome Standalone App Mode...")
                print("    (Optional: run 'pip install pywebview' to use embedded WebView2)")
            else:
                print(f'[-] Failed to initialize native webview: {e}. Falling back to Edge/Chrome App Mode...')
            import subprocess
            import webbrowser

            candidates = []
            if sys.platform == 'win32':
                pf = os.getenv('ProgramFiles', r'C:\Program Files')
                pf86 = os.getenv('ProgramFiles(x86)', r'C:\Program Files (x86)')
                local = os.getenv('LOCALAPPDATA', '')
                candidates = [
                    os.path.join(pf86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
                    os.path.join(pf, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
                    os.path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'),
                    os.path.join(pf86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
                    os.path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe'),
                ]
            exe = next((p for p in candidates if p and os.path.exists(p)), None)
            proc = None
            try:
                if exe:
                    proc = subprocess.Popen([exe, f'--app={url}', '--window-size=1440,920'])
                    print(f'[+] Opened standalone app window via {os.path.basename(exe)}: {url}')
                else:
                    webbrowser.open(url)
                    print(f'[+] Opened dashboard in default browser: {url}')
                while self.running:
                    if proc is not None and proc.poll() is not None:
                        break
                    time.sleep(0.5)
                return True
            except KeyboardInterrupt:
                return True
            finally:
                self.on_window_closed()


def main():
    parser = argparse.ArgumentParser(description='CursorRouter - Native Desktop Application')
    parser.add_argument('--port', type=int, default=7860, help='Port for background server (default: 7860)')
    parser.add_argument('--headless', action='store_true', help='Run in headless mode without GUI window')
    args = parser.parse_args()

    controller = DesktopAppController(port=args.port, headless=args.headless)
    controller.launch()


if __name__ == '__main__':
    main()
