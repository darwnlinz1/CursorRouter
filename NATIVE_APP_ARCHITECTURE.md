# Native Desktop Application Architecture & System Specification
================================================================

This document details the architectural design, IPC contracts, OS integration protocols, and roadmap for CursorRouter's native Windows desktop application container (Microsoft Edge WebView2 / Tauri v2 + Rust + Win32).

---

## 1. System Vision & Core Objectives

- **Ultra-Low Footprint**: Packaged with Microsoft Edge WebView2 / native runtime. Installation footprint `< 15MB`, operational memory `< 40MB` (compared to Electron's 300MB+ overhead).
- **Zero-Friction Startup**: Runs standalone on Windows without requiring manual Python environment setup or terminal windows.
- **Deep Windows OS Integration**:
  - **System Tray Management**: Background daemon with quick action menu (Instant Account Switch, Process Toggle, Modifier Key Release, Cookie Scanning).
  - **Native WinRT Toast Notifications**: Rich alerts for automatic account rotation, quota warnings, and cooldown events.
  - **Global Hotkeys**: System-wide shortcut (`Ctrl + Alt + S`) for immediate account rotation from any focused window.
  - **Windows Auto-Start**: Optional auto-boot with system tray minimization.

---

## 2. End-to-End Operational Lifecycle

```
[User / Startup]
       │
       ▼
[1. IDE Process Detection (Win32 EnumWindows)]
       │
       ├── Cursor running: Capture PID, HWND, and sync auth state from state.vscdb
       └── Cursor stopped: Display notification banner with 1-click startup button
       │
       ▼
[2. Background Pool Sync (ThreadPoolExecutor 10-15 workers)]
       │
       ├── Tier 1: Quota < 50% (Fast request pool)
       ├── Tier 2: Quota 50% - 99% (Slow pool fallback)
       └── Quarantine: Quota >= 100% or rate-locked (7-day automated quarantine)
       │
       ▼
[3. Cookie Ingestion Dropzone (12-Worker PKCE Pipeline)]
       │
       └── Drop Netscape/raw cookies -> Extract & exchange for 60-day access tokens
       │
       ▼
[4. Account Switching & Anti-Detect Hardware Isolation]
       │
       ├── Terminate Cursor.exe to release SQLite file locks
       ├── Inject unique hardware fingerprint into storage.json (machineId, macMachineId, devDeviceId, sqmId)
       ├── Inject auth tokens into globalStorage/state.vscdb
       ├── Relaunch Cursor.exe via WMI/CIM
       └── If Auto-Continue is active: dispatch prompt safely strictly to Cursor HWND
```

---

## 3. Architecture Comparison: CursorRouter vs Alternative Tooling

| Capability | Generic / Extension Tools | CursorRouter Architecture | Architectural Advantage |
| :--- | :--- | :--- | :--- |
| **Authentication Flow** | Browser OAuth / SSO Google redirect | **Cookie-First PKCE Token Exchange** | Bypasses Google CAPTCHA, SMS 2FA, and automated login blocks during unattended night-time rotation. |
| **Quota Pool Management** | Binary (active vs depleted) | **Two-Tier Priority (<50% / 50-99%) + 7-Day Quarantine** | Maximizes fast requests via Tier 1; exhausts 5-hour rolling slow queue via Tier 2; quarantines exhausted accounts to eliminate spamming. |
| **Startup Discovery Speed** | Serial sequential HTTP calls | **12-15 Parallel ThreadPool Workers** | Scans and reconciles 100+ accounts in 3-5 seconds with WAL-backed SQLite transactions. |
| **Host Keyboard Safety** | Fake Alt keydown hacks without release | **Win32 Activation + Guaranteed Modifier Unstick in `finally:`** | Prevents host keyboard freeze; auto-releases Alt (0x12), Ctrl (0x11), Shift (0x10), Win keys. |
| **Prompt Dispatch Isolation**| Blind clipboard paste into active window | **3-Layer Protection: Inactive typing (>2.5s) + HWND check + Clipboard restore** | Never pastes prompts into browser or external apps; restores user's original clipboard in 50ms. |
| **Cursor Configuration** | Minimal or absent | **Full 8-Category Cursor Settings Taxonomy** | Comprehensive 2-way sync with `%APPDATA%\Cursor\User\settings.json` across all native categories. |
| **Hardware Privacy** | Shared machine identity | **Persistent Anti-Detect Fingerprint Isolation** | Blocks device-level multi-account cross-linking by isolating unique hardware IDs per account. |

---

## 4. Native IPC Interface Specification

For packaging with Tauri v2 / PyWebView:

```rust
// Core Window & Process Commands
fn cmd_is_cursor_running() -> Result<bool, String>;
fn cmd_launch_cursor() -> Result<bool, String>;
fn cmd_terminate_cursor() -> Result<bool, String>;

// Account Pool & Anti-Detect Commands
fn cmd_get_pool_status() -> Result<PoolStatus, String>;
fn cmd_switch_account(account_id: String) -> Result<SwitchResult, String>;
fn cmd_rotate_best() -> Result<SwitchResult, String>;
fn cmd_get_antidetect_profile(account_id: String) -> Result<HardwareFingerprint, String>;
fn cmd_apply_antidetect_fingerprint(account_id: String) -> Result<bool, String>;

// File System & Ingestion
fn cmd_open_cookies_folder() -> Result<bool, String>;
fn cmd_scan_cookies_directory() -> Result<IngestionSummary, String>;

// Win32 Safety
fn cmd_unstick_modifier_keys() -> Result<bool, String>;
```

---

## 5. Security & Cryptographic Invariants

1. **HMAC Token Protection**: All access tokens stored in `cursor_accounts.db` are encrypted at rest using machine-bound PBKDF2/HMAC keys (`.cursor_master.key`).
2. **Strict File Locks**: Access to `state.vscdb` is only performed when `Cursor.exe` processes are halted or via non-locking SQLite query configurations.
3. **Repository Cleanliness**: No private cookies, test keys, databases, or binaries are tracked by version control.