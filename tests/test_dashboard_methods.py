import sqlite3
import requests

con = sqlite3.connect('cursor_accounts.db')
cur = con.cursor()
cur.execute("SELECT id, email, access_token, status, usage_percent FROM accounts WHERE access_token IS NOT NULL AND status != 'EXPIRED' ORDER BY id DESC")
rows = cur.fetchall()
con.close()

print(f"Total active tokens: {len(rows)}")
with_quota = []
for acc_id, email, token, status, usage_pct in rows[:30]:
    try:
        resp = requests.post(
            'https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Connect-Protocol-Version': '1'},
            json={},
            timeout=4
        )
        if resp.status_code == 200:
            data = resp.json()
            is_zero = data.get('includedLimitZero', False)
            if not is_zero:
                print(f"[FOUND WITH QUOTA!] ID={acc_id} {email}: {data}")
                with_quota.append((acc_id, email, data))
            else:
                print(f"ID={acc_id} {email}: zero quota")
        else:
            print(f"ID={acc_id} {email}: status={resp.status_code}")
    except Exception as e:
        print(f"ID={acc_id} {email}: {e}")

print(f"Accounts with quota: {len(with_quota)}")
