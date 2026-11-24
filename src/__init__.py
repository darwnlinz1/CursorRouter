# -*- coding: utf-8 -*-
"""
Cursor Manager - Core Package
Chứa toàn bộ các module xử lý nghiệp vụ, quản lý token, đồng bộ Cursor, proxy và server.
"""

import os
import sys

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)

for _p in (SRC_DIR, ROOT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

__version__ = "3.1.0"

__all__ = [
    "account_pool",
    "ai_optimizer",
    "app_paths",
    "async_sync_engine",
    "chat_lock_detector",
    "cursor_checksum",
    "cursor_manager",
    "cursor_reloader",
    "cursor_settings",
    "cursor_storage",
    "jsonc_utils",
    "pkce_auth",
    "rotating_proxy",
    "server",
    "smart_task_filter",
    "token_cipher",
    "token_pool",
    "updater",
    "windows_notifier",
]
