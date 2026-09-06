import os
import sys
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import sqlite3
import requests

con = sqlite3.connect('cursor_accounts.db')
cur = con.cursor()
ids = [4, 38, 241, 324, 347, 378, 532, 635, 715, 751, 794]
cur.execute(f"SELECT id, email, access_token, usage_percent, total_spend FROM accounts WHERE id IN ({','.join(map(str, ids))})")
rows = cur.fetchall()
con.close()

for r in rows:
    acc_id, email, tok, db_u, db_s = r
    headers = {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json', 'Connect-Protocol-Version': '1'}
    try:
        r_u = requests.post('https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage', headers=headers, json={}, timeout=5)
        ud = r_u.json() if r_u.status_code == 200 else {}
        pu = ud.get('planUsage', {})
        tot_u = pu.get('totalPercentUsed', 0)
        auto_u = pu.get('autoPercentUsed', 0)
        spend = pu.get('totalSpend', 0)
        
        r_s = requests.post('https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus', headers=headers, json={}, timeout=5)
        sd = r_s.json() if r_s.status_code == 200 else {}
        is_zero = sd.get('includedLimitZero', False)
        
        print(f"ID={acc_id} {email}: totalPercentUsed={tot_u}%, autoPercentUsed={auto_u}%, totalSpend=${spend}, includedLimitZero={is_zero}")
    except Exception as e:
        print(f"ID={acc_id} {email}: {e}")
