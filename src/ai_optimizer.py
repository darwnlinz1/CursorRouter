"""
AI-Driven Optimizations & Heuristics for Cursor Manager
======================================================
Provides:
1. Intelligent Quota Burn Velocity Prediction (EMA smoothed token burn rate,
   Time-To-Exhaustion TTE estimation, and proactive pre-flight handover).
2. Dynamic Rolling-Window Cooldown Reduction (Parses server reset headers,
   5-hour soft fast-request windows, and transient rate limits to avoid rigid 7-day blacklists).
3. Context-Preserving Continuation Synthesizer (Extracts tail anchor AST lines
   and generates structured continuation prompts that prevent code repetition).
"""

import re
import time
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("CursorThings.AIOptimizer")

RESET_REGEX_PATTERNS = [
    re.compile(r"resets?\s+in\s+(?:(?P<days>\d+)\s*d(?:ays?)?\s*)?(?:(?P<hours>\d+)\s*h(?:ours?)?\s*)?(?:(?P<minutes>\d+)\s*m(?:in(?:utes?)?)?\s*)?(?:(?P<seconds>\d+)\s*s(?:ec(?:onds?)?)?)?", re.I),
    re.compile(r"paused\s+until\s+(?P<iso_ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)", re.I),
    re.compile(r"try\s+again\s+in\s+(?P<seconds>\d+)\s*(?:s|sec|seconds)", re.I),
    re.compile(r"retry[-_]after:\s*(?P<seconds>\d+)", re.I)
]


class IntelligentQuotaPredictor:
    """Tracks token burn rates and predicts time-to-exhaustion across rolling windows."""

    def __init__(self, alpha: float = 0.25, critical_tte_seconds: float = 45.0):
        self.alpha = alpha
        self.critical_tte_seconds = critical_tte_seconds
        # account_id -> list of (timestamp, usage_percent, total_spend)
        self.history: Dict[str, List[Tuple[float, float, float]]] = {}
        # account_id -> current smoothed burn velocity (usage_percent per second)
        self.ema_burn_rate: Dict[str, float] = {}

    def record_usage(self, account_id: str, usage_percent: float, total_spend: float = 0.0):
        """Record instantaneous usage snapshot."""
        now = time.time()
        if account_id not in self.history:
            self.history[account_id] = []

        hist = self.history[account_id]
        hist.append((now, float(usage_percent), float(total_spend)))

        # Retain 15-minute sliding window
        cutoff = now - 900
        self.history[account_id] = [pt for pt in hist if pt[0] >= cutoff]

        if len(self.history[account_id]) >= 2:
            prev_t, prev_u, _ = self.history[account_id][-2]
            dt = max(0.1, now - prev_t)
            du = max(0.0, usage_percent - prev_u)
            instant_burn = du / dt

            old_ema = self.ema_burn_rate.get(account_id, instant_burn)
            self.ema_burn_rate[account_id] = (self.alpha * instant_burn) + ((1.0 - self.alpha) * old_ema)

    def predict_exhaustion(self, account_id: str, current_usage: float) -> Dict[str, Any]:
        """Estimate time-to-exhaustion and determine if proactive swap is recommended."""
        burn_rate = self.ema_burn_rate.get(account_id, 0.0)
        remaining_quota = max(0.0, 100.0 - current_usage)

        if burn_rate <= 0.00001:
            tte = 86400.0
        else:
            tte = remaining_quota / burn_rate

        should_proactively_swap = bool((tte < self.critical_tte_seconds) and (current_usage >= 50.0))
        return {
            "account_id": account_id,
            "burn_rate_percent_per_min": round(burn_rate * 60, 3),
            "tte_seconds": round(tte, 1),
            "is_critical": tte < self.critical_tte_seconds,
            "should_proactively_swap": should_proactively_swap,
            "remaining_percent": round(remaining_quota, 2)
        }


