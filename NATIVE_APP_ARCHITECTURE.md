# Native Desktop Application Architecture & System Specification (v3.1)
=======================================================================

This document details the architectural design, IPC contracts, OS integration protocols, and security invariants for CursorRouter's native Windows desktop application container (Microsoft Edge WebView2 via `pywebview` with automatic Edge/Chrome `--app` standalone fallback).

---

## 1. System Vision & Core Objectives

- **Ultra-Low Footprint**: Packaged with Microsoft Edge WebView2 (`pywebview`) and 100% local offline static assets (zero external CDNs).
- **Zero-Friction Startup**: Runs standalone on Windows via `python main.py --native` or a compiled PyInstaller executable, with automatic Edge/Chrome `--app` fallback if `pywebview` is absent.
- **Deep Windows OS Integration**:
  - **DPAPI `cursor-safe-storage:v1:` Codec**: Native bidirectional encryption/decryption using `%APPDATA%\Cursor\Local State` (`os_crypt.encrypted_key`) + Win32 `CryptUnprotectData` + AES-256-GCM.
  - **Native WinRT Toast Notifications**: Alerts for automatic account rotation, quota warnings, and cooldown events.
  - **Global Hotkeys & Keyboard Safety**: Win32 modifier key watchdog (`release_all_modifier_keys` in `finally:`) preventing stuck Alt/Ctrl/Shift/Win keys.

---

## 2. End-to-End Operational Lifecycle (Pure OAuth 2.0 PKCE)

```text
[User / Startup]
       │
       ▼
[1. Official OAuth 2.0 PKCE Onboarding]
       │
       ├── Interactive PKCE Flow: cursor.com/loginDeepControl -> api2.cursor.sh/auth/poll (60-day IDE Session)
       └── 1-Click Capture Active Session: DPAPI decrypts state.vscdb (cursor-safe-storage:v1:)
       │
       ▼
[2. Persistent Async HTTP/2 Sync Engine (~372ms Warm)]
       │
       ├── Tier 1: Quota < 50% (Priority Fast Pool)
       ├── Tier 2: Quota 50% - 99% (Fallback Pool)
       └── Cooldown Quarantine: Quota >= 100% or rate-locked
       │
       ▼
[3. Encrypted SQLite WAL + Sub-Millisecond RAM Token Pool]
       │
       ├── PBKDF2-HMAC encryption at rest + Bounded LRU Cache (0.011ms)
       └── Rotating Connect-RPC Proxy (:8999) swaps tokens in 7.2µs at HTTP protocol layer (zero GUI paste)
       │
       ▼
[4. Account Switching & Anti-Detect Hardware Isolation]
       │
       ├── Terminate Cursor.exe cleanly to release SQLite file locks
       ├── Apply per-account hardware fingerprint to storage.json (account_locked / always_random / native_standard)
       ├── Encrypt OAuth tokens into cursor-safe-storage:v1: and write state.vscdb + glass.lastSignedInAuthId
       └── Relaunch Cursor.exe cleanly via WMI/CIM
```

---

## 3. Architecture Comparison: CursorRouter v3.1 vs Legacy Tooling

| Capability | Legacy Cookie Tools (v2.x) | CursorRouter v3.1 (Pure OAuth PKCE) | Architectural Advantage |
| :--- | :--- | :--- | :--- |
| **Authentication Flow** | Exported web cookies (blocked by `FRESH_SIGN_IN_REQUIRED` & `401` on IDE AI) | **Official OAuth 2.0 PKCE + DPAPI IDE Session Capture** | Issues genuine 60-day `type: "session"` desktop tokens with zero `Authentication error` sign-outs. |
| **Token Storage Compatibility** | Plaintext writes breaking modern `authTokenEncryptionAtRest` | **Native `cursor-safe-storage:v1:` AES-256-GCM + DPAPI Codec** | Seamlessly interoperates with Cursor v0.48+/v2.x+ encrypted `state.vscdb`. |
| **Quota Sync Speed** | Unpooled `requests` (5w, 3 RPCs: ~6,938 ms) | **Persistent `httpx` HTTP/2 Multiplexing (~372 ms warm)** | ~18.6× faster warm quota synchronization over coalesced TLS streams. |
| **Retry Mechanism** | Intrusive Win32 clipboard paste (`"Tiếp tục"`) | **Protocol-Level Connect-RPC Rotating Proxy (`:8999`)** | Transparent HTTP-layer retry on `429` with zero focus stealing or clipboard pollution. |
| **Cursor Configuration** | Raw `json.loads()` wiping comments | **String-Aware JSONC Sync (`src/jsonc_utils.py`)** | Preserves comments & URLs across all 8 Cursor settings categories. |
| **Hardware Privacy** | Shared machine identity | **Persistent Anti-Detect Fingerprint Vault** | Isolates `machineId`, `macMachineId`, `devDeviceId`, `sqmId` per account. |

---

## 4. Native IPC Interface Specification

```rust
// Core Window & Process Commands
fn cmd_is_cursor_running() -> Result<bool, String>;
fn cmd_launch_cursor() -> Result<bool, String>;
fn cmd_terminate_cursor() -> Result<bool, String>;

// OAuth PKCE & Session Pool Commands
fn cmd_start_oauth_pkce_login() -> Result<OAuthLoginSession, String>;
fn cmd_poll_oauth_pkce_status(session_id: String) -> Result<OAuthSessionStatus, String>;
fn cmd_capture_active_cursor_session() -> Result<AccountSummary, String>;
fn cmd_sync_oauth_pool_h2() -> Result<SyncSummary, String>;

// Routing & Anti-Detect Commands
fn cmd_get_pool_status() -> Result<PoolStatus, String>;
fn cmd_switch_account(account_id: String) -> Result<SwitchResult, String>;
fn cmd_rotate_best() -> Result<SwitchResult, String>;
fn cmd_get_antidetect_profile(account_id: String) -> Result<HardwareFingerprint, String>;
fn cmd_apply_antidetect_fingerprint(account_id: String) -> Result<bool, String>;

// Win32 Safety
fn cmd_unstick_modifier_keys() -> Result<bool, String>;
```

---

## 5. Security & Cryptographic Invariants

1. **PBKDF2-HMAC Token Protection**: All OAuth access/refresh tokens stored in `cursor_accounts.db` are encrypted at rest (`enc:v1:`) using machine-bound keys (`.cursor_master.key`) with a bounded LRU decrypt cache.
2. **Native Electron SafeStorage Interop**: Reads and writes `cursor-safe-storage:v1:` tokens strictly via Windows DPAPI for the current OS user account.
3. **Zero Secret Leaks in API & Git**: List/status endpoints redact raw JWTs; `.gitignore` permanently excludes `*.db`, `*.key`, and `.env` files.
