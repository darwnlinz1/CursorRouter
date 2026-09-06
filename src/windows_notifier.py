import os
import sys
import html
import base64
import subprocess
import threading
from typing import Optional

def _run_powershell_toast(title: str, message: str, app_id: str = "Cursor Manager") -> bool:
    """Gui thong bao native Toast Notification tren Windows thong qua PowerShell."""
    if sys.platform != "win32":
        return False

    try:
        safe_title = html.escape(title)
        safe_msg = html.escape(message)
        safe_app = html.escape(app_id)

        ps_script = f"""
try {{
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $xml = @"
<toast duration="short">
    <visual>
        <binding template="ToastGeneric">
            <text>{safe_title}</text>
            <text>{safe_msg}</text>
        </binding>
    </visual>
    <audio src="ms-winsoundevent:Notification.Default" />
</toast>
"@
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{safe_app}')
    $notifier.Show($toast)
}} catch {{
    try {{
        Add-Type -AssemblyName System.Windows.Forms
        $balloon = New-Object System.Windows.Forms.NotifyIcon
        $balloon.Icon = [System.Drawing.SystemIcons]::Information
        $balloon.BalloonTipTitle = "{safe_title}"
        $balloon.BalloonTipText = "{safe_msg}"
        $balloon.Visible = $true
        $balloon.ShowBalloonTip(4000)
    }} catch {{}}
}}
"""
        encoded = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creation_flags
        )
        return res.returncode == 0
    except Exception as e:
        print(f"[-] Toast Notification error: {e}")
        return False

def send_windows_notification(title: str, message: str, app_id: str = "Cursor Manager", async_exec: bool = True) -> bool:
    """
    Gui thong bao Windows Native Toast.
    Neu async_exec=True (mac dinh), thuc thi trong background thread de khong block luong chinh.
    """
    if sys.platform != "win32":
        return False

    if async_exec:
        t = threading.Thread(target=_run_powershell_toast, args=(title, message, app_id), daemon=True)
        t.start()
        return True
    else:
        return _run_powershell_toast(title, message, app_id)

if __name__ == "__main__":
    ok = send_windows_notification("Cursor Account Rotated", "Switched from userA@test.com to userB@test.com (15.0%)", async_exec=False)
    print("Notification sent:", ok)
