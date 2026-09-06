"""
CursorRouter - Standalone Windows Binary Builder
================================================
Compiles CursorRouter desktop application into a standalone Windows binary
with Edge WebView2 support, resource bundling, and SHA-256 update manifests.
"""

import os
import sys
import shutil
import hashlib
import json
import subprocess

DESKTOP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(DESKTOP_DIR, '..'))
DIST_DIR = os.path.join(DESKTOP_DIR, 'dist')
BUILD_DIR = os.path.join(DESKTOP_DIR, 'build')


def calculate_sha256(filepath: str) -> str:
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_desktop_app(onefile: bool = True, windowed: bool = False) -> bool:
    print('=' * 65)
    print('  ?? CURSORROUTER - DESKTOP STANDALONE BINARY BUILDER')
    print('=' * 65)

    entrypoint = os.path.join(DESKTOP_DIR, 'desktop_main.py')
    if not os.path.exists(entrypoint):
        entrypoint = os.path.join(ROOT_DIR, 'native_app.py')

    try:
        import PyInstaller
        print(f'[+] PyInstaller version: {PyInstaller.__version__}')
    except ImportError:
        print('[*] PyInstaller not detected. Installing via pip...')
        res = subprocess.run([sys.executable, '-m', 'pip', 'install', 'pyinstaller'], check=False)
        if res.returncode != 0:
            print('[-] Failed to install PyInstaller.')
            return False

    templates_dir = os.path.join(ROOT_DIR, 'templates')
    cookies_dir = os.path.join(ROOT_DIR, 'Cookies')
    src_dir = os.path.join(ROOT_DIR, 'src')

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--name', 'CursorRouter',
        '--clean',
        '--noconfirm',
        '--distpath', DIST_DIR,
        '--workpath', BUILD_DIR,
    ]

    if onefile:
        cmd.append('--onefile')
    else:
        cmd.append('--onedir')

    if windowed:
        cmd.append('--windowed')
    else:
        cmd.append('--console')

    if os.path.exists(templates_dir):
        cmd.extend(['--add-data', f'{templates_dir};templates'])
    if os.path.exists(cookies_dir):
        cmd.extend(['--add-data', f'{cookies_dir};Cookies'])
    if os.path.exists(src_dir):
        cmd.extend(['--add-data', f'{src_dir};src'])

    hidden_imports = [
        'flask', 'jinja2', 'requests', 'psutil', 'sqlite3', 'ctypes', 'webview',
        'ai_optimizer', 'updater', 'cursor_settings', 'account_pool',
        'cursor_reloader', 'rotating_proxy', 'token_pool', 'smart_task_filter',
        'chat_lock_detector'
    ]
    for hi in hidden_imports:
        cmd.extend(['--hidden-import', hi])

    cmd.extend(['--paths', src_dir])
    cmd.extend(['--paths', ROOT_DIR])
    cmd.extend(['--paths', DESKTOP_DIR])
    cmd.append(entrypoint)

    print('[*] Compiling standalone Windows binary...')
    res = subprocess.run(cmd, cwd=DESKTOP_DIR)
    if res.returncode != 0:
        print(f'[-] PyInstaller build failed with exit code {res.returncode}')
        return False

    target_exe = os.path.join(DIST_DIR, 'CursorRouter.exe')
    if not os.path.exists(target_exe):
        target_exe = os.path.join(DIST_DIR, 'CursorRouter', 'CursorRouter.exe')

    if os.path.exists(target_exe):
        size_mb = os.path.getsize(target_exe) / (1024 * 1024)
        sha256_hash = calculate_sha256(target_exe)
        print('\n' + '=' * 65)
        print('  ? BUILD SUCCESSFUL!')
        print(f'  ?? Binary: {target_exe}')
        print(f'  ?? Size:   {size_mb:.2f} MB')
        print(f'  ?? SHA256: {sha256_hash}')
        print('=' * 65)

        manifest = {
            'version': '2.5.0',
            'release_date': '2026-09-06',
            'channel': 'stable',
            'download_url': 'CursorRouter.exe',
            'sha256': sha256_hash,
            'mandatory': False,
            'min_version': '2.0.0',
            'changelog': '1. Anti-detect hardware fingerprint isolation.\n2. Compact accounts view with email masking and instant switch.\n3. Dedicated desktop app build pipeline.\n4. Real-time quota metrics and zero-stuck modifier keys.\n5. Full 8-category Cursor settings taxonomy.'
        }
        manifest_path = os.path.join(DIST_DIR, 'manifest.json')
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f'  ?? Manifest: {manifest_path}\n')
        return True
    else:
        print(f'[-] Target binary not found in {DIST_DIR}')
        return False


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Build CursorRouter Standalone Windows Binary')
    parser.add_argument('--onedir', action='store_true', help='Build directory instead of single-file executable')
    parser.add_argument('--windowed', action='store_true', help='Hide console window on startup')
    args = parser.parse_args()
    success = build_desktop_app(onefile=not args.onedir, windowed=args.windowed)
    sys.exit(0 if success else 1)
