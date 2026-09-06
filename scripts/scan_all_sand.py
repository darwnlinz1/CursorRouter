import os
import sys
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import sqlite3
import requests
import concurrent.futures

con = sqlite3.connect('cursor_accounts.db')
cur = con.cursor()
cur.execute("SELECT id, email, access_token, usage_percent, status FROM accounts WHERE access_token IS NOT NULL")
rows = cur.fetchall()
con.close()

print(f"Total accounts with tokens: {len(rows)}")

def check_account(r):
    acc_id, email, tok, u, status = r
    headers = {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json', 'Connect-Protocol-Version': '1'}
    try:
        resp = requests.post('https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus', headers=headers, json={}, timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            is_zero = data.get('includedLimitZero', False)
            if not is_zero:
                return (acc_id, email, True, data)
        return (acc_id, email, False, resp.status_code)
    except Exception as e:
        return (acc_id, email, False, str(e))

valid = []
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    results = executor.map(check_account, rows)
    for res in results:
        if res[2]:
            print("FOUND WORKING ACCOUNT WITH QUOTA:", res)
            valid.append(res)

print(f"Total working accounts with non-zero quota: {len(valid)}")
