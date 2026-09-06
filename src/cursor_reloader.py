import os
import sys
import json
import time
import subprocess
import requests
from typing import Dict, Any, Optional, List, Tuple

try:
    import ctypes
    from ctypes import wintypes
except ImportError:
    ctypes = None
    wintypes = None

try:
    import psutil
except ImportError:
    psutil = None

try:
    import win32clipboard
except ImportError:
    win32clipboard = None

user32 = getattr(getattr(ctypes, "windll", None), "user32", None) if ctypes else None
kernel32 = getattr(getattr(ctypes, "windll", None), "kernel32", None) if ctypes else None

KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_LWIN = 0x5B
VK_RWIN = 0x5C

BRIDGE_PORT = 49123

def _parse_jsonc(content: str) -> Any:
    """Parse JSON with single-line comments, multi-line comments, and trailing commas."""
    import re
    # Strip multi-line comments /* ... */
    c = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    # Strip single-line comments // ...
    c = re.sub(r'//.*$', '', c, flags=re.MULTILINE)
    # Strip trailing commas before ] or }
    c = re.sub(r',\s*([\]}])', r'\1', c)
    return json.loads(c)

def ensure_keybinding() -> bool:
    """Dam bao phim tat ctrl+shift+f11 duoc gan cho workbench.action.reloadWindow trong keybindings.json (Tranh 100% xung dot voi NVIDIA Alt+R)."""
    try:
        appdata = os.getenv("APPDATA")
        if not appdata:
            return False
        kb_path = os.path.join(appdata, "Cursor", "User", "keybindings.json")
        
        bindings = []
        if os.path.exists(kb_path):
            try:
                with open(kb_path, "r", encoding="utf-8") as f:
                    c = f.read().strip()
                    if c:
                        bindings = _parse_jsonc(c)
            except Exception as parse_err:
                print(f"[-] Canh bao parse keybindings.json: {parse_err}. Sao luu truoc khi ghi de...")
                try:
                    import shutil
                    shutil.copy2(kb_path, kb_path + ".bak")
                except Exception:
                    pass
                bindings = []
        
        # Xoa bo cac binding cu gay xung dot voi NVIDIA GeForce Experience (ctrl+alt+r hoac bat ky phim chua alt+r nao)
        bindings = [
            b for b in bindings 
            if not (isinstance(b, dict) and b.get("command") == "workbench.action.reloadWindow" and "alt" in b.get("key", "").lower())
        ]
        
        has_binding = any(
            isinstance(b, dict) and b.get("command") == "workbench.action.reloadWindow" and b.get("key") == "ctrl+shift+f11"
            for b in bindings
        )
        
        if not has_binding:
            bindings.append({
                "key": "ctrl+shift+f11",
                "command": "workbench.action.reloadWindow"
            })
            os.makedirs(os.path.dirname(kb_path), exist_ok=True)
            with open(kb_path, "w", encoding="utf-8") as f:
                json.dump(bindings, f, indent=2)
        return True
    except Exception as e:
        print(f"[-] Loi ghi keybindings.json: {e}")
        return False

def reload_via_extension() -> bool:
    """Cap do 1: Reload 100% ngam (Silent) thong qua local extension bridge tren port 49123."""
    try:
        r = requests.get(f"http://127.0.0.1:{BRIDGE_PORT}/reload", timeout=1.2)
        if r.status_code == 200:
            print("[+] Da gui tin hieu reload thanh cong qua Extension Bridge (Silent Mode)!")
            return True
    except Exception:
        pass
    return False

def find_cursor_window():
    """Tim HWND cua cua so chinh Cursor IDE tren desktop Default, tuyet doi khong nham voi cac ung dung khac hoac Chrome."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        import psutil
        user32 = ctypes.windll.user32
        
        hdesk = None
        try:
            hdesk = user32.OpenDesktopW("Default", 0, False, 0x10000000)
            if hdesk:
                user32.SetThreadDesktop(hdesk)

            target_hwnd = None
            def enum_cb(hwnd, lparam):
                nonlocal target_hwnd
                if not user32.IsWindowVisible(hwnd):
                    return True
                
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    p = psutil.Process(pid.value)
                    pname = p.name().lower()
                except Exception:
                    return True
                    
                # Chi chap nhan process duy nhat la Cursor.exe, loai bo tuyet doi cac app khac va Chrome
                if pname != "cursor.exe":
                    return True

                buf_class = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf_class, 256)
                if buf_class.value == "Chrome_WidgetWin_1":
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf_title = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf_title, length + 1)
                        title = buf_title.value.strip()
                        # Dam bao cua so co tieu de hop le
                        if title:
                            target_hwnd = hwnd
                            return False
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            if hdesk:
                user32.EnumDesktopWindows(hdesk, WNDENUMPROC(enum_cb), 0)
            else:
                user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
                
            return target_hwnd
        finally:
            if hdesk and hasattr(user32, "CloseDesktop"):
                try:
                    user32.CloseDesktop(hdesk)
                except Exception:
                    pass
    except Exception as e:
        print(f"[-] Loi tim HWND Cursor: {e}")
        return None

def find_all_cursor_windows() -> List[Tuple[int, str]]:
    """Tim toan bo danh sach cac HWND cua cac cua so Cursor IDE dang mo."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        import psutil
        user32 = ctypes.windll.user32
        
        hdesk = None
        try:
            hdesk = user32.OpenDesktopW("Default", 0, False, 0x10000000)
            if hdesk:
                user32.SetThreadDesktop(hdesk)

            windows = []
            def enum_cb(hwnd, lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    p = psutil.Process(pid.value)
                    pname = p.name().lower()
                except Exception:
                    return True
                    
                if pname != "cursor.exe":
                    return True

                buf_class = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf_class, 256)
                if buf_class.value == "Chrome_WidgetWin_1":
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf_title = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf_title, length + 1)
                        title = buf_title.value.strip()
                        if title:
                            windows.append((hwnd, title))
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            if hdesk:
                user32.EnumDesktopWindows(hdesk, WNDENUMPROC(enum_cb), 0)
            else:
                user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
                
            return windows
        finally:
            if hdesk and hasattr(user32, "CloseDesktop"):
                try:
                    user32.CloseDesktop(hdesk)
                except Exception:
                    pass
    except Exception as e:
        print(f"[-] Loi find_all_cursor_windows: {e}")
        return []

