import os
import sys
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import os
import sqlite3
import json
from cursor_storage import CursorStorageManager
from account_pool import AccountPoolManager

def main():
    sm = CursorStorageManager()
    print("=== State.vscdb Path ===", sm.db_path)
    active = sm.get_active_account()
    print("=== Active Account from CursorStorageManager ===")
    for k, v in active.items():
        if k in ("access_token", "refresh_token") and v:
            print(f"  {k}: {v[:30]}... len={len(v)}")
        else:
            print(f"  {k}: {v}")

    pool = AccountPoolManager()
    synced = pool.sync_active_from_cursor()
    print("=== Synced Active from Pool ===")
    for k, v in (synced or {}).items():
        print(f"  {k}: {v}")

    con = sqlite3.connect("cursor_accounts.db")
    cur = con.cursor()
    cur.execute("SELECT id, email, usage_percent, total_spend, status, auth_id FROM accounts WHERE email LIKE '%vlad%' OR email LIKE '%mustafa%'")
    print("=== Vlad / Mustafa in cursor_accounts.db ===")
    for r in cur.fetchall():
        print(" ", r)

    all_accs = pool.get_all_accounts()
    active_in_pool = [a for a in all_accs if a.get("is_active")]
    print("=== Active account(s) according to pool.get_all_accounts() ===")
    for a in active_in_pool:
        print(f"  ID={a['id']}, email={a['email']}, is_active={a['is_active']}, status={a['status']}")

if __name__ == "__main__":
    main()
