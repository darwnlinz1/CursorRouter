# ?? CursorRouter Automated Test Suite

Comprehensive automated test suite covering unit tests, integration scenarios, Win32 API safety, and end-to-end verification workflows.

---

## ?? Running the Tests

- **Run complete test suite:**
  `ash
  pytest tests/ -q
  `

- **Run specific test suite:**
  `ash
  pytest tests/test_antidetect_fingerprint.py -v
  pytest tests/test_desktop_app_module.py -v
  `

- **Run with failure stop (-x):**
  `ash
  pytest tests/ -x
  `

---

## ?? Test Suite Index

| Suite | Description |
| :--- | :--- |
| **	est_antidetect_fingerprint.py** | Anti-detect profile isolation: deterministic hardware IDs generation, SQLite persistence, and storage.json injection. |
| **	est_desktop_app_module.py** | Standalone native desktop app: PyInstaller flags, Edge WebView2 controller, and modifier unstick cleanup. |
| **	est_standalone_builder.py** | Binary compiler: SHA-256 hash calculation and manifest generation. |
| **	est_cursor_sync_and_switch.py** | Bidirectional synchronization between Cursor IDE and cockpit, plus account switching. |
| **	est_hard_restart_and_chain_continuation.py** | Process termination, lock release, fresh token injection, and safe prompt continuation. |
| **	est_interruption_filter_and_task_continuation.py** | Smart filter discriminating between interrupted tasks (resends prompt) and completed tasks (blocks duplicate prompt). |
| **	est_all_cursor_settings_and_profiles.py** | 8-category Cursor IDE settings taxonomy bi-directional sync and custom configuration profiles. |
| **	est_chat_lock_and_proxy_comprehensive.py** | 4-layer chat lockout detection and local Connect-RPC streaming proxy (:8999). |
| **	est_realtime_and_quota_persistence.py** | Persistent quota engine (guarantees quota is retained across F5 page reloads) and live clock. |
| **	est_ui_and_native_features.py** | Active account first-row sorting, cookie folder auto-creation, and Windows Toast notification escaping. |
| **	est_pkce_auth_and_cookie_parsing.py** | PKCE OAuth 2.0 exchange, Netscape cookie parser, and HTTP error handling. |
| **	est_cursor_storage_manager.py** | JWT decoding, SQLite ItemTable updates in state.vscdb, and remote profile synchronization. |
| **	est_token_pool_and_checksum.py** | Sub-millisecond in-memory token cache, fair round-robin scheduling, and dynamic checksum calculation. |
