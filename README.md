# ?? CursorRouter

<p align="center">
  <a href="https://github.com/darwnlinz1/CursorRouter/releases"><img src="https://img.shields.io/github/v/release/darwnlinz1/CursorRouter?style=flat-square&color=3b82f6" alt="Latest Release"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/tests-309%20passed-10b981?style=flat-square" alt="Tests"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-3b82f6?style=flat-square" alt="Python 3.10+"></a>
  <a href="https://cursor.com/"><img src="https://img.shields.io/badge/Cursor%20IDE-v0.45%2B-000000?style=flat-square" alt="Cursor IDE"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-64748b?style=flat-square" alt="License MIT"></a>
  <a href="https://microsoft.com"><img src="https://img.shields.io/badge/platform-Windows%2010%2F11-0284c7?style=flat-square" alt="Windows 10/11"></a>
</p>

<p align="center">
  <b>Production-grade local account pool manager, two-tier quota router, anti-detect profile isolator, and desktop cockpit built exclusively for Cursor IDE.</b>
</p>

---

## ?? Downloads & Releases

Pre-compiled standalone Windows executables are available on the GitHub Releases page:

| Platform | Architecture | Package Type | Download Link |
| :--- | :--- | :--- | :--- |
| **Windows 10 / 11** | x64 | Standalone .exe (WebView2) | [Download Latest Release](https://github.com/darwnlinz1/CursorRouter/releases/latest) |
| **Windows 10 / 11** | x64 | Portable Source Archive | [Source Code (zip)](https://github.com/darwnlinz1/CursorRouter/archive/refs/heads/main.zip) |

*For local building and custom packaging, see the [Desktop App Packaging Guide](desktop_app/README.md).*

---

## ?? Key Features

- ??? **Anti-Detect Hardware Isolation**: Binds persistent, randomized hardware fingerprints (machineId, macMachineId, devDeviceId, sqmId) to each individual account in SQLite, preventing Cursor telemetry from linking multiple accounts to a single machine identity.
- ? **Two-Tier Priority Routing Engine**:
  - **Tier 1 (Priority Pool - Quota < 50%)**: Dispatched first for blazing fast AI completions.
  - **Tier 2 (Fallback Pool - Quota 50% - 99%)**: Utilized when Tier 1 is exhausted, maximizing Cursor's 5-hour rolling slow queue.
  - **7-Day Cooldown Quarantine**: Accounts reaching 100% or encountering chat lock are automatically blacklisted for 7 days, then released once quota resets.
- ?? **Seamless Cookie Dropzone**: Auto-generates Cookies/ directory. Drop .txt cookie files and convert them via **12 parallel worker threads** into 60-day access tokens.
- ?? **Zero Context Loss**: Modifies only globalStorage/state.vscdb auth credentials without touching workspaceStorage. Chats, composer history, and file context remain 100% intact across switches.
- ?? **Keyboard Safety & Keystroke Isolation**: Eliminates fake Alt key hacks with guaranteed Win32 modifier unstick (
elease_all_modifier_keys in inally:). Prompt dispatch strictly targets verified Cursor.exe HWND with inactive typing checks (>2.5s).
- ??? **Full 8-Category Cursor Settings Taxonomy**: 100% bi-directional sync with %APPDATA%\\Cursor\\User\\settings.json across General, Appearance, Agent, Git & PRs, Worktrees, Browser & Network, Tab, and Code Intelligence.
- ??? **Minimalist Web & Native Desktop Cockpit**: High-density compact account cards with email masking (???), real-time quota gauges, live sync clocks, and standalone Microsoft Edge WebView2 packaging in desktop_app/.

---

## ??? System Architecture

`mermaid
flowchart TD
    subgraph Ingestion ["1. Account Ingestion & Cryptography"]
        CK["Cookies/*.txt (Dropzone)"] -->|12 Parallel Threads| PKCE["PKCE Auth Exchange (SHA-256)"]
        PKCE -->|HMAC Machine Key| DB[("cursor_accounts.db (SQLite WAL)")]
    end

    subgraph Routing ["2. Intelligent Two-Tier Routing Engine"]
        DB --> POOL["Two-Tier Account Pool"]
        POOL -->|Quota < 50%| T1["Tier 1: Fast Request Pool"]
        POOL -->|50% <= Quota < 100%| T2["Tier 2: Fallback Slow Pool"]
        POOL -->|Quota >= 100% / Lock| CD["7-Day Quarantine Cooldown"]
    end

    subgraph AntiDetect ["3. Anti-Detect Hardware Isolation Engine"]
        AD["Anti-Detect Profile Manager"] -->|UUID / SHA-256| FP["Per-Account Fingerprint"]
        FP -->|Inject on Switch| SJ["%APPDATA%/Cursor/User/globalStorage/storage.json"]
        SJ -.->|Spoofs| HIDS["machineId, macMachineId, devDeviceId, sqmId"]
    end

    subgraph Execution ["4. Execution & IDE Orchestration"]
        T1 & T2 --> ROUTER["Account Switch Coordinator"]
        ROUTER --> AD
        ROUTER -->|Inject Auth Tokens| DBVSC["globalStorage/state.vscdb"]
        ROUTER -->|CIM / WMI Process Control| CEXE["Cursor.exe IDE"]
        CEXE -->|Transparent Interception| PROXY["Connect-RPC Streaming Proxy (:8999)"]
    end
`

---

## ??? Anti-Detect Hardware Fingerprint Isolation

Standard browser and desktop extensions share the host operating system's hardware identifiers, allowing telemetry providers to link multiple free/trial accounts back to the same machine. **CursorRouter** provides deep hardware isolation inspired by anti-detect browsers (such as Dolphin{anty} and AdsPower):

`json
{
  "telemetry.machineId": "auth0|user_7a4f9b8c2e1d034a5f6e8b7c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e",
  "telemetry.macMachineId": "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2",
  "telemetry.devDeviceId": "8f3b2a1c-4e5d-6f7a-8b9c-0d1e2f3a4b5c",
  "telemetry.sqmId": "{3B7F1C9A-4D5E-6A7B-8C9D-0E1F2A3B4C5D}"
}
`

### Supported Modes:
1. **Account-Locked Profile (Recommended)**: Each account has a unique, deterministic fingerprint permanently saved in SQLite. Whenever you switch to that account, its exact hardware identity is restored in storage.json.
2. **Stealth Randomization**: Generates fresh, random hardware IDs on every single switch.
3. **Native Host Standard**: Preserves your existing machine IDs without modification.

---

## ?? Project Structure

`
CursorRouter/
??? START_HOST.bat               # 1-Click launcher: Flask background server + Browser cockpit
??? START_NATIVE_APP.bat         # 1-Click launcher: Native Edge WebView2 desktop application
??? main.py                      # Unified Python CLI & server entry point
??? auto_switch_config.json      # Configuration: auto-switch, reset mode, anti-detect settings
??? cursor_config_profiles.json  # Exportable/importable 8-category Cursor setting profiles
??? pytest.ini                   # Pytest automation configuration
??? LICENSE                      # MIT Open Source License
??? .gitignore                   # Security exclusion rules (zero leaked keys/databases)
?
??? desktop_app/                 # ??? Isolated Native Desktop Application Package
?   ??? desktop_main.py          # Native WebView2 controller & lifecycle manager
?   ??? build.py                 # Standalone PyInstaller compiler & SHA-256 manifest generator
?   ??? BUILD_APP.bat            # 1-Click Windows binary builder
?   ??? START_DESKTOP.bat        # 1-Click desktop application launcher
?   ??? requirements.txt         # Desktop-specific dependencies (pywebview, pyinstaller)
?   ??? README.md                # Desktop package documentation
?
??? src/                         # ?? Core Architecture
?   ??? account_pool.py          # SQLite WAL account pool, two-tier priority, 7-day quarantine
?   ??? cursor_settings.py       # 8-category settings taxonomy & anti-detect fingerprinting
?   ??? cursor_reloader.py       # Win32 window automation, process reboot, modifier unstick
?   ??? cursor_storage.py        # Token injection into globalStorage/state.vscdb
?   ??? server.py                # REST API backend & static cockpit routes
?   ??? updater.py               # Detached Windows auto-updater with SHA-256 verification
?   ??? ai_optimizer.py          # EMA token velocity predictor & tail-anchor prompt generator
?   ??? chat_lock_detector.py    # 4-layer chat lockout & Connect-RPC quota error detector
?   ??? smart_task_filter.py     # Continuation filter (interrupted task vs finished task)
?   ??? token_pool.py            # Sub-millisecond in-memory token cache for proxy routing
?   ??? rotating_proxy.py        # Transparent streaming Connect-RPC proxy (:8999)
?   ??? windows_notifier.py      # Win32 Toast Notifications with XML escaping
?   ??? pkce_auth.py             # PKCE code_challenge / verifier authentication client
?   ??? token_cipher.py          # HMAC-SHA256 token encryption for SQLite security
?
??? templates/                   # Frontend UI
?   ??? index.html               # Minimalist HUD Cockpit (Compact Cards, View Toggle, Settings Modal)
??? Cookies/                     # User cookies dropzone directory (.gitignored)
?   ??? .gitkeep                 # Preserved folder placeholder
??? tests/                       # Automated Pytest Suite (309 tests, 100% pass rate)
`

---

## ?? Getting Started

### Prerequisites
- **Windows 10 / 11** (64-bit)
- **Python 3.10+** (with pip)
- **Cursor IDE** installed at %LOCALAPPDATA%\\Programs\\cursor\\Cursor.exe

### 1. Installation
Clone the repository and install core dependencies:
`ash
git clone https://github.com/darwnlinz1/CursorRouter.git
cd CursorRouter
pip install -r desktop_app/requirements.txt
`

### 2. Launching Cockpit

- **Web Dashboard Mode**:
  `ash
  START_HOST.bat
  `
  Or run python main.py. Cockpit opens automatically at http://127.0.0.1:7860.

- **Native Desktop App Mode**:
  `ash
  START_NATIVE_APP.bat
  `
  Or run python desktop_app/desktop_main.py. Opens as a standalone window using Microsoft Edge WebView2.

---

## ?? Cookie Ingestion Workflow

1. Click **"?? Open Cookies Folder"** on the dashboard. Windows Explorer will open the local Cookies/ directory.
2. Drop your exported Netscape or raw cookie .txt files into Cookies/.
3. Click **"? Batch Scan & Convert"**. The engine launches 12 parallel threads to exchange cookies for 60-day access tokens, generates unique anti-detect hardware profiles, and inserts them into your active pool.

---

## ??? REST API Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| /api/status | GET | Current pool status, active account, process state, and live metrics. |
| /api/accounts | GET | Retrieve accounts partitioned into Tier 1, Tier 2, and Cooldown. |
| /api/switch/<id> | POST | Switch active account, inject tokens, apply fingerprint, and relaunch. |
| /api/rotate | POST | Automatically route to the next best available Tier 1 or Tier 2 account. |
| /api/antidetect/config | GET / POST | Read or update global anti-detect mode (ccount_locked, stealth_randomize). |
| /api/antidetect/account/<id> | GET | Inspect persistent hardware fingerprint for a specific account. |
| /api/antidetect/regenerate/<id>| POST | Regenerate a new persistent fingerprint for an account. |
| /api/cookies/open-folder | POST | Open Windows Explorer to the local Cookies/ folder. |
| /api/cookies/scan-all | POST | Execute 12-thread parallel cookie-to-token ingestion. |
| /api/cursor/settings | GET / POST | Read or update 8-category Cursor IDE settings. |
| /api/unstick-keys | POST | Emergency trigger to release all physical modifier keys. |

---

## ?? Testing & Verification

The test suite contains over **300 automated unit and integration tests** verifying anti-detect spoofing, account switching, window focus safety, modifier key unsticking, and two-tier quota prioritization:

`ash
# Run all tests
pytest tests/ -q

# Run anti-detect tests specifically
pytest tests/test_antidetect_fingerprint.py -v
`

---

## ?? Security & Privacy Notice

- **Zero Remote Telemetry**: CursorRouter runs 100% locally on your machine (127.0.0.1).
- **Encrypted Storage**: Sensitive access tokens in SQLite are encrypted using machine-bound HMAC keys.
- **Git Security**: Private cookies, SQLite databases, master keys, and cache directories are permanently excluded in .gitignore.

---

## ?? License

Distributed under the **MIT License**. See [LICENSE](LICENSE) for details.
