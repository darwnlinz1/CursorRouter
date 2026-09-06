"""
Unified Multi-Layer Chat Lockout (Khóa Chat) Detector for Cursor AI
===================================================================

This module provides exhaustive, multi-layer detection of account lockout / quota
exhaustion across all communication channels in Cursor:

Layer 1 (API Quota Metrics):
    - autoPercentUsed >= 100.0% (Cursor Auto-mode quota exhausted)
    - totalSpend >= 100.0 (Which equals 50.0% of the 200 displayThreshold)
    - usage_percent >= QUOTA_EXHAUSTION_THRESHOLD (50.0%)
    - Account status == 'EXHAUSTED' / 'RATE_LIMITED'

Layer 2 (HTTP Status Codes & Protocol Headers):
    - HTTP 429 Too Many Requests
    - HTTP 402 Payment Required
    - HTTP 403 Forbidden
    - HTTP 401 Unauthorized (Expired / revoked session)
    - Headers: grpc-status: 8 (RESOURCE_EXHAUSTED), grpc-status: 9 (FAILED_PRECONDITION)

Layer 3 (Connect-RPC Protocol Envelope & Trailers):
    - Connect-RPC binary frames: [1-byte flag][4-byte big-endian len][payload]
    - Trailer frames with flag & 0x02 containing:
      {"error": {"code": "resource_exhausted", ...}} or
      {"code": "failed_precondition", ...}

Layer 4 (JSON Body & Keyword Heuristics):
    - JSON bodies containing explicit error messages like "resource_exhausted",
      "exceeded your current quota", "monthly request limit", "rate limit",
      "free usage limit", "hardLimit", etc.
"""

import os
import re
import json
import struct
from typing import Optional, Dict, Any, List, Tuple, Union

# Configurable threshold: Cursor Free accounts can be used up to 100%. The 50% threshold is only the 5-hour soft fast-request window.
QUOTA_EXHAUSTION_THRESHOLD = float(os.getenv("CURSOR_QUOTA_THRESHOLD", "100.0"))
QUOTA_WARNING_THRESHOLD = float(os.getenv("CURSOR_QUOTA_WARNING_THRESHOLD", "50.0"))

# Connect-RPC Frame Flags
FRAME_FLAG_DATA = 0x00
FRAME_FLAG_COMPRESSED = 0x01
FRAME_FLAG_TRAILER = 0x02

# Lockout keyword regex patterns (case-insensitive)
LOCKOUT_KEYWORD_PATTERNS = [
    r"resource_exhausted",
    r"exceeded\s+your\s+current\s+quota",
    r"rate\s*limit",
    r"monthly\s*(?:request|usage)\s*limit",
    r"free\s*usage\s*limit",
    r"usage\s*limit\s*reached",
    r"quota\s*exceeded",
    r"insufficient_quota",
    r"hardlimit",
    r"hasreachedlimit",
    r"isquotaexceeded",
    r"you(?:'ve|\s+have)\s+reached\s+your\s+free\s+plan\s+limit",
    r"you(?:'ve|\s+have)\s+reached\s+your\s+limit",
    r"you(?:'ve|\s+have)\s+reached\s+your",
    r"chat\s+is\s+locked",
    r"account\s+(?:is\s+)?locked",
    r"out\s+of\s+usage",
    r"you\s+are\s+out\s+of\s+usage",
    r"paused\s+until\s+your\s+usage\s+resets",
    r"switched\s+to\s+cursor\s+models",
    r"usage_limit",
    r"plan_limit",
]

COMPILED_LOCKOUT_REGEX = re.compile(
    "|".join(f"(?:{p})" for p in LOCKOUT_KEYWORD_PATTERNS),
    re.IGNORECASE
)

# Connect-RPC error codes that definitively signify account / quota lockouts
LOCKOUT_CONNECT_CODES = {
    "resource_exhausted",
    "failed_precondition",
    "quota_exceeded",
}

