import argparse
import os
import sys
import webbrowser

# Ensure UTF-8 console output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SRC_DIR)
for _p in (_SRC_DIR, _ROOT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from account_pool import AccountPoolManager
from cursor_storage import CursorStorageManager
from pkce_auth import CursorOAuthPKCEClient


def main():
    parser = argparse.ArgumentParser(
        description="CursorRouter v3.1 — Pure OAuth PKCE Account & Session Manager",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("--status", action="store_true", help="Kiem tra trang thai tai khoan hien tai trong Cursor")
    parser.add_argument("--oauth-login", action="store_true", help="Dang nhap them tai khoan moi qua Cursor OAuth PKCE chinh thuc")
    parser.add_argument("--capture", action="store_true", help="Luu phien Cursor IDE dang dang nhap (tu dong giai ma DPAPI safeStorage) vao pool")
    parser.add_argument("--sync-profile", action="store_true", help="Dong bo toan dien Profile (Email, Name, AuthID, Quota) cho token dang dung")
    parser.add_argument("--serve", action="store_true", help="Khoi dong Web Dashboard Host tai http://127.0.0.1:7860")
    parser.add_argument("--auto-switch", action="store_true", help="Tu dong tim tai khoan OAuth co quota tot nhat va nap vao Cursor")
    parser.add_argument("--inject-only", type=str, help="Ghi truc tiep 1 OAuth JWT AccessToken vao Cursor state.vscdb")
    parser.add_argument("--clean-exhausted", action="store_true", help="Xoa toan bo tai khoan da can quota (>= 100%%) khoi pool")

    args = parser.parse_args()

    storage = CursorStorageManager()
    pool = AccountPoolManager()

    if args.status:
        info = storage.get_active_account()
        print("=== TRANG THAI CURSOR HIEN TAI ===")
        print(f"Email dang nhap:    {info.get('email') or 'Chua dang nhap'}")
        print(f"Ten hien thi:       {info.get('displayName') or 'Khong co'}")
        print(f"Auth ID:            {info.get('authId') or 'Khong co'}")
        print(f"Da co Access Token: {'Co' if info.get('has_token') else 'Khong'}")
        print(f"Loai Token:         {info.get('token_type') or 'unknown'}")
        print(f"SafeStorage Ma Hoa: {'Co' if info.get('safe_storage_encrypted') else 'Khong'}")
        print(f"Goi dich vu (Tier): {info.get('membership_type') or 'Free'}")
        print(f"Duong dan database: {storage.db_path}")
        return

    if args.oauth_login:
        client = CursorOAuthPKCEClient()
        sess = client.create_login_session()
        print("\n=== DANG NHAP CURSOR OAUTH 2.0 PKCE ===")
        print(f"Link dang nhap chinh thuc:\n  {sess['login_url']}\n")
        try:
            webbrowser.open(sess["login_url"])
            print("[*] Da mo trinh duyet. Hay xac nhan 'Yes, Log In' (hoac copy link dan vao tab An danh)...")
        except Exception:
            print("[*] Hay mo link tren bang trinh duyet cua ban va xac nhan dang nhap...")
        tokens = client.poll_until_complete(sess["uuid"], sess["verifier"], timeout_sec=240.0)
        if tokens and tokens.get("accessToken"):
            saved = pool.upsert_session_account(
                access_token=tokens["accessToken"],
                refresh_token=tokens.get("refreshToken"),
                source="oauth_pkce",
            )
            if saved.get("success"):
                print(f"[+] Thanh cong! Da luu phien OAuth IDE: {saved.get('email')} ({saved.get('status')}, {saved.get('usage_percent')}%)")
            else:
                print(f"[-] Khong the luu phien OAuth: {saved.get('error')}")
        else:
            print("[-] Het thoi gian cho hoac dang nhap bi tu choi.")
        return

    if args.capture:
        print("[*] Dang doc & giai ma phien Cursor IDE hien tai (DPAPI safeStorage)...")
        res = pool.capture_active_cursor_session()
        if res.get("success"):
            print(f"[+] {res.get('message')}")
        else:
            print(f"[-] {res.get('error')}")
        return

    if args.sync_profile:
        print("[*] Dang lay token hien tai de dong bo profile tu API...")
        active = storage.get_active_account()
        token = active.get("access_token")
        if not token:
            print("[-] Khong tim thay Access Token trong Cursor!")
            return
        success = storage.inject_full_profile(token, active.get("refresh_token"))
        if success:
            updated = storage.get_active_account()
            print("[+] Dong bo Profile hoan tat:")
            print(f" - Email:       {updated.get('email')}")
            print(f" - Ten Profile: {updated.get('displayName')}")
            print(f" - Auth ID:     {updated.get('authId')}")
        return

    if args.auto_switch:
        print("[*] Dang tim kiem tai khoan OAuth con quota tot nhat trong Pool...")
        best = pool.auto_switch_best_account()
        if best:
            print(f"[+] Da nap thanh cong tai khoan: {best.get('email')} (Usage: {best.get('usage_percent')}%)")
        else:
            print("[-] Khong con tai khoan OAuth kha dung trong pool.")
        return

    if args.serve:
        import server
        server.start_background_services()
        server.app.run(
            host=os.getenv("CURSOR_HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "7860")),
            debug=False,
            threaded=True,
        )
        return

    if args.inject_only:
        storage.inject_full_profile(args.inject_only)
        print("[!] Da nap OAuth token va dong bo profile vao state.vscdb.")
        return

    if args.clean_exhausted:
        print(f"[*] Dang xoa toan bo tai khoan can quota (>= {pool.QUOTA_EXHAUSTION_THRESHOLD}%)...")
        deleted = pool.delete_exhausted_accounts()
        print(f"[+] Da don dep thanh cong {deleted} phien tai khoan het quota!")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
