"""
Cursor AI x-cursor-checksum Generator
=====================================
Computes the dynamic x-cursor-checksum header required by api2.cursor.sh
using the client timestamp and machine IDs.
"""

import os
import time
import json
import base64
import uuid
from typing import Optional, Tuple

_CACHED_IDS: Optional[Tuple[str, str]] = None
_LAST_STORAGE_MTIME: Optional[float] = None


def reset_cached_machine_ids():
    """Invalidates the in-memory cached machine IDs so subsequent calls read fresh values."""
    global _CACHED_IDS, _LAST_STORAGE_MTIME
    _CACHED_IDS = None
    _LAST_STORAGE_MTIME = None


def get_cursor_machine_ids(force_refresh: bool = False) -> Tuple[str, str]:
    """
    Retrieves machineId and macMachineId from Cursor's storage.json.
    Falls back to stable machine-derived IDs if not found.
    Automatically refreshes if storage.json mtime changed or force_refresh is True.
    """
    global _CACHED_IDS, _LAST_STORAGE_MTIME

    appdata = os.getenv("APPDATA")
    storage_path = os.path.join(appdata, "Cursor", "User", "globalStorage", "storage.json") if appdata else None

    # Check if storage.json on disk was modified since last read
    current_mtime = None
    if storage_path and os.path.exists(storage_path):
        try:
            current_mtime = os.path.getmtime(storage_path)
        except Exception:
            pass

    if not force_refresh and _CACHED_IDS is not None:
        if current_mtime is not None and _LAST_STORAGE_MTIME is not None:
            if current_mtime == _LAST_STORAGE_MTIME:
                return _CACHED_IDS
        elif current_mtime is None and _LAST_STORAGE_MTIME is None:
            return _CACHED_IDS

    if storage_path and os.path.exists(storage_path):
        try:
            with open(storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                m_id = data.get("telemetry.machineId")
                mac_id = data.get("telemetry.macMachineId")
                if m_id:
                    _CACHED_IDS = (m_id, mac_id or "")
                    _LAST_STORAGE_MTIME = current_mtime
                    return _CACHED_IDS
        except Exception:
            pass

    # Fallback to stable IDs
    m_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "cursor.machine.id"))
    mac_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "cursor.mac.machine.id"))
    _CACHED_IDS = (m_id, mac_id)
    _LAST_STORAGE_MTIME = current_mtime
    return _CACHED_IDS


def generate_cursor_checksum(
    machine_id: Optional[str] = None,
    mac_machine_id: Optional[str] = None,
    timestamp_ms: Optional[int] = None
) -> str:
    """
    Generates a valid x-cursor-checksum string with the specified or current timestamp.
    """
    if not machine_id:
        m_id, mac_id = get_cursor_machine_ids()
        machine_id = machine_id or m_id
        mac_machine_id = mac_machine_id or mac_id

    now_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    c = now_ms // 1000000

    x = bytearray([
        (c >> 40) & 255,
        (c >> 32) & 255,
        (c >> 24) & 255,
        (c >> 16) & 255,
        (c >> 8) & 255,
        c & 255
    ])

    t = 165
    for n in range(len(x)):
        x[n] = ((x[n] ^ t) + (n % 256)) & 255
        t = x[n]

    b64 = base64.b64encode(x).decode("utf-8")
    if mac_machine_id:
        return f"{b64}{machine_id}/{mac_machine_id}"
    return f"{b64}{machine_id}"


if __name__ == "__main__":
    m_id, mac_id = get_cursor_machine_ids()
    checksum = generate_cursor_checksum(m_id, mac_id)
    print("Machine ID:", m_id)
    print("Mac Machine ID:", mac_id)
    print("Generated Checksum:", checksum)
