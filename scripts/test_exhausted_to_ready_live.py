import os
import sys
import time
import json
import sqlite3
import urllib.request

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from cursor_storage import CursorStorageManager
from account_pool import AccountPoolManager, DB_FILE, QUOTA_EXHAUSTION_THRESHOLD

def run_test():
    storage = CursorStorageManager()
    pool = AccountPoolManager()
    
    print("=" * 65)
    print("TEST: TU DONG CHUYEN DOI TAI KHOAN HET QUOTA SANG ACC READY")
    print("=" * 65)
    
    # 1. Check current active
    initial_active = storage.get_active_account()
    print(f"[*] Tai khoan hien tai truoc test: {initial_active.get('email')}")
    
    # 2. Get exhausted account (13vladoprea@gmail.com - ID 1, 51% quota)
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM accounts WHERE email = '13vladoprea@gmail.com'")
    exhausted_row = cur.fetchone()
    con.close()
    
    if not exhausted_row:
        print("[-] Khong tim thay 13vladoprea@gmail.com trong DB, lay tai khoan >= 50% khac...")
        con = sqlite3.connect(DB_FILE)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT * FROM accounts WHERE usage_percent >= 50.0 AND access_token IS NOT NULL LIMIT 1")
        exhausted_row = cur.fetchone()
        con.close()
    
    assert exhausted_row is not None, "Khong tim thay tai khoan het quota trong DB!"
    target_email = exhausted_row["email"]
    print(f"[*] Tai khoan het quota duoc su dung: {target_email}")
    print(f"    - Quota da dung: {exhausted_row['usage_percent']}% (Nguong khoa: {QUOTA_EXHAUSTION_THRESHOLD}%)")
    print(f"    - Total spend: ${exhausted_row['total_spend'] or 0.0:.2f}")
    print(f"    - Trang thai trong DB: {exhausted_row['status']}")
    
    # 3. Inject exhausted account credentials into Cursor state.vscdb
    print(f"\n[+] BUOC 1: Nap tai khoan het quota ({target_email}) vao Cursor state.vscdb...")
    ok = storage.inject_full_profile(
        exhausted_row["access_token"],
        exhausted_row["refresh_token"],
        profile={
            "email": exhausted_row["email"],
            "displayName": exhausted_row["display_name"],
            "authId": exhausted_row["auth_id"]
        }
    )
    assert ok, "Nap token that bai!"
    
    active_now = storage.get_active_account()
    print(f"[V] Da xac thuc {target_email} dang active trong Cursor: {active_now.get('email') == target_email}")
    
    # 4. Wait for watcher (chu ky 5s) to detect and auto-switch
    print("\n[+] BUOC 2: Cho Auto-Rotate Watcher (chu ky 5s) phat hien va tu dong chuyen sang acc READY...")
    start_time = time.time()
    switched_to = None
    
    for i in range(20):
        time.sleep(1)
        current = storage.get_active_account()
        curr_email = (current.get("email") or "").strip().lower()
        if curr_email and curr_email != target_email.lower():
            switched_to = current
            elapsed = time.time() - start_time
            print(f"\n[!] THÀNH CÔNG: He thong da TU DONG CHUYEN DOI sau {elapsed:.1f} giay!")
            break
        print(f"    ... Giay {i+1}: Dang giam sat ({target_email} van o state.vscdb)...")
        
    assert switched_to is not None, "He thong khong tu dong chuyen doi khoi tai khoan het quota!"
    
    new_email = switched_to.get("email")
    print(f"\n[+] BUOC 3: Kiem tra tai khoan moi duoc nap vao Cursor: {new_email}")
    
    # Check new account in DB
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM accounts WHERE LOWER(email) = LOWER(?)", (new_email.strip().lower(),))
    new_row = cur.fetchone()
    con.close()
    
    assert new_row is not None, f"Khong tim thay {new_email} trong DB!"
    new_usage = new_row["usage_percent"] if new_row["usage_percent"] is not None else 0.0
    new_status = new_row["status"]
    print(f"    - Email moi: {new_email}")
    print(f"    - Quota tai khoan moi: {new_usage}% (< {QUOTA_EXHAUSTION_THRESHOLD}%)")
    print(f"    - Total spend: ${new_row['total_spend'] or 0.0:.2f}")
    print(f"    - Trang thai trong DB: {new_status}")
    
    assert new_usage < QUOTA_EXHAUSTION_THRESHOLD, f"Tai khoan moi phai co quota < 50%, nhan duoc: {new_usage}%"
    assert new_status in ("READY", "HIGH_USAGE"), f"Tai khoan moi phai co trang thai READY hoac HIGH_USAGE, nhan duoc: {new_status}"
    
    print("\n" + "=" * 65)
    print(">>> KET LUAN: TEST THANH CONG 100%! <<<")
    print(f"He thong da tu dong phat hien tai khoan {target_email} het quota ({exhausted_row['usage_percent']}%),")
    print(f"thuc hien Hard Reset app Cursor va tu dong chuyen sang tai khoan READY ({new_email}, Quota: {new_usage}%).")
    print("=" * 65)

if __name__ == "__main__":
    run_test()
