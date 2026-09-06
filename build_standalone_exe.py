"""
Standalone EXE Builder for Cursor Manager (PyInstaller)
======================================================
Compiles CursorThings native desktop application into a standalone Windows binary:
- Preserves templates, cookies folder, and static configurations.
- Bundles Edge WebView2 native runtime support.
- Generates build manifest with SHA-256 integrity hash for Auto-Updater.
"""

import os
import sys
import shutil
import hashlib
import json
import subprocess

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(ROOT_DIR, "dist")
BUILD_DIR = os.path.join(ROOT_DIR, "build")


def calculate_sha256(filepath: str) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_executable(onefile: bool = True, windowed: bool = False):
    print("=" * 65)
    print("  🚀 CURSOR MANAGER - STANDALONE WINDOWS BUILDER")
    print("=" * 65)

    entrypoint = os.path.join(ROOT_DIR, "main.py")
    if not os.path.exists(entrypoint):
        print(f"[-] Entrypoint not found: {entrypoint}")
        return False

    # Check PyInstaller
    try:
        import PyInstaller
        print(f"[+] PyInstaller version: {PyInstaller.__version__}")
    except ImportError:
        print("[*] PyInstaller not detected in current environment. Installing...")
        res = subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=False)
        if res.returncode != 0:
            print("[-] Failed to install PyInstaller via pip.")
            return False

    templates_dir = os.path.join(ROOT_DIR, "templates")
    cookies_dir = os.path.join(ROOT_DIR, "Cookies")
    src_dir = os.path.join(ROOT_DIR, "src")

    # Construct PyInstaller command line
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "CursorManager",
        "--clean",
        "--noconfirm",
    ]

    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    if windowed:
        cmd.append("--windowed")
    else:
        cmd.append("--console")

    # Add data directories (Windows uses semicolon separator)
    if os.path.exists(templates_dir):
        cmd.extend(["--add-data", f"{templates_dir};templates"])
    if os.path.exists(cookies_dir):
        cmd.extend(["--add-data", f"{cookies_dir};Cookies"])
    if os.path.exists(src_dir):
        cmd.extend(["--add-data", f"{src_dir};src"])

    # Hidden imports
    hidden_imports = [
        "flask",
        "jinja2",
        "requests",
        "psutil",
        "sqlite3",
        "ctypes",
        "webview",
        "ai_optimizer",
        "updater",
        "cursor_settings",
        "account_pool",
        "cursor_reloader",
        "rotating_proxy",
        "token_pool",
        "smart_task_filter",
        "chat_lock_detector"
    ]
    for hi in hidden_imports:
        cmd.extend(["--hidden-import", hi])

    cmd.extend(["--paths", src_dir])
    cmd.extend(["--paths", ROOT_DIR])
    cmd.append(entrypoint)

    print(f"[*] Executing PyInstaller build...")
    print(" ".join(cmd))
    res = subprocess.run(cmd, cwd=ROOT_DIR)

    if res.returncode != 0:
        print(f"[-] PyInstaller build failed with exit code {res.returncode}")
        return False

    # Check generated executable
    target_exe = os.path.join(DIST_DIR, "CursorManager.exe")
    if not os.path.exists(target_exe):
        target_exe = os.path.join(DIST_DIR, "CursorManager", "CursorManager.exe")

    if os.path.exists(target_exe):
        size_mb = os.path.getsize(target_exe) / (1024 * 1024)
        sha256_hash = calculate_sha256(target_exe)
        print("\n" + "=" * 65)
        print(f"  ✅ BUILD SUCCESSFUL!")
        print(f"  📦 Binary: {target_exe}")
        print(f"  📏 Size:   {size_mb:.2f} MB")
        print(f"  🔒 SHA256: {sha256_hash}")
        print("=" * 65)

        # Generate update manifest for distribution
        manifest = {
            "version": "2.4.0",
            "release_date": "2026-09-06",
            "channel": "stable",
            "download_url": f"CursorManager.exe",
            "sha256": sha256_hash,
            "mandatory": False,
            "min_version": "2.0.0",
            "changelog": "1. Two-tier quota pool (<50% Tier 1, 50-99% Tier 2).\n2. Authentic 8-category Cursor settings taxonomy.\n3. Zero stuck Alt/modifier keys guarantee.\n4. Native Edge WebView2 container.\n5. Windows auto-updater with SHA-256 verification."
        }
        manifest_path = os.path.join(DIST_DIR, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"  📄 Manifest: {manifest_path}\n")
        return True
    else:
        print(f"[-] Build target not found in {DIST_DIR}")
        return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build CursorManager Standalone Windows Binary")
    parser.add_argument("--onedir", action="store_true", help="Build directory instead of single-file executable")
    parser.add_argument("--windowed", action="store_true", help="Hide console window on startup")
    args = parser.parse_args()

    success = build_executable(onefile=not args.onedir, windowed=args.windowed)
    sys.exit(0 if success else 1)