def is_cursor_window(hwnd: int) -> bool:
    """
    Xac thuc nghiem ngat xem HWND co thuc su thuoc ve ung dung Cursor IDE hay khong:
    - Kiem tra HWND hop le tren Windows
    - Kiem tra Process ID thuoc ve cursor.exe (tuyet doi khong phai browser, terminal, hay app khac)
    - Kiem tra Window Class Name (Chrome_WidgetWin_1)
    """
    if not hwnd or sys.platform != "win32":
        return False
    try:
        u32 = user32 or (ctypes.windll.user32 if ctypes and hasattr(ctypes, "windll") else None)
        if not u32 or not u32.IsWindow(hwnd):
            return False

        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False

        proc_module = psutil
        if proc_module is None:
            import psutil as proc_module

        try:
            p = proc_module.Process(pid.value)
            pname = p.name().lower()
        except Exception:
            return False

        if pname != "cursor.exe":
            return False

        buf_class = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, buf_class, 256)
        cname = str(buf_class.value or "")
        if cname != "Chrome_WidgetWin_1" and not cname.startswith("Chrome_WidgetWin_"):
            return False

        return True
    except Exception:
        return False

KEYEVENTF_EXTENDEDKEY = 0x0001

def get_modifier_keys_status(u32=None) -> Dict[str, Any]:
    """
    Kiem tra trang thai thuc te cua cac modifier keys (Alt, Ctrl, Shift, Win) tren Windows.
    Su dung GetAsyncKeyState de kiem tra bit cao nhat (0x8000).
    Tra ve dict cho biet co phim nao dang bi de/ket hay khong.
    """
    if sys.platform != "win32":
        return {"alt": False, "ctrl": False, "shift": False, "win": False, "any_stuck": False}
    try:
        user_lib = u32 or user32 or (ctypes.windll.user32 if ctypes and hasattr(ctypes, "windll") else None)
        if not user_lib or not hasattr(user_lib, "GetAsyncKeyState"):
            return {"alt": False, "ctrl": False, "shift": False, "win": False, "any_stuck": False}

        alt_pressed = bool(user_lib.GetAsyncKeyState(0x12) & 0x8000 or user_lib.GetAsyncKeyState(0xA4) & 0x8000 or user_lib.GetAsyncKeyState(0xA5) & 0x8000)
        ctrl_pressed = bool(user_lib.GetAsyncKeyState(0x11) & 0x8000 or user_lib.GetAsyncKeyState(0xA2) & 0x8000 or user_lib.GetAsyncKeyState(0xA3) & 0x8000)
        shift_pressed = bool(user_lib.GetAsyncKeyState(0x10) & 0x8000 or user_lib.GetAsyncKeyState(0xA0) & 0x8000 or user_lib.GetAsyncKeyState(0xA1) & 0x8000)
        win_pressed = bool(user_lib.GetAsyncKeyState(0x5B) & 0x8000 or user_lib.GetAsyncKeyState(0x5C) & 0x8000)

        any_stuck = alt_pressed or ctrl_pressed or shift_pressed or win_pressed
        return {
            "alt": alt_pressed,
            "ctrl": ctrl_pressed,
            "shift": shift_pressed,
            "win": win_pressed,
            "any_stuck": any_stuck
        }
    except Exception:
        return {"alt": False, "ctrl": False, "shift": False, "win": False, "any_stuck": False}


