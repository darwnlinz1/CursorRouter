# 🛠️ Utility & Diagnostic Scripts

This directory contains diagnostic utilities, state inspectors, benchmark runners, and automation helpers for CursorRouter.

---

## 📋 Script Catalog

| Script | Purpose |
| :--- | :--- |
| **`check_current_state.py`** | Inspects active account credentials in Cursor's `state.vscdb` and reconciles against `cursor_accounts.db`. |
| **`check_quota_accounts.py`** | Queries and summarizes accounts with elevated quota usage or active cooldown locks. |
| **`scan_all_sand.py`** | Scans all accounts in SQLite and categorizes status by quota and tier. |
| **`send_prompt_to_cursor.py`** | Sends test prompts to Cursor's Composer / Chat window via Win32 automation. |
| **`inspect_composer.py`** | Examines active Composer workspace sessions and JSON metadata. |
| **`inspect_logs.py`** | Tails internal Cursor IDE runtime logs for error patterns and rate limit indicators. |
| **`closed_loop_benchmark.py`** | Measures account rotation latency, proxy throughput, and Connect-RPC response times. |
| **`multi_project_closed_loop_tester.py`** | Multi-project concurrent stress test verifying token injection resilience. |
| **`test_live_auto_fix.py`** | Verifies self-healing recovery triggers under simulated network dropouts. |

---

*Note: All scripts configure `ROOT_DIR` automatically and can be executed directly:*
```bash
python scripts/<script_name>.py
```