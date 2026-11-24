"""
CursorRouter - Unified Path & Persistence Resolver
==================================================
Cleanly separates read-only bundled assets (templates/, static/) from writable
persistent user data (cursor_accounts.db, .cursor_master.key, Cookies/, configs,
and .updates/) across both development checkouts and PyInstaller --onefile builds.
"""

import os
import sys
import tempfile
from typing import Tuple, Optional


def is_frozen() -> bool:
    """Return True when running inside a PyInstaller / frozen executable."""
    return bool(getattr(sys, "frozen", False))


def get_bundle_dir() -> str:
    """Return directory holding read-only bundled assets (templates/, static/)."""
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return os.path.abspath(sys._MEIPASS)
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _is_dir_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".cr_probe_", dir=path)
        os.close(fd)
        os.remove(probe)
        return True
    except Exception:
        return False


def get_data_dir() -> str:
    """
    Return writable directory for persistent runtime state:
    - Honors CURSOR_ROUTER_DATA_DIR env override when set.
    - In frozen --onefile mode: prefers the directory next to the .exe (portable mode)
      if writable; otherwise falls back to %LOCALAPPDATA%\\CursorRouter (or %APPDATA%).
    - In development / pytest mode: returns repository root for 100% backward compatibility.
    """
    env_dir = os.getenv("CURSOR_ROUTER_DATA_DIR")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return os.path.abspath(env_dir)

    if is_frozen():
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if _is_dir_writable(exe_dir):
            return exe_dir
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or os.path.expanduser("~")
        fallback = os.path.abspath(os.path.join(base, "CursorRouter"))
        os.makedirs(fallback, exist_ok=True)
        return fallback

    return get_bundle_dir()


def get_templates_dir() -> str:
    return os.path.join(get_bundle_dir(), "templates")


def get_static_dir() -> str:
    return os.path.join(get_bundle_dir(), "static")


def get_db_path() -> str:
    return os.path.abspath(
        os.getenv("CURSOR_ACCOUNTS_DB", os.path.join(get_data_dir(), "cursor_accounts.db"))
    )


def get_cookies_dir(custom: Optional[str] = None) -> str:
    if custom:
        if os.path.isabs(custom):
            return os.path.abspath(custom)
        return os.path.abspath(os.path.join(get_data_dir(), custom))
    return os.path.abspath(os.path.join(get_data_dir(), "Cookies"))


def get_updates_dir() -> str:
    return os.path.abspath(os.path.join(get_data_dir(), ".updates"))


def get_config_path(filename: str) -> str:
    return os.path.abspath(os.path.join(get_data_dir(), filename))


def get_master_key_paths() -> Tuple[str, Optional[str], Optional[str]]:
    """
    Return (primary_key_path, appdata_mirror_path, legacy_appdata_path)
    so moving a portable binary never orphans an existing encrypted SQLite DB.
    """
    primary = os.path.join(get_data_dir(), ".cursor_master.key")
    appdata = os.getenv("APPDATA")
    mirror = os.path.join(appdata, "CursorRouter", ".master.key") if appdata else None
    legacy = os.path.join(appdata, "CursorManager", ".master.key") if appdata else None
    return primary, mirror, legacy