def is_user_actively_typing(idle_threshold_sec: float = 3.0, u32=None, k32=None) -> Tuple[bool, float]:
    """
    Kiem tra xem nguoi dung co dang tich cuc go phim hoac thao tac chuot tren may host khong.
    Tra ve (is_active, idle_seconds).
    Neu nguoi dung vua thao tac trong vong idle_threshold_sec, tra ve is_active=True de
    tranh cuop focus dot ngot lam hong qua trinh go van ban / lam viec cua nguoi dung.
    """
    if sys.platform != "win32":
        return False, 999.0
    # Trong moi truong test tu dong (pytest/unittest): chi danh gia neu u32/k32 la mock, khong doc input vat ly cua developer tren OS
    if "unittest" in sys.modules or "pytest" in sys.modules:
        is_mock = hasattr(u32, "_mock_name") or hasattr(u32, "mock_calls") or hasattr(k32, "_mock_name")
        if not is_mock:
            return False, 999.0
    try:
        user_lib = u32 or user32 or (ctypes.windll.user32 if ctypes and hasattr(ctypes, "windll") else None)
        kernel_lib = k32 or kernel32 or (ctypes.windll.kernel32 if ctypes and hasattr(ctypes, "windll") else None)
        if not user_lib or not hasattr(user_lib, "GetLastInputInfo") or not kernel_lib:
            return False, 999.0

        from ctypes import wintypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("dwTime", wintypes.DWORD)
            ]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if user_lib.GetLastInputInfo(ctypes.byref(lii)):
            tc = kernel_lib.GetTickCount()
            elapsed_ms = (tc - lii.dwTime) & 0xFFFFFFFF
            idle_sec = max(0.0, elapsed_ms / 1000.0)
            return (idle_sec < idle_threshold_sec), idle_sec
    except Exception:
        pass
    return False, 999.0


def release_all_modifier_keys(u32=None) -> bool:
    """
    Giai phong triet de toan bo modifier keys (Alt, Ctrl, Shift, Windows keys) tren Windows.
    Dam bao ban phim nguoi dung khong bao gio bi ket trang thai de phim Alt hoac Ctrl.
    Goi an toan o bat ky dau, dac biet la trong cac khoi finally.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        user_lib = u32 or user32 or (ctypes.windll.user32 if ctypes and hasattr(ctypes, "windll") else None)
        if not user_lib or not hasattr(user_lib, "keybd_event"):
            return False

        KEYEVENTF_KEYUP = 0x0002

        MODIFIERS_TO_RELEASE = [
            0x12,  # VK_MENU (Alt)
            0xA4,  # VK_LMENU (Left Alt)
            0xA5,  # VK_RMENU (Right Alt)
            0x11,  # VK_CONTROL (Ctrl)
            0xA2,  # VK_LCONTROL (Left Ctrl)
            0xA3,  # VK_RCONTROL (Right Ctrl)
            0x10,  # VK_SHIFT (Shift)
            0xA0,  # VK_LSHIFT (Left Shift)
            0xA1,  # VK_RSHIFT (Right Shift)
            0x5B,  # VK_LWIN (Left Windows Key)
            0x5C,  # VK_RWIN (Right Windows Key)
        ]

        any_success = False
        for vk in MODIFIERS_TO_RELEASE:
            try:
                user_lib.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                any_success = True
            except Exception:
                pass
        return any_success
    except Exception as e:
        print(f"[-] Loi release_all_modifier_keys: {e}")
        return False

def force_bring_to_front(hwnd: int, u32=None) -> bool:
    """
    Dua cua so hwnd len foreground mot cach manh me va an toan tren Windows.
    Tuyet doi KHONG su dung fake Alt key (tranh 100% nguy co ket ban phim nguoi dung).
    Su dung co che Win32 native: SwitchToThisWindow, SetWindowPos (Z-order cycling),
    ShowWindow, SetForegroundWindow, va AttachThreadInput an toan voi khoi try/finally.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        u32 = u32 or user32 or (ctypes.windll.user32 if ctypes and hasattr(ctypes, "windll") else None)
        k32 = kernel32 or (ctypes.windll.kernel32 if ctypes and hasattr(ctypes, "windll") else None)
        if not u32:
            return False

        fg_hwnd = u32.GetForegroundWindow()
        if fg_hwnd == hwnd:
            return True

        # Unlock foreground restrictions on Windows
        try:
            u32.LockSetForegroundWindow(2)  # LSFW_UNLOCK = 2
            u32.AllowSetForegroundWindow(-1)  # ASFW_ANY = -1
        except Exception:
            pass

        # Use SwitchToThisWindow if available (clean, does not press any keys)
        try:
            if hasattr(u32, "SwitchToThisWindow"):
                u32.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass

        cur_thread = k32.GetCurrentThreadId() if k32 else 0
        fg_pid = wintypes.DWORD()
        fg_thread = u32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(fg_pid)) if fg_hwnd else 0
        target_pid = wintypes.DWORD()
        target_thread = u32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))

        attached_fg = False
        attached_target = False

        try:
            if cur_thread and fg_thread and fg_thread != cur_thread:
                attached_fg = bool(u32.AttachThreadInput(cur_thread, fg_thread, True))
            if cur_thread and target_thread and target_thread != cur_thread:
                attached_target = bool(u32.AttachThreadInput(cur_thread, target_thread, True))

            # Bring to top using SetWindowPos Z-order cycling without modifying size/position
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_SHOWWINDOW = 0x0040
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            HWND_TOP = 0
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW

            try:
                u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
                u32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                u32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, flags)
            except Exception:
                pass

            SW_RESTORE = 9
            SW_SHOW = 5
            if hasattr(u32, "IsIconic") and u32.IsIconic(hwnd):
                u32.ShowWindow(hwnd, SW_RESTORE)
            else:
                u32.ShowWindow(hwnd, SW_SHOW)

            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
        finally:
            if attached_fg:
                try:
                    u32.AttachThreadInput(cur_thread, fg_thread, False)
                except Exception:
                    pass
            if attached_target:
                try:
                    u32.AttachThreadInput(cur_thread, target_thread, False)
                except Exception:
                    pass

        time.sleep(0.15)
        fg_now = u32.GetForegroundWindow()
        return fg_now == hwnd or is_cursor_window(fg_now)
    except Exception as e:
        print(f"[-] Loi force_bring_to_front: {e}")
        return False

