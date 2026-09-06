import sys
import os
import argparse
import json

# Dam bao in tieng Viet tren Windows khong bi loi cp1252
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

from pkce_auth import CursorAuthClient, parse_cookie_from_file
from cursor_storage import CursorStorageManager
from account_pool import AccountPoolManager

def main():
    parser = argparse.ArgumentParser(
        description="Cursor Account & Session Manager",
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    parser.add_argument("--status", action="store_true", help="Kiem tra trang thai tai khoan hien tai trong Cursor")
    parser.add_argument("--sync-profile", action="store_true", help="Dong bo toan dien Profile (Email, Name, AuthID, Quota) cho token dang dung de het bi lech thong tin")
    parser.add_argument("--serve", action="store_true", help="Khoi dong Web Dashboard Host tai http://localhost:7860")
    parser.add_argument("--scan", action="store_true", help="Quet folder Cookies/ de nap tat ca account vao Pool")
    parser.add_argument("--auto-switch", action="store_true", help="Tu dong tim account co quota tot nhat va nap vao Cursor")
    parser.add_argument("--test-cookie", type=str, help="Kiem tra doi 1 Cookie sang Token (truyen chuoi cookie hoac duong dan file .txt)")
    parser.add_argument("--inject-only", type=str, help="Ghi truc tiep 1 JWT AccessToken vao Cursor state.vscdb")
    parser.add_argument("--clean-exhausted", action="store_true", help="Xoa toan bo tai khoan da can quota (>= 100%%) va xoa file cookie tuong ung")
    
    args = parser.parse_args()

    storage = CursorStorageManager()
    pool = AccountPoolManager(cookies_dir="Cookies")

    if args.status:
        info = storage.get_active_account()
        print("=== TRANG THAI CURSOR HIEN TAI ===")
        print(f"Email dang nhap:    {info.get('email') or 'Chua dang nhap'}")
        print(f"Ten hien thi:       {info.get('displayName') or 'Khong co'}")
        print(f"Auth ID:            {info.get('authId') or 'Khong co'}")
        print(f"Da co Access Token: {'Co' if info.get('has_token') else 'Khong'}")
        print(f"Goi dich vu (Tier): {info.get('membership_type') or 'Free'}")
        print(f"Duong dan database: {storage.db_path}")
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
            print("\n[!] Hay vao Cursor nhan Ctrl+Shift+P -> 'Developer: Reload Window' de xem profile cap nhat 100%!")
        return

    if args.scan:
        print("[*] Dang quet thu muc Cookies/...")
        count = pool.scan_cookies_folder()
        print(f"[+] Da quet xong! Them moi {count} tai khoan vao database pool.")
        return

    if args.auto_switch:
        print("[*] Dang tim kiem tai khoan con quota tot nhat trong Pool...")
        best = pool.auto_switch_best_account()
        if best:
            print(f"[+] Da nap thanh cong tai khoan: {best.get('email')} (Usage: {best.get('usage_percent')}%)")
            print("[!] Hay vao Cursor nhan Ctrl+Shift+P -> 'Developer: Reload Window' de tiep tuc code!")
        else:
            print("[-] Khong con tai khoan kha dung trong pool.")
        return

    if args.serve:
        import server
        server.app.run(host="127.0.0.1", port=7860, debug=False)
        return

    if args.test_cookie:
        target = args.test_cookie
        if os.path.exists(target):
            print(f"[*] Dang doc cookie tu file: {target}")
            cookie_val = parse_cookie_from_file(target)
        else:
            cookie_val = target

        print(f"[*] WorkosCursorSessionToken preview: {cookie_val[:25]}... (Do dai: {len(cookie_val)})")
        
        client = CursorAuthClient()
        tokens = client.exchange_cookie_to_tokens(cookie_val)

        if tokens:
            print("\n[V] KET QUA: DOI TOKEN THANH CONG!")
            print(f"Access Token:  {tokens.get('accessToken', '')[:35]}...")
            print(f"Refresh Token: {tokens.get('refreshToken', '')[:35]}...")
            
            confirm = input("\nBan co muon nap ngay Token nay vao Cursor khong? (y/n): ")
            if confirm.lower() == 'y':
                storage.inject_full_profile(tokens['accessToken'], tokens.get('refreshToken'))
                print("\n[!] Hay vao Cursor va nhan: Ctrl + Shift + P -> 'Developer: Reload Window' de ap dung ngay ma khong mat Chat/Memory!")
        else:
            print("\n[X] That bai: Khong the lay token tu cookie nay.")
        return

    if args.inject_only:
        storage.inject_full_profile(args.inject_only)
        print("[!] Da nap token va dong bo profile. Hay Reload Window Cursor de kich hoat.")
        return

    if args.clean_exhausted:
        print(f"[*] Dang xoa toan bo tai khoan can quota (>= {pool.QUOTA_EXHAUSTION_THRESHOLD}%)...")
        deleted = pool.delete_exhausted_accounts()
        print(f"[+] Da don dep thanh cong {deleted} tai khoan va file cookie vat ly!")
        return

    parser.print_help()

if __name__ == "__main__":
    main()
