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
import time
import subprocess
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
GENERIC_ALL = 0x10000000

def ensure_desktop():
    h_default_desk = user32.OpenDesktopW("Default", 0, False, GENERIC_ALL)
    if h_default_desk:
        user32.SetThreadDesktop(h_default_desk)

def scan_and_detect_errors(target_dir):
    print(f"[*] Scanning {target_dir} for syntax and runtime errors...")
    errors = []
    for root, dirs, files in os.walk(target_dir):
        for f in files:
            if f.endswith('.js'):
                fpath = os.path.join(root, f)
                res = subprocess.run(['node', '-c', fpath], capture_output=True, text=True)
                if res.returncode != 0:
                    errors.append({
                        "file": f,
                        "path": fpath,
                        "stderr": res.stderr.strip()
                    })
    return errors

def dispatch_fix_to_cursor(err_info):
    t0 = time.perf_counter()
    ensure_desktop()

    # Find Cursor Window
    cursor_hwnds = []
    import psutil
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                pname = psutil.Process(pid.value).name().lower()
            except Exception:
                pname = ""
            if pname == "cursor.exe":
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                    cursor_hwnds.append((hwnd, title))
        return True
    user32.EnumWindows(WNDENUMPROC(enum_proc), 0)

    if not cursor_hwnds:
        print("[-] Could not find Cursor window.")
        return False, 0

    hwnd = cursor_hwnds[0][0]
    user32.ShowWindowAsync(hwnd, 9)
    time.sleep(0.3)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)

    # Formulate precise fix prompt
    prompt_text = (
        f"FIX ERROR IN PROJECT:\n"
        f"File: {err_info['file']}\n"
        f"Error Trace:\n{err_info['stderr']}\n\n"
        f"Please fix the syntax error immediately, correct the code, and explain the fix."
    )

    # Copy to clipboard
    subprocess.run(["powershell", "-Command", f"Set-Clipboard -Value @'\n{prompt_text}\n'@"], check=True)

    VK_CONTROL = 0x11
    VK_I = 0x49
    VK_V = 0x56
    VK_RETURN = 0x0D
    KEYEVENTF_KEYUP = 0x0002

    def send_combo(m, k):
        user32.keybd_event(m, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(k, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(k, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        user32.keybd_event(m, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.1)

    print("[*] Opening Composer (Ctrl+I)...")
    send_combo(VK_CONTROL, VK_I)
    time.sleep(1.0)

    print("[*] Pasting Fix Prompt into Composer (Ctrl+V)...")
    send_combo(VK_CONTROL, VK_V)
    time.sleep(0.5)

    print("[*] Submitting Fix to Cursor AI (Ctrl+Enter)...")
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    t_elapsed = (time.perf_counter() - t0) * 1000
    return True, t_elapsed

if __name__ == '__main__':
    print("=" * 70)
    print("DEMONSTRATING LIVE AUTO-FIX DETECTION & DISPATCH IN CURSOR")
    print("=" * 70)

    target_dir = r"C:\Users\darwnlinz\Downloads\CursorThings\test"
    errors = scan_and_detect_errors(target_dir)

    print(f"\n[+] Found {len(errors)} error(s) in project:")
    for i, e in enumerate(errors, 1):
        print(f"--- Error {i} in {e['file']} ---")
        print(f"{e['stderr']}")
        print("-" * 50)

    if errors:
        first_err = errors[0]
        print(f"\n[*] Triggering Auto-Fix for {first_err['file']}...")
        success, latency = dispatch_fix_to_cursor(first_err)
        print(f"\n[+] Auto-Fix Dispatch Result: {'SUCCESS' if success else 'FAILED'}")
        print(f"[+] Total Detection-to-Dispatch Latency: {latency:.2f} ms ({latency/1000:.2f} seconds) [OK]")
    else:
        print("[+] No errors found to fix.")
