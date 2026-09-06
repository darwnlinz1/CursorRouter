import os
import sys
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import os
import ctypes
from ctypes import wintypes
import time
import subprocess

# 1. Ensure Master Prompt is in clipboard
prompt_path = r"C:\Users\darwnlinz\Downloads\CursorThings\test\PROMPT.md"
with open(prompt_path, "r", encoding="utf-8") as f:
    prompt_text = f.read()

# Put in Windows clipboard
import win32clipboard
try:
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardText(prompt_text, win32clipboard.CF_UNICODETEXT)
    win32clipboard.CloseClipboard()
    print("[+] Prompt text copied to Windows clipboard via Win32 API.")
except Exception as e:
    subprocess.run(["powershell", "-Command", f"Get-Content '{prompt_path}' -Raw | Set-Clipboard"], check=True)
    print("[+] Prompt text copied to Windows clipboard via PowerShell.")

# 2. Find Cursor PID and HWND
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

GENERIC_ALL = 0x10000000
h_default_desk = user32.OpenDesktopW("Default", 0, False, GENERIC_ALL)
if h_default_desk:
    user32.SetThreadDesktop(h_default_desk)

# Get all Cursor PIDs
ps_cmd = "Get-Process -Name Cursor -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"
res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True)
cursor_pids = set(int(line.strip()) for line in res.stdout.splitlines() if line.strip().isdigit())
print(f"[+] Found {len(cursor_pids)} Cursor PIDs: {sorted(list(cursor_pids))[:5]}...")

cursor_windows = []
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

def enum_proc(hwnd, lparam):
    if user32.IsWindowVisible(hwnd):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in cursor_pids:
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            cursor_windows.append((hwnd, pid.value, buf.value))
    return True

user32.EnumWindows(WNDENUMPROC(enum_proc), 0)

if not cursor_windows:
    print("[-] No visible Cursor window found!")
    sys.exit(1)

print(f"[+] Found {len(cursor_windows)} Cursor HWNDs:")
for h, p, t in cursor_windows:
    print(f"    HWND: {h} | PID: {p} | Title: {t}")

# Pick main window
main_hwnd = cursor_windows[0][0]
for h, p, t in cursor_windows:
    if "cursor" in t.lower() or "test" in t.lower() or len(t) > 3:
        main_hwnd = h
        break

print(f"[+] Targeting Cursor Window HWND: {main_hwnd}")

# 3. Bring to front
user32.ShowWindowAsync(main_hwnd, 9) # SW_RESTORE
time.sleep(0.3)
user32.SetForegroundWindow(main_hwnd)
time.sleep(0.4)

# 4. Key events
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_RETURN = 0x0D
VK_I = 0x49
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002

def send_combo(mod, key):
    user32.keybd_event(mod, 0, 0, 0)
    time.sleep(0.06)
    user32.keybd_event(key, 0, 0, 0)
    time.sleep(0.06)
    user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.06)
    user32.keybd_event(mod, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)

print("[*] Pressing Ctrl+I (Open Composer)...")
send_combo(VK_CONTROL, VK_I)
time.sleep(1.5)

print("[*] Pressing Ctrl+V (Paste Master Prompt)...")
send_combo(VK_CONTROL, VK_V)
time.sleep(1.0)

print("[*] Pressing Enter (Start Generation)...")
user32.keybd_event(VK_RETURN, 0, 0, 0)
time.sleep(0.06)
user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
time.sleep(0.1)

print("[+] COMPOSER GENERATION STARTED AUTOMATICALLY IN CURSOR! 🚀")