class DynamicCooldownManager:
    """
    Computes dynamic cooldown durations instead of static 7-day blacklists.
    Recognizes 5-hour soft limits, 30-minute transient rate limits, and server headers.
    """
    DEFAULT_SHORT_COOLDOWN = 1800       # 30 mins for transient rate limit
    DEFAULT_FAST_WINDOW = 5 * 3600      # 5 hours for fast request quota reset
    MAX_FALLBACK_COOLDOWN = 7 * 86400   # 7 days max fallback

    @classmethod
    def parse_reset_duration(cls, error_text: str, headers: Optional[Dict[str, str]] = None) -> Optional[int]:
        """Parse cooldown duration from headers or error message."""
        if headers:
            for k in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset"):
                val = headers.get(k) or headers.get(k.lower())
                if val and str(val).isdigit():
                    return int(val)

        for pattern in RESET_REGEX_PATTERNS:
            match = pattern.search(error_text or "")
            if match:
                gd = match.groupdict()
                if "seconds" in gd and gd["seconds"]:
                    return int(gd["seconds"])
                if "iso_ts" in gd and gd["iso_ts"]:
                    try:
                        target = datetime.fromisoformat(gd["iso_ts"].replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        delta = (target - now).total_seconds()
                        if delta > 0:
                            return int(delta)
                    except Exception:
                        pass

                total_s = 0
                if gd.get("days"): total_s += int(gd["days"]) * 86400
                if gd.get("hours"): total_s += int(gd["hours"]) * 3600
                if gd.get("minutes"): total_s += int(gd["minutes"]) * 60
                if gd.get("seconds"): total_s += int(gd["seconds"])
                if total_s > 0:
                    return total_s

        lower = (error_text or "").lower()
        if "fast request" in lower or "5 hour" in lower or "5-hour" in lower or "fast usage" in lower:
            return cls.DEFAULT_FAST_WINDOW
        if "too many requests" in lower or "rate limit" in lower or "429" in lower:
            return cls.DEFAULT_SHORT_COOLDOWN
        return None

    @classmethod
    def calculate_cooldown(cls, error_text: str = "", headers: Optional[Dict[str, str]] = None) -> int:
        """Calculate dynamic cooldown seconds with safe 5% jitter buffer."""
        parsed_duration = cls.parse_reset_duration(error_text, headers)
        if parsed_duration:
            return int(parsed_duration * 1.05)
        return cls.MAX_FALLBACK_COOLDOWN


class ContextPreservingContinuationSynthesizer:
    """
    Synthesizes intelligent, context-preserving continuation prompts
    to prevent code repetition or hallucinations upon account rotation.
    """

    @staticmethod
    def synthesize_prompt(
        truncated_response: str = "",
        target_file: Optional[str] = None,
        language: str = "code"
    ) -> str:
        """Generate structured continuation prompt with tail anchor."""
        lines = [l for l in (truncated_response or "").splitlines() if l.strip()]
        if not lines:
            return "Tiếp tục hoàn thiện nhiệm vụ đang dang dở."

        tail_lines = lines[-4:] if len(lines) >= 4 else lines
        tail_snippet = "\n".join(tail_lines)

        file_ctx = f" trong `{target_file}`" if target_file else ""

        continuation_prompt = (
            f"[SYSTEM: Quá trình sinh code trước đó bị ngắt quãng giữa chừng do chuyển đổi tài khoản{file_ctx}.]\n"
            f"Các dòng code đã sinh cuối cùng:\n"
            f"```{language}\n"
            f"{tail_snippet}\n"
            f"```\n\n"
            f"[YÊU CẦU]\n"
            f"Tiếp tục sinh code ngay lập tức tiếp nối đoạn neo trên.\n"
            f"KHÔNG lặp lại các dòng đã sinh ở trên. Tiếp tục trực tiếp phần logic còn lại đến khi hoàn thành."
        )
        return continuation_prompt


# Global singleton instances
_global_predictor: Optional[IntelligentQuotaPredictor] = None

def get_quota_predictor() -> IntelligentQuotaPredictor:
    global _global_predictor
    if _global_predictor is None:
        _global_predictor = IntelligentQuotaPredictor()
    return _global_predictor
