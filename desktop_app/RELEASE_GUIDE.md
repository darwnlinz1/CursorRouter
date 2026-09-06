# 🚀 GitHub Release & Distribution Guide

This guide outlines how to release and distribute **CursorRouter** standalone desktop binaries on GitHub.

---

## Method 1: Automated Release via GitHub Actions (Recommended)

Whenever you push a version tag starting with `v` (e.g., `v2.5.0`), GitHub Actions will automatically:
1. Spin up a clean `windows-latest` virtual machine.
2. Install Python 3.11 and dependencies.
3. Run the complete automated test suite (`pytest tests/ -q`).
4. Compile `desktop_app/dist/CursorRouter.exe` using PyInstaller.
5. Create a GitHub Release and attach the executable and SHA-256 update `manifest.json`.

### Steps:
```bash
# 1. Create a version tag
git tag -a v2.5.0 -m "Release v2.5.0: Anti-detect hardware isolation, compact cards, and desktop app"

# 2. Push tag to GitHub
git push origin v2.5.0
```

---

## Method 2: Local Compilation & Manual Release

If you prefer compiling locally on your machine:

1. **Build the Standalone Binary:**
   - Double-click `desktop_app/BUILD_APP.bat` or run:
     ```bash
     cd desktop_app
     python build.py --windowed
     ```
   - PyInstaller compiles `desktop_app/dist/CursorRouter.exe` and outputs `desktop_app/dist/manifest.json`.

2. **Publish on GitHub:**
   - Open your browser to: [https://github.com/darwnlinz1/CursorRouter/releases/new](https://github.com/darwnlinz1/CursorRouter/releases/new)
   - Choose a tag (e.g. `v2.5.0`) and title (e.g. `CursorRouter v2.5.0 - Hardware Isolation & Desktop Cockpit`).
   - Drag and drop `desktop_app/dist/CursorRouter.exe` and `manifest.json` into the binary attachment area.
   - Click **"Publish release"**.