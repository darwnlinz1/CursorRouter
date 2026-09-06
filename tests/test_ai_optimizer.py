"""
Comprehensive Test Suite for AI Optimizer & Dynamic Cooldown Engine
===================================================================
Tests:
1. Intelligent Quota Burn Velocity & Time-to-Exhaustion (TTE).
2. Proactive Pre-Flight Switch Triggering.
3. Dynamic Cooldown Duration Parsing (Headers & Error Strings).
4. Heuristic Fallbacks (5-hour fast-request, 30-min rate limit, 7-day max).
5. Context-Preserving Continuation Synthesizer (Tail Anchor Extraction).
"""

import os
import sys
import time
import unittest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_optimizer import (
    IntelligentQuotaPredictor,
    DynamicCooldownManager,
    ContextPreservingContinuationSynthesizer,
    get_quota_predictor
)


class TestAIOptimizer(unittest.TestCase):
    def setUp(self):
        self.predictor = IntelligentQuotaPredictor(alpha=0.5, critical_tte_seconds=60.0)

    def test_01_quota_burn_rate_and_tte_prediction(self):
        acc = "acc_burn_test@example.com"
        # Simulate initial state: 40% usage
        self.predictor.record_usage(acc, 40.0)
        time.sleep(0.15)
        # Simulate burst: 70% usage after 0.15s (very high burn)
        self.predictor.record_usage(acc, 70.0)

        pred = self.predictor.predict_exhaustion(acc, current_usage=70.0)
        self.assertEqual(pred["account_id"], acc)
        self.assertGreater(pred["burn_rate_percent_per_min"], 0.0)
        self.assertLess(pred["tte_seconds"], 60.0)
        self.assertTrue(pred["is_critical"])
        self.assertTrue(pred["should_proactively_swap"])

    def test_02_dynamic_cooldown_parsing_compound_strings(self):
        # Test "resets in 2h 30m"
        dur1 = DynamicCooldownManager.parse_reset_duration("Rate limit exceeded, quota resets in 2h 30m")
        self.assertEqual(dur1, 2 * 3600 + 30 * 60)

        # Test "try again in 45s"
        dur2 = DynamicCooldownManager.parse_reset_duration("Too many requests, try again in 45s")
        self.assertEqual(dur2, 45)

        # Test retry-after header
        dur3 = DynamicCooldownManager.parse_reset_duration("", headers={"retry-after": "180"})
        self.assertEqual(dur3, 180)

    def test_03_dynamic_cooldown_heuristics(self):
        # 5-hour soft fast-request window
        cd_fast = DynamicCooldownManager.calculate_cooldown("You have reached your 5-hour fast request allowance")
        self.assertAlmostEqual(cd_fast, int(5 * 3600 * 1.05), delta=10)

        # Transient 429 rate limit
        cd_rate = DynamicCooldownManager.calculate_cooldown("429 Too Many Requests: Please slow down")
        self.assertAlmostEqual(cd_rate, int(1800 * 1.05), delta=10)

        # Unknown / standard fallback (7 days)
        cd_fallback = DynamicCooldownManager.calculate_cooldown("Generic unknown account failure")
        self.assertEqual(cd_fallback, 7 * 86400)

    def test_04_context_preserving_continuation_synthesizer(self):
        truncated_code = """def process_items(items):
    results = []
    for item in items:
        if not item.is_valid():
            continue
        cleaned = item.clean()
        results.append(cleaned)"""

        prompt = ContextPreservingContinuationSynthesizer.synthesize_prompt(
            truncated_response=truncated_code,
            target_file="src/processor.py",
            language="python"
        )

        self.assertIn("src/processor.py", prompt)
        self.assertIn("results.append(cleaned)", prompt)
        self.assertIn("KHÔNG lặp lại các dòng đã sinh ở trên", prompt)
        self.assertIn("Tiếp tục sinh code", prompt)

    def test_05_singleton_instance(self):
        p1 = get_quota_predictor()
        p2 = get_quota_predictor()
        self.assertIs(p1, p2)


if __name__ == "__main__":
    unittest.main()
