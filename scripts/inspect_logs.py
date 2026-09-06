import os
import sys
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import os
import glob
import re

p = os.path.expandvars(r'%APPDATA%\Cursor\logs')
logs = glob.glob(os.path.join(p, '**', '*Structured*.log'), recursive=True)
logs.sort(key=os.path.getmtime, reverse=True)

for log_path in logs[:3]:
    print("=== File:", log_path)
    lines = open(log_path, 'r', encoding='utf-8', errors='ignore').readlines()
    for l in lines:
        if 'resource_exhausted' in l or 'ERROR_RATE_LIMITED' in l or "hit your usage limit" in l:
            print("LINE:", l.strip())
