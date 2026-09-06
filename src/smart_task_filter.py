"""
Smart Task Completion Filter for Cursor AI Auto-Rotation
=========================================================

Ensures that auto-continue / prompt-resend ONLY fires when a task was actually
interrupted mid-flight by a quota limit or chat lockout.

If a task is completed or ended naturally (e.g. outcome == "success", clean generation,
or no active mid-flight error), prompt-resend is STRICTLY SUPPRESSED.
"""

import os
import re
import glob
import json
import time
import threading
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

from chat_lock_detector import COMPILED_LOCKOUT_REGEX, LOCKOUT_CONNECT_CODES


class TaskStateTracker:
    """Thread-safe singleton tracking runtime task interruption events."""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TaskStateTracker, cls).__new__(cls)
                cls._instance._init_tracker()
            return cls._instance

    def _init_tracker(self):
        self._state_lock = threading.Lock()
        self._last_interruption: Optional[Dict[str, Any]] = None
        self._last_completion: Optional[Dict[str, Any]] = None
        self._handled_request_ids = set()
        self._last_handled_time: float = 0.0

    def mark_interrupted(self, reason: str, request_id: Optional[str] = None, source: str = "proxy"):
        with self._state_lock:
            self._last_interruption = {
                "time": time.time(),
                "reason": str(reason),
                "request_id": request_id,
                "source": source
            }

    def mark_completed(self, request_id: Optional[str] = None, source: str = "normal"):
        with self._state_lock:
            self._last_interruption = None
            self._last_completion = {
                "time": time.time(),
                "request_id": request_id,
                "source": source
            }

    def mark_interruption_consumed(self, request_id: Optional[str] = None, timestamp: Optional[float] = None):
        """Marks an interruption as addressed so it cannot trigger duplicate prompts."""
        with self._state_lock:
            self._last_interruption = None
            now = time.time()
            self._last_handled_time = max(now, timestamp or 0.0)
            if request_id:
                self._handled_request_ids.add(str(request_id))

    def is_interruption_handled(self, request_id: Optional[str] = None, timestamp: Optional[float] = None) -> bool:
        """Checks if a specific interruption (by request_id or timestamp) was already acted upon."""
        with self._state_lock:
            if request_id and str(request_id) in self._handled_request_ids:
                return True
            if timestamp and timestamp <= self._last_handled_time:
                return True
            return False

    def get_tracked_events(self) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        with self._state_lock:
            return (
                dict(self._last_interruption) if self._last_interruption else None,
                dict(self._last_completion) if self._last_completion else None
            )

    def reset(self):
        with self._state_lock:
            self._last_interruption = None
            self._last_completion = None
            self._handled_request_ids.clear()
            self._last_handled_time = 0.0


