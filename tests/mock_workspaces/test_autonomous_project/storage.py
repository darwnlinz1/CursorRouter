
import sqlite3

def init_db(db_path):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            amount REAL NOT NULL,
            action TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            symbol TEXT PRIMARY KEY,
            shares REAL NOT NULL,
            avg_price REAL NOT NULL
        )
    """)
    con.commit()
    con.close()

def record_transaction(db_path, asset, amount, action):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("INSERT INTO ledger (asset, amount, action) VALUES (?, ?, ?)", (asset, amount, action))
    con.commit()
    con.close()

def get_balance(db_path, asset):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT SUM(amount) FROM ledger WHERE asset = ?", (asset,))
    res = cur.fetchone()[0]
    con.close()
    return float(res) if res is not None else 0.0
