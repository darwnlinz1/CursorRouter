import os
import sys
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import sqlite3
import json
import os

p = os.path.expandvars(r'%APPDATA%\Cursor\User\globalStorage\state.vscdb')
con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
cur = con.cursor()
cur.execute('SELECT value FROM cursorDiskKV WHERE key = ?', ('composerData:787b2608-55c3-4d47-81f6-7486dcb87f1c',))
row = cur.fetchone()
d = json.loads(row[0])

print("Status:", d.get("status"))
print("generatingBubbleIds:", d.get("generatingBubbleIds"))
print("isContinuationInProgress:", d.get("isContinuationInProgress"))
print("latestChatGenerationUUID:", d.get("latestChatGenerationUUID"))

cmap = d.get("conversationMap", {})
print("ConversationMap count:", len(cmap))
# Get last 3 bubbles
bubble_ids = list(cmap.keys())
for bid in bubble_ids[-3:]:
    b = cmap[bid]
    print(f"--- Bubble {bid} ---")
    print("  type:", b.get("type"))
    print("  status:", b.get("status"))
    print("  hasError:", b.get("hasError"))
    print("  error:", str(b.get("error"))[:200])
    print("  text:", repr(b.get("text", "")[:100]))