class SmartTaskCompletionFilter:
    """
    Evaluates multi-source evidence (Cursor Structured Logs, In-Memory Tracker,
    and state.vscdb Composer State) to determine whether a task was interrupted
    mid-flight by quota/lockout, or finished naturally.
    """

    DEFAULT_INTERRUPTION_WINDOW_SECONDS = 180.0  # 3 minutes

    def __init__(self, logs_dir: Optional[str] = None, db_path: Optional[str] = None):
        self.appdata = os.getenv("APPDATA") or ""
        self.logs_dir = logs_dir or os.path.join(self.appdata, "Cursor", "logs")
        self.db_path = db_path or os.path.join(self.appdata, "Cursor", "User", "globalStorage", "state.vscdb")
        self.tracker = TaskStateTracker()

    def scan_recent_cursor_logs(self, max_files: int = 5, max_lines_per_file: int = 500) -> List[Dict[str, Any]]:
        """
        Scans recent Cursor structured log files and extracts agent turn events
        ordered from newest to oldest.
        """
        if not os.path.exists(self.logs_dir):
            return []

        pattern = os.path.join(self.logs_dir, "**", "*Structured*.log")
        log_files = glob.glob(pattern, recursive=True)
        if not log_files:
            return []

        log_files.sort(key=os.path.getmtime, reverse=True)
        events = []

        KEYWORDS = (
            "agent.turn.outcome",
            "Error in AI response",
            "resource_exhausted",
            "Stream error",
            "ConnectError",
            "Stream ended without turnEnded",
            "usage limit",
            "ActionRequiredError",
        )

        for fpath in log_files[:max_files]:
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                    # Process lines in reverse order (newest first)
                    for line in reversed(lines[-max_lines_per_file:]):
                        if any(kw in line for kw in KEYWORDS):
                            entry = self._parse_log_line(line)
                            if entry:
                                events.append(entry)
            except Exception:
                continue

        # Sort all discovered events by timestamp descending
        events.sort(key=lambda x: x.get("timestamp_epoch", 0.0), reverse=True)
        return events

    def _parse_log_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Parses a structured log line into a normalized event dictionary."""
        try:
            # Format typically: YYYY-MM-DD HH:MM:SS.mmm [level] {json}
            parts = line.split(" [", 1)
            if len(parts) < 2:
                return None
            ts_str = parts[0].strip()

            bracket_parts = parts[1].split("] ", 1)
            if len(bracket_parts) < 2:
                return None
            level = bracket_parts[0].strip()
            json_str = bracket_parts[1].strip()

            data = json.loads(json_str)
            meta = data.get("metadata", {})

            # Parse timestamp to epoch
            ts_epoch = 0.0
            try:
                # Handle YYYY-MM-DD HH:MM:SS.mmm
                clean_ts = ts_str.split(".")[0]
                dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
                # Add milliseconds if present
                ms = 0
                if "." in ts_str:
                    ms_part = ts_str.split(".")[1][:3]
                    ms = int(ms_part) if ms_part.isdigit() else 0
                ts_epoch = dt.timestamp() + (ms / 1000.0)
            except Exception:
                ts_epoch = time.time()

            msg = data.get("message", "")
            outcome = meta.get("outcome")
            error_text = (
                meta.get("error_text")
                or meta.get("errorMessage")
                or meta.get("error.message")
                or meta.get("caughtError")
                or meta.get("cause.error.message")
                or ""
            )
            error_code = str(
                meta.get("error_code")
                or meta.get("errorCode")
                or meta.get("error.code")
                or meta.get("causeCode")
                or meta.get("cause.error.code")
                or ""
            )
            error_type = meta.get("error_type") or meta.get("error.kind") or ""
            request_id = meta.get("request_id") or meta.get("requestId")

            # Check if this error signifies a quota exhaustion or lockout
            is_quota_lockout = False
            lockout_reason = None

            if outcome == "error" or "Error" in msg or "error" in level or "warn" in level:
                combined_err = f"{msg} {error_text} {error_code} {error_type}"
                if error_code in ("upgrade", "resource_exhausted", "8", "9", "failed_precondition"):
                    is_quota_lockout = True
                    lockout_reason = f"Cursor log error code: {error_code} ({error_text[:100]})"
                elif COMPILED_LOCKOUT_REGEX.search(combined_err):
                    match = COMPILED_LOCKOUT_REGEX.search(combined_err)
                    is_quota_lockout = True
                    lockout_reason = f"Cursor log matched '{match.group(0)}': {error_text[:100]}"

            return {
                "timestamp_str": ts_str,
                "timestamp_epoch": ts_epoch,
                "message": msg,
                "level": level,
                "outcome": outcome,
                "is_success": (outcome == "success"),
                "is_quota_lockout": is_quota_lockout,
                "lockout_reason": lockout_reason,
                "error_text": error_text,
                "request_id": request_id
            }
        except Exception:
            return None

    def get_latest_task_status(self, max_age_seconds: Optional[float] = None) -> Dict[str, Any]:
        """
        Combines In-Memory Tracking, Structured Logs, and state.vscdb
        to determine current task status.
        """
        max_age = max_age_seconds or self.DEFAULT_INTERRUPTION_WINDOW_SECONDS
        now = time.time()

        status = {
            "is_interrupted": False,
            "is_completed_naturally": False,
            "interruption_reason": None,
            "source": "none",
            "age_seconds": None,
            "latest_event": None
        }

        # 1. Check In-Memory Tracker
        last_int, last_comp = self.tracker.get_tracked_events()
        int_time = last_int.get("time", 0.0) if last_int else 0.0
        comp_time = last_comp.get("time", 0.0) if last_comp else 0.0

        # If in-memory completion occurred after interruption
        if comp_time > int_time:
            status["is_completed_naturally"] = True
            status["source"] = "in_memory_completion"
            status["latest_event"] = last_comp
            return status

        # If in-memory interruption is recent and not yet handled
        if int_time > 0 and (now - int_time) <= max_age:
            req_id = last_int.get("request_id")
            if not self.tracker.is_interruption_handled(request_id=req_id, timestamp=int_time):
                status["is_interrupted"] = True
                status["interruption_reason"] = last_int.get("reason", "In-flight stream interrupted by quota")
                status["source"] = "in_memory_tracker"
                status["age_seconds"] = now - int_time
                status["latest_event"] = last_int
                return status

        # 2. Check Structured Logs
        log_events = self.scan_recent_cursor_logs(max_files=3)
        if log_events:
            latest = log_events[0]
            status["latest_event"] = latest
            evt_age = now - latest["timestamp_epoch"]
            status["age_seconds"] = evt_age

            # If newest turn event was a natural SUCCESS
            if latest.get("is_success"):
                status["is_completed_naturally"] = True
                status["source"] = "cursor_log_success"
                return status

            # If newest turn event was an ERROR with quota/lockout
            if latest.get("is_quota_lockout"):
                req_id = latest.get("request_id")
                evt_ts = latest.get("timestamp_epoch")
                if self.tracker.is_interruption_handled(request_id=req_id, timestamp=evt_ts):
                    status["source"] = "handled_cursor_log_error"
                    return status

                if evt_age <= max_age:
                    status["is_interrupted"] = True
                    status["interruption_reason"] = latest.get("lockout_reason") or latest.get("error_text")
                    status["source"] = "cursor_log_lockout"
                    return status
                else:
                    # Stale error beyond the interruption window
                    status["source"] = "stale_cursor_log_error"
                    return status

        return status

    def should_auto_continue(
        self,
        auto_continue: Optional[bool] = None,
        auto_resend_cfg: str = "auto",
        max_age_seconds: Optional[float] = None,
        consume: bool = False
    ) -> Tuple[bool, str]:
        """
        Primary decision gate for Auto-Continue / Prompt-Resend.

        Returns (should_continue: bool, rationale: str).

        Guarantees:
        - If auto_continue is explicitly False -> NEVER continue.
        - If auto_resend_cfg is 'manual' (and not explicitly overridden) -> NEVER continue.
        - If a task finished naturally with success -> NEVER continue.
        - If a task was ACTUALLY interrupted mid-flight by quota limit or chat lock -> CONTINUE.
        - If idle / no active interruption -> NEVER continue.
        - If consume is True: marks the interruption as handled so it cannot fire again.
        """
        # Rule 1: Explicitly disabled by caller
        if auto_continue is False:
            return False, "Auto-continue explicitly suppressed by caller (auto_continue=False)"

        # Rule 1.5: Config explicitly set to always send continue prompt
        if auto_resend_cfg in ("always", "force"):
            return True, f"Auto-continue always enabled by configuration (auto_resend_action='{auto_resend_cfg}')"

        # Rule 2: User disabled in config
        if auto_resend_cfg != "auto" and auto_continue is not True:
            return False, f"Auto-continue suppressed by configuration (auto_resend_action='{auto_resend_cfg}')"

        # Evaluate live task status
        task_status = self.get_latest_task_status(max_age_seconds=max_age_seconds)

        # Rule 3: Task completed naturally
        if task_status.get("is_completed_naturally"):
            return False, "Task was completed or ended naturally with success; prompt-resend suppressed to avoid repeating tests."

        # Rule 4: Task was genuinely interrupted mid-flight
        if task_status.get("is_interrupted"):
            reason = task_status.get("interruption_reason", "Quota limit or chat lock hit mid-flight")
            if consume:
                latest_evt = task_status.get("latest_event") or {}
                self.tracker.mark_interruption_consumed(
                    request_id=latest_evt.get("request_id"),
                    timestamp=latest_evt.get("timestamp_epoch") or latest_evt.get("time")
                )
            return True, f"Task was interrupted mid-flight by quota limit / chat lock: {reason}"

        # Rule 5: Explicitly forced by caller
        if auto_continue is True:
            return True, "Auto-continue explicitly forced by caller (auto_continue=True)"

        # Rule 6: Default fallback (Idle / no interruption detected)
        return False, "No active mid-flight quota interruption detected; prompt-resend suppressed."


# Global convenience helpers
def mark_task_interrupted(reason: str, request_id: Optional[str] = None, source: str = "proxy"):
    TaskStateTracker().mark_interrupted(reason=reason, request_id=request_id, source=source)


def mark_task_completed(request_id: Optional[str] = None, source: str = "normal"):
    TaskStateTracker().mark_completed(request_id=request_id, source=source)


def consume_task_interruption(request_id: Optional[str] = None, timestamp: Optional[float] = None):
    TaskStateTracker().mark_interruption_consumed(request_id=request_id, timestamp=timestamp)


def should_trigger_auto_continue(auto_continue: Optional[bool] = None, auto_resend_cfg: str = "auto", consume: bool = False) -> Tuple[bool, str]:
    return SmartTaskCompletionFilter().should_auto_continue(auto_continue=auto_continue, auto_resend_cfg=auto_resend_cfg, consume=consume)