def _send_reload_keys(hwnd: int, auto_focus: bool = True, force_focus: bool = True) -> bool:
    """Gui to hop phim Ctrl+Shift+F11 toi cua so hwnd va tuy chon focus lai Composer (Ctrl+L)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32

        current_active = user32.GetForegroundWindow()
        if current_active != hwnd and not force_focus:
            return False

        VK_CONTROL = 0x11
        VK_SHIFT = 0x10
        VK_F11 = 0x7A
        KEYEVENTF_KEYUP = 0x0002

        try:
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_SHIFT, 0, 0, 0)
            user32.keybd_event(VK_F11, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        finally:
            release_all_modifier_keys(user32)

        if auto_focus:
            time.sleep(1.2)
            if user32.GetForegroundWindow() == hwnd or force_focus:
                try:
                    VK_L = 0x4C
                    user32.keybd_event(VK_CONTROL, 0, 0, 0)
                    user32.keybd_event(VK_L, 0, 0, 0)
                    time.sleep(0.05)
                    user32.keybd_event(VK_L, 0, KEYEVENTF_KEYUP, 0)
                    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
                finally:
                    release_all_modifier_keys(user32)

        return True
    except Exception as e:
        print(f"[-] Loi _send_reload_keys: {e}")
        return False


def reload_via_powershell(auto_focus: bool = True, force_focus: bool = True, reload_all: bool = False) -> bool:
    """
    Cap do 2: Kich hoat va gui to hop phim Ctrl+Shift+F11 an toan.
    Dac biet:
    - KHONG dung Alt (tranh 100% NVIDIA Overlay Alt+R).
    - Su dung force_bring_to_front de vuot qua gioi han foreground lock cua Windows.
    - Ho tro reload_all: reload tat ca cac cua so Cursor dang mo.
    """
    ensure_keybinding()

    if reload_all:
        windows = find_all_cursor_windows()
        if not windows:
            print("[*] Khong tim thay cua so Cursor nao de reload all.")
            return False
        success_count = 0
        for hwnd, title in windows:
            if force_focus:
                force_bring_to_front(hwnd)
            time.sleep(0.2)
            if _send_reload_keys(hwnd, auto_focus=auto_focus, force_focus=force_focus):
                success_count += 1
        print(f"[+] Da reload {success_count}/{len(windows)} cua so Cursor thanh cong!")
        return success_count > 0

    hwnd = find_cursor_window()
    if not hwnd:
        print("[*] Khong tim thay cua so Cursor dang mo tren desktop.")
        return False

    if force_focus:
        force_bring_to_front(hwnd)
    time.sleep(0.2)

    ok = _send_reload_keys(hwnd, auto_focus=auto_focus, force_focus=force_focus)
    if ok:
        print("[+] Da kich hoat va reload cua so Cursor thanh cong qua Win32 API (Ctrl+Shift+F11)!")
    return ok


def reload_all_cursor_windows(auto_focus: bool = True) -> Dict[str, Any]:
    """Reload tat ca cac cua so Cursor IDE dang mo de dong bo state.vscdb."""
    if reload_via_powershell(auto_focus=auto_focus, force_focus=True, reload_all=True):
        return {
            "success": True,
            "method": "powershell_win32_multi",
            "message": "Da reload tat ca cua so Cursor qua Ctrl+Shift+F11"
        }
    return {
        "success": False,
        "method": "none",
        "message": "Khong the reload cac cua so Cursor"
    }

def is_cursor_running() -> bool:
    """Kiem tra xem Cursor.exe co dang chay hay khong."""
    try:
        import psutil
        for p in psutil.process_iter(['name']):
            try:
                if p.info['name'] and p.info['name'].lower() == 'cursor.exe':
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    try:
        res = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Cursor.exe", "/NH"], capture_output=True, text=True, timeout=5)
        return "Cursor.exe" in res.stdout
    except Exception:
        return False

def get_cursor_exe_path() -> Optional[str]:
    """Tim duong dan file thuc thi Cursor.exe tren may tinh."""
    localappdata = os.getenv("LOCALAPPDATA")
    if localappdata:
        p = os.path.join(localappdata, "Programs", "cursor", "Cursor.exe")
        if os.path.exists(p):
            return p
    appdata = os.getenv("APPDATA")
    if appdata:
        p = os.path.abspath(os.path.join(appdata, "..", "Local", "Programs", "cursor", "Cursor.exe"))
        if os.path.exists(p):
            return p
    import shutil
    w = shutil.which("cursor")
    if w and os.path.exists(w):
        return w
    return None

def get_cursor_status() -> Dict[str, Any]:
    """Kiem tra toan dien trang thai tien trinh va cua so ung dung Cursor."""
    running = is_cursor_running()
    hwnd = find_cursor_window() if running else None
    window_title = ""
    if hwnd and sys.platform == "win32":
        try:
            import ctypes
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                window_title = buf.value
        except Exception:
            pass

    return {
        "is_running": running,
        "has_window": bool(hwnd),
        "hwnd": hwnd,
        "window_title": window_title,
        "exe_path": get_cursor_exe_path()
    }

def terminate_cursor(timeout_sec: float = 8.0) -> bool:
    """
    Tat hoan toan tien trinh Cursor.exe bang taskkill va psutil,
    cho doi cho den khi toan bo cac process con va chinh deu thoat han de nha file locks.
    """
    if not is_cursor_running():
        return True

    # 1. Thu taskkill /F /T /IM Cursor.exe
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/IM", "Cursor.exe"],
            capture_output=True,
            text=True,
            timeout=5
        )
    except Exception as e:
        print(f"[-] Loi taskkill: {e}")

    # 2. Quet psutil kill tat ca tien trinh cursor.exe con lai
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if proc.info['name'] and proc.info['name'].lower() == 'cursor.exe':
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as pe:
        print(f"[-] Loi psutil terminate: {pe}")

    # 3. Cho doi cac process thoat hoan toan
    start_t = time.time()
    while time.time() - start_t < timeout_sec:
        if not is_cursor_running():
            print("[+] Da tat hoan toan tien trinh Cursor.exe!")
            time.sleep(0.5)
            return True
        time.sleep(0.3)

    # 4. Fallback PowerShell Stop-Process neu con tien trinh bi treo
    if is_cursor_running():
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Stop-Process -Name 'Cursor' -Force -ErrorAction SilentlyContinue"],
                capture_output=True,
                timeout=5
            )
            time.sleep(0.5)
        except Exception:
            pass

    return not is_cursor_running()

def launch_cursor_app(wait_for_window: bool = True, timeout_sec: float = 15.0) -> bool:
    """
    Khoi dong Cursor.exe doc lap khoi Terminal Job Object,
    sau do tuy chon cho cua so xuat hien va dua len foreground.
    """
    cursor_exe = get_cursor_exe_path()
    if not cursor_exe or not os.path.exists(cursor_exe):
        print(f"[-] Khong tim thay Cursor.exe tai: {cursor_exe}")
        return False

    launched = False
    # Cach 1: CIM Win32_Process Create (tuyet doi khong bi kill khi parent thoat)
    try:
        escaped_exe = cursor_exe.replace("'", "''")
        cmd = f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='\"{escaped_exe}\" --reuse-window'}}; if ($r.ReturnValue -ne 0) {{ exit $r.ReturnValue }}"
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            launched = True
            print(f"[+] Da khoi dong Cursor.exe qua CIM tu: {cursor_exe}")
    except Exception as e:
        print(f"[-] Loi CIM: {e}")

    # Cach 2: os.startfile
    if not launched and hasattr(os, "startfile"):
        try:
            os.startfile(cursor_exe)
            launched = True
            print(f"[+] Da khoi dong Cursor.exe qua os.startfile tu: {cursor_exe}")
        except Exception as e:
            print(f"[-] Loi os.startfile: {e}")

    # Cach 3: subprocess.Popen detached
    if not launched:
        try:
            creationflags = 0
            if sys.platform == "win32":
                DETACHED_PROCESS = 0x00000008
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([cursor_exe, "--reuse-window"], creationflags=creationflags, close_fds=True)
            launched = True
            print(f"[+] Da khoi dong Cursor.exe qua Popen detached")
        except Exception as e:
            print(f"[-] Loi Popen: {e}")

    if not launched:
        return False

    if wait_for_window:
        start_wait = time.time()
        hwnd = None
        while time.time() - start_wait < timeout_sec:
            hwnd = find_cursor_window()
            if hwnd:
                break
            time.sleep(0.5)

        if hwnd:
            print(f"[+] Cua so Cursor da xuat hien (HWND: {hwnd}). Dua len foreground...")
            force_bring_to_front(hwnd)
            return True
        else:
            print("[*] Cursor da khoi dong nhung cua so chua hien trong thoi gian cho.")
            return is_cursor_running()

    return True

def launch_cursor_if_needed() -> bool:
    """Neu Cursor chua bat, khoi dong Cursor.exe doc lap khoi Terminal Job Object."""
    if is_cursor_running():
        return True
    return launch_cursor_app(wait_for_window=False)

def hard_restart_cursor(inject_callback=None, wait_for_window: bool = True, timeout_sec: float = 15.0) -> Dict[str, Any]:
    """
    Thuc hien Hard App Reset toan dien:
    1. Terminate Cursor.exe hoan toan.
    2. Cho process thoat sach de nha toan bo file locks (state.vscdb, storage.json).
    3. Chay inject_callback (nap token, spoof machine IDs) khi app da tat.
    4. Relaunch Cursor.exe cleanly.
    5. Cho cua so xuat hien va dua len foreground.
    """
    print("[HARD-RESET] Bat dau quy trinh Hard App Reset Cursor...")
    was_running = is_cursor_running()

    # 1. Terminate Cursor.exe
    if was_running:
        term_ok = terminate_cursor(timeout_sec=8.0)
        if not term_ok:
            print("[-] Canh bao: Khong the kill Cursor.exe hoan toan!")

    # 2. Inject callback (SQLite token injection & hardware spoofing)
    inject_ok = True
    if inject_callback and callable(inject_callback):
        try:
            inject_ok = inject_callback()
            print(f"[HARD-RESET] Inject callback executed (Success={inject_ok})")
        except Exception as e:
            print(f"[-] Loi khi chay inject_callback: {e}")
            inject_ok = False

    # 3. Relaunch Cursor.exe
    launch_ok = launch_cursor_app(wait_for_window=wait_for_window, timeout_sec=timeout_sec)

    return {
        "success": launch_ok,
        "method": "hard_restart",
        "was_running": was_running,
        "injected": inject_ok,
        "message": "Đã tắt hoàn toàn và khởi động lại Cursor thành công" if launch_ok else "Khởi động lại Cursor thất bại"
    }

def _get_clipboard_backup() -> Tuple[bool, Optional[str]]:
    """Sao luu nguyen ven text trong clipboard cua nguoi dung truoc khi paste prompt."""
    if sys.platform != "win32":
        return False, None
    try:
        import win32clipboard
        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                        data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                        return True, data
                    elif win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_TEXT):
                        data = win32clipboard.GetClipboardData(win32clipboard.CF_TEXT)
                        if isinstance(data, bytes):
                            data = data.decode("utf-8", errors="replace")
                        return True, data
                    has_formats = win32clipboard.CountClipboardFormats() > 0
                    if not has_formats:
                        return True, None
                    return False, None
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.05)
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard"],
            capture_output=True, text=True, timeout=3
        )
        if res.returncode == 0 and res.stdout is not None:
            return True, res.stdout.rstrip("\r\n")
    except Exception:
        pass

    return False, None

def _set_clipboard_text(text: str) -> bool:
    """Nap noi dung vao Windows clipboard."""
    if sys.platform != "win32":
        return False
    try:
        import win32clipboard
        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                    return True
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.05)
    except Exception:
        pass

    try:
        import base64
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        ps_cmd = f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Set-Clipboard -Value ([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64}')))"
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd], capture_output=True, timeout=5)
        return True
    except Exception:
        return False

def _restore_clipboard(has_backup: bool, backup_text: Optional[str]) -> bool:
    """Khoi phuc lai clipboard nguoi dung de tranh lam rac he thong."""
    if sys.platform != "win32":
        return True
    if has_backup and backup_text is not None:
        return _set_clipboard_text(backup_text)
    elif has_backup and backup_text is None:
        try:
            import win32clipboard
            for _ in range(2):
                try:
                    win32clipboard.OpenClipboard()
                    try:
                        win32clipboard.EmptyClipboard()
                        return True
                    finally:
                        win32clipboard.CloseClipboard()
                except Exception:
                    time.sleep(0.05)
        except Exception:
            pass

        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value $null"],
                capture_output=True, timeout=3
            )
            return True
        except Exception:
            pass
    # Neu not has_backup (nguoi dung dang co du lieu non-text nhu hinh anh, tep tin, screenshot):
    # Tuyet doi KHONG EmptyClipboard de bao ve du lieu cua nguoi dung tren host!
    return True

def _is_safe_cursor_focused(hwnd: int, u32) -> bool:
    """
    Kiem tra nghiem ngat xem cua so dang giu foreground tren desktop co thuc su la Cursor hay khong.
    Neu nguoi dung dang thao tac tren ung dung khac (browser, terminal, editor khac...)
    hoac cua so Cursor bi thu nho (minimized/iconic), ham se tra ve False ngay lap tuc.
    """
    if not hwnd or not u32:
        return False
    try:
        if hasattr(u32, "IsIconic"):
            try:
                val = u32.IsIconic(hwnd)
                if isinstance(val, (bool, int)) and bool(val):
                    return False
            except Exception:
                pass

        fg = u32.GetForegroundWindow()
        if not fg:
            return False
        if fg == hwnd:
            return True
        return is_cursor_window(fg)
    except Exception:
        return False

def _send_key_combo(u32, main_key: int, modifier_keys: List[int]) -> bool:
    """Gui to hop phim an toan tren Windows, luon giai phong modifier keys du gap loi."""
    KEYEVENTF_KEYUP = 0x0002
    main_key_down = False
    try:
        for mod in modifier_keys:
            u32.keybd_event(mod, 0, 0, 0)
            time.sleep(0.02)
        u32.keybd_event(main_key, 0, 0, 0)
        main_key_down = True
        time.sleep(0.04)
        u32.keybd_event(main_key, 0, KEYEVENTF_KEYUP, 0)
        main_key_down = False
        return True
    finally:
        if main_key_down:
            try:
                u32.keybd_event(main_key, 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
        for mod in reversed(modifier_keys):
            try:
                u32.keybd_event(mod, 0, KEYEVENTF_KEYUP, 0)
                time.sleep(0.02)
            except Exception:
                pass

def _send_single_key(u32, key: int) -> bool:
    """Gui mot phim don an toan tren Windows."""
    KEYEVENTF_KEYUP = 0x0002
    try:
        u32.keybd_event(key, 0, 0, 0)
        time.sleep(0.04)
        u32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False

def send_continue_prompt(
    prompt_text: str = "Tiếp tục",
    target_composer: bool = True,
    fresh_session: bool = True,
    restore_previous_focus: bool = True,
    respect_user_activity: bool = True,
    truncated_snippet: Optional[str] = None,
    target_file: Optional[str] = None
) -> Dict[str, Any]:
    """
    An toan tuyet doi cho may Host:
    1. Kiem tra xem nguoi dung co dang go phim/chuot o ung dung khac khong.
       Neu dang go, tri hoan dispatch de khong cuop chuot/phim lam anh huong nguoi dung.
    2. Ghi nho cua so dang active truoc do cua nguoi dung de hoan tra focus ngay sau khi paste.
    3. Bao ve nguyen ven clipboard nguoi dung (khong xoa nham du lieu hinh anh, file).
    4. Chi gui prompt khi cua so Cursor thuc su duoc xac thuc an toan tren OS.
    5. Luon giai phong toan bo modifier keys tren khoi finally.
    6. Ho tro ContextPreservingContinuationSynthesizer de neo vao doan code do dang truoc do.
    """
    if sys.platform != "win32":
        return {"success": False, "error": "Chi ho tro moi truong Windows"}

    if truncated_snippet:
        try:
            from ai_optimizer import ContextPreservingContinuationSynthesizer
            prompt_text = ContextPreservingContinuationSynthesizer.synthesize_prompt(
                truncated_response=truncated_snippet,
                target_file=target_file
            )
        except Exception:
            pass

    hwnd = find_cursor_window()
    if not hwnd:
        for _ in range(6):
            time.sleep(0.5)
            hwnd = find_cursor_window()
            if hwnd:
                break

    if not hwnd:
        return {"success": False, "error": "Khong tim thay cua so Cursor dang hoat dong"}

    try:
        u32 = user32 or (ctypes.windll.user32 if ctypes and hasattr(ctypes, "windll") else None)
        if not u32:
            return {"success": False, "error": "user32 API not available"}

        # 0. Kiem tra xem nguoi dung co dang thao tac tich cuc tren may khong
        if respect_user_activity:
            active_now, idle_s = is_user_actively_typing(idle_threshold_sec=2.5, u32=u32, k32=kernel32)
            if active_now:
                for _ in range(8):
                    time.sleep(0.25)
                    active_now, idle_s = is_user_actively_typing(idle_threshold_sec=2.0, u32=u32, k32=kernel32)
                    if not active_now:
                        break
                if active_now:
                    fg_cur = u32.GetForegroundWindow() if hasattr(u32, "GetForegroundWindow") else 0
                    if fg_cur != hwnd and not is_cursor_window(fg_cur):
                        print(f"[*] Nguoi dung dang go phim tren ung dung khac (idle: {idle_s:.1f}s). Hoan dispatch continue prompt de bao ve host.")
                        return {
                            "success": False,
                            "deferred": True,
                            "error": f"Nguoi dung dang thao tac tren may host (idle {idle_s:.1f}s), da hoan de khong lam phien"
                        }

        # 1. Kiem tra xem hwnd co hop le thuoc Cursor hay khong
        try:
            if u32.IsWindow(hwnd) and not is_cursor_window(hwnd):
                return {
                    "success": False,
                    "error": f"Cua so HWND {hwnd} khong phai la Cursor IDE hop le"
                }
        except Exception:
            pass

        # 2. Ghi nho cua so active hien tai cua nguoi dung de hoan tra sau khi gui prompt
        prev_fg = u32.GetForegroundWindow() if hasattr(u32, "GetForegroundWindow") else 0
        should_restore_focus = bool(restore_previous_focus and prev_fg and prev_fg != hwnd and not is_cursor_window(prev_fg))

        # 3. Dua Cursor len foreground va bat buoc kiem tra da focus thanh cong
        focused = force_bring_to_front(hwnd)
        if not focused:
            time.sleep(0.2)
            focused = force_bring_to_front(hwnd)

        if not focused:
            try:
                fg = u32.GetForegroundWindow()
                if fg == hwnd or is_cursor_window(fg):
                    focused = True
            except Exception:
                pass

        if not focused:
            print(f"[-] Khong the focus vao Cursor (HWND {hwnd}). Da huy dispatch prompt de tranh paste vao ung dung khac!")
            return {
                "success": False,
                "error": "Khong the focus vao cua so Cursor, da huy gui prompt de tranh paste vao ung dung khac"
            }

        text = (prompt_text or "Tiếp tục").strip()

        # 4. Sao luu clipboard cua nguoi dung truoc khi thay doi
        has_backup, backup_text = _get_clipboard_backup()
        clipboard_modified = False
        keys_dispatched = False

        try:
            # 5. Set clipboard cho prompt text
            if not _set_clipboard_text(text):
                return {"success": False, "error": "Khong the thiet lap Windows clipboard"}
            clipboard_modified = True

            time.sleep(0.15)

            # 6. Kiem tra lan nua: Foreground window phai van la Cursor!
            if not _is_safe_cursor_focused(hwnd, u32):
                print("[-] Cursor bi mat focus truoc khi paste! Huy dispatch de bao ve thiet bi nguoi dung.")
                return {
                    "success": False,
                    "error": "Cursor bi mat focus truoc khi paste, da huy de tranh paste ra ngoai"
                }

            VK_CONTROL = 0x11
            VK_I = 0x49
            VK_L = 0x4C
            VK_N = 0x4E
            VK_V = 0x56
            VK_RETURN = 0x0D

            target_key = VK_I if target_composer else VK_L

            # Kiem tra focus truoc khi mo Composer/Chat
            if not _is_safe_cursor_focused(hwnd, u32):
                print("[-] Cursor bi mat focus truoc khi mo composer/chat! Huy dispatch de bao ve thiet bi.")
                return {
                    "success": False,
                    "error": "Cursor bi mat focus truoc khi paste, da huy de tranh paste ra ngoai"
                }

            keys_dispatched = True
            _send_key_combo(u32, target_key, [VK_CONTROL])
            time.sleep(0.5)

            # Neu fresh_session: gui tiep Ctrl+N de tao phien moi
            if fresh_session:
                if not _is_safe_cursor_focused(hwnd, u32):
                    print("[-] Cursor bi mat focus truoc khi tao new session! Huy dispatch de bao ve thiet bi.")
                    return {
                        "success": False,
                        "error": "Cursor bi mat focus truoc khi paste, da huy de tranh paste ra ngoai"
                    }
                _send_key_combo(u32, VK_N, [VK_CONTROL])
                time.sleep(0.3)

            # 7. Kiem tra focus ngay truoc khi paste (Ctrl+V)
            if not _is_safe_cursor_focused(hwnd, u32):
                print("[-] Cursor bi mat focus ngay truoc khi paste! Huy dispatch de bao ve thiet bi nguoi dung.")
                return {
                    "success": False,
                    "error": "Cursor bi mat focus truoc khi paste, da huy de tranh paste ra ngoai"
                }

            _send_key_combo(u32, VK_V, [VK_CONTROL])
            time.sleep(0.1)

            # Khoi phuc ngay lap tuc noi dung clipboard cua nguoi dung
            if clipboard_modified:
                _restore_clipboard(has_backup, backup_text)
                clipboard_modified = False

            # 8. Kiem tra focus truoc khi nhan Enter de bat dau sinh code
            time.sleep(0.1)
            if not _is_safe_cursor_focused(hwnd, u32):
                print("[-] Cursor bi mat focus truoc khi nhan Enter! Huy dispatch de bao ve thiet bi nguoi dung.")
                return {
                    "success": False,
                    "error": "Cursor bi mat focus truoc khi nhan Enter, da huy de bao ve thiet bi nguoi dung"
                }

            _send_single_key(u32, VK_RETURN)
            time.sleep(0.05)

            # 9. Hoan tra focus lai cho ung dung truoc do cua nguoi dung tren host
            if should_restore_focus and hasattr(u32, "SetForegroundWindow"):
                try:
                    time.sleep(0.15)
                    u32.SetForegroundWindow(prev_fg)
                except Exception:
                    pass

            try:
                safe_text = text.encode("ascii", errors="replace").decode("ascii")
                print(f"[+] Da tu dong gui prompt tiep tuc ('{safe_text}') an toan vao Cursor {target_composer and 'Composer' or 'Chat'} thanh cong!")
            except Exception:
                pass

            return {
                "success": True,
                "prompt": text,
                "target": "composer" if target_composer else "chat"
            }
        finally:
            if clipboard_modified:
                try:
                    time.sleep(0.05)
                    _restore_clipboard(has_backup, backup_text)
                except Exception:
                    pass
            if keys_dispatched:
                release_all_modifier_keys(u32)
    except Exception as e:
        print(f"[-] Loi gui prompt tiep tuc: {e}")
        return {"success": False, "error": str(e)}

def trigger_cursor_reload(auto_focus: bool = True, reset_mode: Optional[str] = None) -> Dict[str, Any]:
    """
    Ham tong hop thuc hien quy trinh reload hoac hard reset app Cursor:
    1. Neu reset_mode == "hard_restart": Terminate Cursor.exe va khoi dong lai sach se.
    2. Neu reset_mode == "soft_reload" (hoac hybrid default):
       - Thu reload ngam qua Extension (khong chiem chuot/phim).
       - Neu Extension chua khoi dong, fallback sang Win32 API native (Ctrl+Shift+F11).
       - Neu Cursor chua chay, khoi dong Cursor.exe.
    """
    if reset_mode == "hard_restart":
        return hard_restart_cursor(wait_for_window=True)

    # 1. Thu Extension
    if reload_via_extension():
        return {
            "success": True,
            "method": "extension_silent",
            "message": "Da reload ngam thanh cong qua Extension Bridge"
        }
    
    # 2. Thu Win32 / PowerShell
    if reload_via_powershell(auto_focus=auto_focus, force_focus=True):
        return {
            "success": True,
            "method": "powershell_win32",
            "message": "Da kich hoat va reload qua to hop phim Ctrl+Shift+F11"
        }
    
    # 3. Kiem tra khoi dong
    if launch_cursor_app(wait_for_window=False):
        return {
            "success": True,
            "method": "launched_cursor",
            "message": "Cursor chua chay, da khoi dong Cursor.exe"
        }
        
    return {
        "success": False,
        "method": "none",
        "message": "Khong the reload hoac khoi dong Cursor"
    }

if __name__ == "__main__":
    res = trigger_cursor_reload()
    print("Ket qua:", res)