# Connect-RPC error codes that definitively DO NOT signify quota lockouts
NON_LOCKOUT_CONNECT_CODES = {
    "canceled",
    "invalid_argument",
    "deadline_exceeded",
    "not_found",
    "already_exists",
    "unimplemented",
    "internal",
    "data_loss",
    "out_of_range",
}


def is_connect_error_lockout(err: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Determines if an extracted Connect-RPC error represents a quota exhaustion
    or chat lockout, rather than a non-lockout client/server error (e.g. canceled, invalid_argument).
    """
    if not isinstance(err, dict):
        return False, None

    code = str(err.get("code", "")).strip().lower()
    msg = str(err.get("message", "")).strip()

    # 1. Definite lockout codes
    if code in LOCKOUT_CONNECT_CODES:
        return True, f"Connect-RPC error code '{code}' (message: '{msg}')"

    # 2. Token revoked / unauthenticated
    if code in ("unauthenticated",):
        return True, f"Connect-RPC error code '{code}' (session invalid or revoked)"

    # 3. If code is in known non-lockout codes and message has no lockout keywords, it's NOT locked
    if code in NON_LOCKOUT_CONNECT_CODES:
        return False, None

    # 4. Check error message against lockout regex patterns
    if msg:
        match = COMPILED_LOCKOUT_REGEX.search(msg)
        if match:
            return True, f"Connect-RPC error message indicates lockout ('{match.group(0)}')"

    # 5. Fallback for custom or permission_denied codes
    if code == "permission_denied" and msg:
        match = COMPILED_LOCKOUT_REGEX.search(msg)
        if match:
            return True, f"Connect-RPC permission_denied indicates lockout ('{match.group(0)}')"

    return False, None


def make_connect_frame(payload: Union[Dict[str, Any], str, bytes], flag: int = FRAME_FLAG_DATA) -> bytes:
    """Encodes a payload into a standard 5-byte Connect-RPC binary frame."""
    if isinstance(payload, dict):
        payload_bytes = json.dumps(payload).encode("utf-8")
    elif isinstance(payload, str):
        payload_bytes = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        payload_bytes = bytes(payload)
    else:
        payload_bytes = str(payload).encode("utf-8")

    flen = len(payload_bytes)
    return bytes([flag]) + struct.pack(">I", flen) + payload_bytes


def parse_connect_frames(data: Union[bytes, bytearray, memoryview]) -> List[Tuple[int, bytes, int]]:
    """
    Parses all complete Connect-RPC frames from a binary buffer.
    Returns a list of tuples: (flag, payload_bytes, total_frame_length).
    Gracefully stops when an incomplete frame is encountered.
    """
    frames = []
    idx = 0
    buffer_len = len(data)

    while idx + 5 <= buffer_len:
        flag = data[idx]
        flen = struct.unpack(">I", data[idx + 1: idx + 5])[0]
        total_frame_len = 5 + flen

        if idx + total_frame_len > buffer_len:
            # Incomplete frame, wait for additional network bytes
            break

        payload = bytes(data[idx + 5: idx + total_frame_len])
        frames.append((flag, payload, total_frame_len))
        idx += total_frame_len

    return frames


def extract_trailer_error(payload: Union[bytes, str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Extracts structured error info from a Connect-RPC trailer payload if present.
    Connect error frames typically format as:
      {"error": {"code": "resource_exhausted", "message": "..."}} or
      {"code": "failed_precondition", "message": "..."}
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = json.loads(payload.decode("utf-8", errors="ignore"))
        except Exception:
            return None

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None

    if not isinstance(payload, dict):
        return None

    if "error" in payload and isinstance(payload["error"], dict):
        return payload["error"]

    if "code" in payload:
        return payload

    return None


class ConnectFrameStreamAccumulator:
    """
    Streaming Connect-RPC frame parser.
    Maintains an internal buffer across arbitrary TCP chunks, resolving
    packet fragmentation, frame coalescing, and split-frame boundaries.
    """

    def __init__(self):
        self._buffer = bytearray()
        self._detected_trailer_error: Optional[Dict[str, Any]] = None
        self._detected_lockout_reason: Optional[str] = None

    def feed(self, chunk: bytes) -> List[Tuple[int, bytes]]:
        """
        Feeds incoming TCP bytes into accumulator and yields all newly completed frames.
        """
        self._buffer.extend(chunk)
        completed = []

        while len(self._buffer) >= 5:
            flag = self._buffer[0]
            flen = struct.unpack(">I", self._buffer[1:5])[0]
            total_len = 5 + flen

            if len(self._buffer) < total_len:
                break

            payload = bytes(self._buffer[5:total_len])
            del self._buffer[:total_len]

            completed.append((flag, payload))

            # Inspect trailer frame immediately
            if (flag & FRAME_FLAG_TRAILER) != 0:
                err = extract_trailer_error(payload)
                if err:
                    self._detected_trailer_error = err
                    is_lock, lock_reason = is_connect_error_lockout(err)
                    if is_lock:
                        self._detected_lockout_reason = lock_reason
                else:
                    # If payload is valid JSON without error/code, it is a clean trailer (e.g. telemetry headers)
                    is_valid_json = False
                    try:
                        parsed = json.loads(payload.decode("utf-8", errors="ignore"))
                        is_valid_json = isinstance(parsed, dict)
                    except Exception:
                        pass

                    if not is_valid_json:
                        # Raw non-JSON text in trailer: inspect for explicit error keywords
                        try:
                            text = payload.decode("utf-8", errors="ignore")
                            match = COMPILED_LOCKOUT_REGEX.search(text)
                            if match:
                                self._detected_lockout_reason = f"Trailer payload matched '{match.group(0)}'"
                        except Exception:
                            pass

        return completed

    @property
    def trailer_error(self) -> Optional[Dict[str, Any]]:
        return self._detected_trailer_error

    @property
    def is_lockout(self) -> bool:
        return self._detected_lockout_reason is not None

    @property
    def lockout_reason(self) -> Optional[str]:
        return self._detected_lockout_reason

    def clear(self):
        self._buffer.clear()
        self._detected_trailer_error = None
        self._detected_lockout_reason = None


def _extract_metric(data: dict, nested: dict, *keys) -> Optional[Any]:
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
        if nested and k in nested and nested[k] is not None:
            return nested[k]
    return None


def is_chat_locked(
    status_code: Optional[int] = None,
    headers: Optional[Dict[str, str]] = None,
    body_or_chunks: Optional[Union[bytes, str, List[bytes], bytearray]] = None,
    usage_data: Optional[Dict[str, Any]] = None,
    threshold: float = QUOTA_EXHAUSTION_THRESHOLD
) -> Tuple[bool, Optional[str]]:
    """
    Unified multi-layer detector for Cursor Chat Lockout.

    Parameters:
        status_code: HTTP response status code (e.g. 200, 429, 402, 403, 401).
        headers: HTTP response headers dict.
        body_or_chunks: Response body (bytes, str, or list of streamed chunks).
        usage_data: Dict containing account usage metrics (from GetCurrentPeriodUsage, GetMe, or accounts DB).
        threshold: Quota exhaustion threshold in percent (default 50.0%).

    Returns:
        (is_locked: bool, reason: Optional[str])
    """

    # =========================================================================
    # LAYER 1: API Quota Check (Usage Metrics & Cursor Policy)
    # =========================================================================
    if usage_data and isinstance(usage_data, dict):
        plan_usage = usage_data.get("planUsage") if isinstance(usage_data.get("planUsage"), dict) else {}

        # 1.1 Cursor Usage Limit Policy Status (workbench.desktop.main.js usageLimitPolicyStatusService)
        policy = usage_data.get("usageLimitPolicyStatus") or usage_data.get("limitPolicyStatus") or usage_data.get("policyStatus")
        if isinstance(policy, dict):
            stage = str(policy.get("stage", "")).upper()
            if stage == "HARD_BLOCK":
                return True, f"Layer 1 (API Quota): usageLimitPolicyStatus stage is '{stage}'"
            # Note: SLOW_POOL is the 5-hour soft fast-request window. Chat is NOT blocked; requests continue in slow queue.
            tray_label = str(policy.get("trayLabel", ""))
            if tray_label and COMPILED_LOCKOUT_REGEX.search(tray_label):
                return True, f"Layer 1 (API Quota): usageLimitPolicyStatus trayLabel indicates lockout ('{tray_label}')"
            for text_field in (policy.get("errorTitle"), policy.get("errorDetail")):
                if text_field and isinstance(text_field, str) and COMPILED_LOCKOUT_REGEX.search(text_field):
                    return True, f"Layer 1 (API Quota): usageLimitPolicyStatus indicates lockout ('{text_field}')"

        stage_top = str(usage_data.get("stage", "")).upper()
        if stage_top == "HARD_BLOCK":
            return True, f"Layer 1 (API Quota): stage is '{stage_top}'"

        # 1.2 Boolean limit flags (hasReachedLimit, isQuotaExceeded, limitReached, etc.)
        for flag_name in ("hasReachedLimit", "isQuotaExceeded", "has_reached_limit", "is_quota_exceeded", "limitReached"):
            if usage_data.get(flag_name) is True or plan_usage.get(flag_name) is True:
                return True, f"Layer 1 (API Quota): Limit flag '{flag_name}' is True"

        # 1.3 autoPercentUsed: In Cursor Free tier, autoPercentUsed reaches 100% at the 5-hour soft limit (50% of total limit),
        # but chat is NOT blocked (requests continue up to 100% totalPercentUsed / 200 total spend).
        # Only treat as lockout if threshold is configured strictly < 100.
        if threshold < 100.0:
            auto_percent = _extract_metric(usage_data, plan_usage, "autoPercentUsed", "auto_percent_used")
            if auto_percent is not None:
                try:
                    auto_val = float(auto_percent)
                    if auto_val >= 100.0:
                        return True, f"Layer 1 (API Quota): autoPercentUsed ({auto_val:.1f}%) >= 100.0% (auto quota exhausted)"
                except (ValueError, TypeError):
                    pass

        # 1.4 totalSpend >= displayThreshold (100% quota exhausted)
        total_spend = _extract_metric(usage_data, plan_usage, "totalSpend", "total_spend")
        display_threshold = _extract_metric(usage_data, plan_usage, "displayThreshold", "display_threshold") or 200.0
        if total_spend is not None:
            try:
                spend_val = float(total_spend)
                thresh_val = float(display_threshold) if display_threshold else 200.0
                if thresh_val > 0:
                    pct = (spend_val / thresh_val) * 100.0
                    if pct >= threshold:
                        return True, f"Layer 1 (API Quota): totalSpend ({spend_val:.1f} / {thresh_val:.0f} = {pct:.1f}%) >= {threshold:.1f}% exhaustion threshold"
                elif spend_val >= 200.0 and threshold >= 100.0:
                    return True, f"Layer 1 (API Quota): totalSpend ({spend_val:.1f}) >= 200.0"
            except (ValueError, TypeError):
                pass

        # 1.5 usage_percent / usagePercent / totalPercentUsed >= threshold (50.0%)
        usage_percent = _extract_metric(usage_data, plan_usage, "usage_percent", "usagePercent", "totalPercentUsed", "total_percent_used")
        if usage_percent is not None:
            try:
                usage_val = float(usage_percent)
                if usage_val >= threshold:
                    return True, f"Layer 1 (API Quota): usage_percent ({usage_val:.1f}%) >= {threshold:.1f}% exhaustion threshold"
            except (ValueError, TypeError):
                pass

        # 1.6 Status already marked EXHAUSTED or RATE_LIMITED
        status_val = usage_data.get("status")
        if status_val in ("EXHAUSTED", "RATE_LIMITED"):
            return True, f"Layer 1 (API Quota): Account status is explicitly '{status_val}'"

        # 1.7 displayMessage containing explicit lock phrase
        display_msg = _extract_metric(usage_data, plan_usage, "displayMessage", "display_message")
        if display_msg and isinstance(display_msg, str):
            match = COMPILED_LOCKOUT_REGEX.search(display_msg)
            if match:
                return True, f"Layer 1 (API Quota): displayMessage indicates lockout ('{match.group(0)}')"

    # =========================================================================
    # LAYER 2: HTTP Status Codes & Upstream Headers
    # =========================================================================
    if status_code is not None:
        if status_code == 429:
            return True, "Layer 2 (HTTP Status): 429 Too Many Requests (Rate limit / Quota exhausted)"
        if status_code == 402:
            return True, "Layer 2 (HTTP Status): 402 Payment Required (Account usage limit reached)"
        if status_code == 403:
            return True, "Layer 2 (HTTP Status): 403 Forbidden (Chat access restricted / Quota denied)"
        if status_code == 401:
            return True, "Layer 2 (HTTP Status): 401 Unauthorized (Token expired or revoked)"

    if headers and isinstance(headers, dict):
        # Case-insensitive header check
        headers_lower = {k.lower(): str(v).lower() for k, v in headers.items()}

        # gRPC / Connect-RPC status headers
        grpc_status = headers_lower.get("grpc-status")
        if grpc_status in ("8", "resource_exhausted", "9", "failed_precondition"):
            grpc_msg = headers_lower.get("grpc-message", "gRPC status indicates exhaustion")
            return True, f"Layer 2 (Headers): grpc-status={grpc_status} ({grpc_msg})"

        # x-cursor-error or connect-error headers
        for hkey in ("x-cursor-error", "connect-error", "x-error-code"):
            if hkey in headers_lower:
                val = headers_lower[hkey]
                if COMPILED_LOCKOUT_REGEX.search(val):
                    return True, f"Layer 2 (Headers): {hkey} indicates lockout ('{val}')"

    # =========================================================================
    # LAYER 3: Connect-RPC Protocol Envelope & Trailer Frame
    # =========================================================================
    raw_bytes: Optional[bytes] = None
    if body_or_chunks is not None:
        if isinstance(body_or_chunks, (bytes, bytearray)):
            raw_bytes = bytes(body_or_chunks)
        elif isinstance(body_or_chunks, str):
            raw_bytes = body_or_chunks.encode("utf-8")
        elif isinstance(body_or_chunks, list):
            raw_bytes = b"".join(
                c if isinstance(c, (bytes, bytearray)) else (c.encode("utf-8") if isinstance(c, str) else b"")
                for c in body_or_chunks
            )

    has_connect_frames = False
    if raw_bytes and len(raw_bytes) >= 5:
        frames = parse_connect_frames(raw_bytes)
        if frames:
            has_connect_frames = True
            for flag, payload, _ in frames:
                if (flag & FRAME_FLAG_TRAILER) != 0:
                    err = extract_trailer_error(payload)
                    if err:
                        is_lock, lock_reason = is_connect_error_lockout(err)
                        if is_lock:
                            return True, f"Layer 3 (Connect-RPC Trailer): {lock_reason}"
                    else:
                        # Non-error trailer: only inspect if payload is NOT valid JSON
                        is_json = False
                        try:
                            json.loads(payload.decode("utf-8", errors="ignore"))
                            is_json = True
                        except Exception:
                            pass
                        if not is_json:
                            try:
                                payload_text = payload.decode("utf-8", errors="ignore")
                                match = COMPILED_LOCKOUT_REGEX.search(payload_text)
                                if match:
                                    return True, f"Layer 3 (Connect-RPC Trailer): Trailer payload matches '{match.group(0)}'"
                            except Exception:
                                pass

    # =========================================================================
    # LAYER 4: JSON Error Body & Heuristic Keyword Matching
    # =========================================================================
    # If the response contains valid Connect-RPC binary frames, the data frames
    # carry LLM output / text generation and must NOT be scanned for keywords.
    if has_connect_frames:
        return False, None

    if raw_bytes:
        # Try JSON parsing first
        try:
            body_json = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
            if isinstance(body_json, dict):
                code = str(body_json.get("code", "")).lower()
                msg = str(body_json.get("message", ""))
                error_obj = body_json.get("error")

                # Definite lockout error codes
                if code in LOCKOUT_CONNECT_CODES:
                    return True, f"Layer 4 (JSON Body): Error code '{code}' - {msg}"
                if code in ("unauthenticated",):
                    return True, f"Layer 4 (JSON Body): Error code '{code}' (session invalid or revoked)"

                if isinstance(error_obj, dict):
                    err_code = str(error_obj.get("code", "")).lower()
                    err_msg = str(error_obj.get("message", ""))
                    if err_code in LOCKOUT_CONNECT_CODES:
                        return True, f"Layer 4 (JSON Body): Error code '{err_code}' - {err_msg}"
                    if err_code in ("unauthenticated",):
                        return True, f"Layer 4 (JSON Body): Error code '{err_code}' (session invalid or revoked)"
                    if err_msg:
                        match = COMPILED_LOCKOUT_REGEX.search(err_msg)
                        if match:
                            return True, f"Layer 4 (JSON Body): Error message matches '{match.group(0)}'"
                elif isinstance(error_obj, str):
                    match = COMPILED_LOCKOUT_REGEX.search(error_obj)
                    if match:
                        return True, f"Layer 4 (JSON Body): Error matches '{match.group(0)}'"

                # Known non-lockout codes (e.g. canceled, invalid_argument)
                if code in NON_LOCKOUT_CONNECT_CODES:
                    return False, None

                if msg and (status_code != 200 or "error" in body_json or code):
                    match = COMPILED_LOCKOUT_REGEX.search(msg)
                    if match:
                        return True, f"Layer 4 (JSON Body): Message matches '{match.group(0)}'"

                # Check usageLimitPolicyStatus in JSON body
                policy = body_json.get("usageLimitPolicyStatus")
                if isinstance(policy, dict):
                    stage = str(policy.get("stage", "")).upper()
                    if stage == "HARD_BLOCK":
                        return True, f"Layer 4 (JSON Body): usageLimitPolicyStatus stage is '{stage}'"
                    if threshold < 100.0:
                        if stage == "SLOW_POOL":
                            return True, f"Layer 4 (JSON Body): usageLimitPolicyStatus stage is '{stage}'"
                        if policy.get("isInSlowPool") is True:
                            return True, "Layer 4 (JSON Body): usageLimitPolicyStatus isInSlowPool is True"

                # Check boolean limit flags
                for flag_name in ("hasReachedLimit", "isQuotaExceeded", "has_reached_limit", "is_quota_exceeded", "limitReached"):
                    if body_json.get(flag_name) is True:
                        return True, f"Layer 4 (JSON Body): Limit flag '{flag_name}' is True"

                # If status is 200 and it's valid non-error JSON, it's not locked
                if status_code == 200:
                    return False, None
        except Exception:
            pass

        # Raw regex scan across text: ONLY when status_code is non-200 or None
        if status_code != 200:
            try:
                body_str = raw_bytes.decode("utf-8", errors="ignore")
                match = COMPILED_LOCKOUT_REGEX.search(body_str)
                if match:
                    return True, f"Layer 4 (JSON Body): Raw body matches '{match.group(0)}'"
            except Exception:
                pass

    return False, None
