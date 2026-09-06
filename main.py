#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cursor Manager - Unified Application Entry Point
Khởi động Web Dashboard hoặc điều khiển CLI Quản trị Cursor.
"""

import os
import sys

# Dam bao encoding tren Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Thêm src/ và root vào sys.path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

if __name__ == "__main__":
    # Dam bao thu muc Cookies luon duoc tu dong tao khi khoi dong
    os.makedirs(os.path.join(ROOT_DIR, "Cookies"), exist_ok=True)

    # Neu co tham so --native hoac --app, khoi chay Native Desktop Window
    if any(arg in ("--native", "--app") for arg in sys.argv[1:]):
        import native_app
        native_app.main()
    # Neu co tham so CLI khac (vi du: --status, --scan, --auto-switch, -h, --help), chuyen cho cursor_manager xu ly
    elif len(sys.argv) > 1 and any(arg.startswith("-") for arg in sys.argv[1:]):
        from cursor_manager import main as cli_main
        cli_main()
    else:
        # Mac dinh khoi dong Web Dashboard Host
        from server import app
        port = int(os.getenv("PORT", "7860"))
        print(f"\n========================================================")
        print(f"  [>] CURSOR MANAGER HOST DANG CHAY TAI:")
        print(f"  [>] http://127.0.0.1:{port}  hoac  http://localhost:{port}")
        print(f"========================================================\n")
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
