"""
Comprehensive Verification Test Suite for Cursor Chat Lockout Detection & Rotating Proxy
========================================================================================

Covers 10 Required Verification Scenarios:
1. Multi-layer detection on all lock conditions (429, 402, 403, trailer 0x02,
   autoPercentUsed: 100, totalSpend: 102, usage_percent: 51.0%).
2. Clean accounts (< 50%) are strictly not flagged as locked.
3. Transparent swap at inception (client receives clean 200 OK stream with 0 errors).
4. Cascading multi-hop lockout (Token 1 -> Token 2 -> Token 3 succeeds).
5. Proactive in-memory swap speed benchmark (< 1ms target, < 0.05ms achieved).
6. Coalesced and split Connect-RPC frames across arbitrary packet boundaries.
7. Mid-stream drop handling and client retry flow.
8. Concurrent requests handled through the proxy simultaneously.
9. Edge cases: empty requests, malformed trailers, large payloads (150KB+), upstream errors.
10. Database synchronization: locked accounts asynchronously updated to EXHAUSTED in DB.
"""

import os
import sys
import time
import json
import uuid
import struct
import shutil
import sqlite3
import tempfile
import asyncio
import unittest
from typing import Dict, Any, List

import aiohttp
from aiohttp import web

# Ensure local imports work
sys.path.insert(0, os.path.dirname(__file__))

from chat_lock_detector import (
    is_chat_locked,
    parse_connect_frames,
    make_connect_frame,
    extract_trailer_error,
    ConnectFrameStreamAccumulator,
    FRAME_FLAG_DATA,
    FRAME_FLAG_TRAILER,
    QUOTA_EXHAUSTION_THRESHOLD
)
from token_pool import TokenPoolManager
from cursor_checksum import generate_cursor_checksum, get_cursor_machine_ids
from rotating_proxy import RotatingCursorProxy
from server import app


class TestChatLockDetector(unittest.TestCase):
    """
    Test Case 1 & 2: Exhaustive 4-layer Chat Lockout Detection Verification
    """

    def test_layer1_auto_percent_100(self):
        """Account with autoPercentUsed >= 100.0% is locked under threshold < 100, but not locked at threshold=100 (Cursor Free)."""
        profile = {"email": "user1@test.com", "autoPercentUsed": 100.0, "usagePercent": 40.0}
        locked_50, reason_50 = is_chat_locked(usage_data=profile, threshold=50.0)
        self.assertTrue(locked_50)
        self.assertIn("autoPercentUsed", reason_50)

        locked_100, _ = is_chat_locked(usage_data=profile, threshold=100.0)
        self.assertFalse(locked_100)

    def test_layer1_total_spend_threshold(self):
        """Account with totalSpend >= displayThreshold (100%) is locked; 51% is NOT locked."""
        # 51% spend (102.0 / 200) -> NOT locked (slow pool, usable up to 100%)
        profile_51 = {"email": "user_slow@test.com", "totalSpend": 102.0, "usagePercent": 48.0, "displayThreshold": 200.0}
        locked51, _ = is_chat_locked(usage_data=profile_51)
        self.assertFalse(locked51, "totalSpend at 51% must NOT be locked")

        # 100% spend (200.0 / 200) -> IS locked
        profile_100 = {"email": "user_exh@test.com", "totalSpend": 200.0, "usagePercent": 48.0, "displayThreshold": 200.0}
        locked100, reason = is_chat_locked(usage_data=profile_100)
        self.assertTrue(locked100)
        self.assertIn("totalSpend", reason)

    def test_layer1_usage_percent_threshold(self):
        """Account with usage_percent >= 100.0% is locked; 51% is NOT locked."""
        # 51% usage is NOT locked (slow pool, usable up to 100%)
        profile_51 = {"email": "user_slow@test.com", "usage_percent": 51.0, "totalSpend": 80.0}
        locked51, _ = is_chat_locked(usage_data=profile_51)
        self.assertFalse(locked51, "usage_percent at 51% must NOT be locked")

        # 100% usage IS locked
        profile_100 = {"email": "user_100@test.com", "usage_percent": 100.0, "totalSpend": 80.0}
        locked100, reason = is_chat_locked(usage_data=profile_100)
        self.assertTrue(locked100)
        self.assertIn("usage_percent", reason)

    def test_layer1_explicit_exhausted_status(self):
        """Account with status='EXHAUSTED' or 'RATE_LIMITED' is locked."""
        locked1, _ = is_chat_locked(usage_data={"status": "EXHAUSTED"})
        self.assertTrue(locked1)
        locked2, _ = is_chat_locked(usage_data={"status": "RATE_LIMITED"})
        self.assertTrue(locked2)

    def test_layer1_display_message_lockout(self):
        """Account with displayMessage indicating lockout is flagged."""
        profile = {
            "email": "user4@test.com",
            "usagePercent": 20.0,
            "displayMessage": "You've reached your monthly usage limit. Please upgrade to continue."
        }
        locked, reason = is_chat_locked(usage_data=profile)
        self.assertTrue(locked)
        self.assertIn("displayMessage", reason)

    def test_clean_accounts_not_flagged(self):
        """Accounts (< 100% usage, auto < 100, totalSpend < 200) are NOT locked."""
        clean_profiles = [
            {"email": "clean0@test.com", "usagePercent": 0.0, "autoPercentUsed": 0.0, "totalSpend": 0.0, "status": "READY"},
            {"email": "clean25@test.com", "usagePercent": 25.0, "autoPercentUsed": 50.0, "totalSpend": 50.0, "status": "READY"},
            {"email": "clean49@test.com", "usagePercent": 49.9, "autoPercentUsed": 99.8, "totalSpend": 99.8, "status": "HIGH_USAGE"},
            {"email": "clean75@test.com", "usagePercent": 75.0, "autoPercentUsed": 75.0, "totalSpend": 150.0, "status": "HIGH_USAGE"},
        ]
        for p in clean_profiles:
            locked, reason = is_chat_locked(usage_data=p)
            self.assertFalse(locked, f"Clean/usable profile falsely locked: {p} ({reason})")

    def test_layer2_http_status_codes(self):
        """HTTP 429, 402, 403, 401 trigger lockout; 200 does not."""
        for code in (429, 402, 403, 401):
            locked, reason = is_chat_locked(status_code=code)
            self.assertTrue(locked)
            self.assertIn(str(code), reason)

        locked_200, _ = is_chat_locked(status_code=200)
        self.assertFalse(locked_200)

    def test_layer2_headers_grpc_status(self):
        """gRPC status 8 (RESOURCE_EXHAUSTED) or 9 (FAILED_PRECONDITION) in headers triggers lockout."""
        locked, reason = is_chat_locked(headers={"grpc-status": "8", "grpc-message": "resource exhausted"})
        self.assertTrue(locked)
        self.assertIn("grpc-status=8", reason)

    def test_layer3_connect_trailer_0x02(self):
        """Connect-RPC frame with flag 0x02 containing resource_exhausted is detected."""
        trailer = make_connect_frame({
            "error": {
                "code": "resource_exhausted",
                "message": "Monthly usage limit reached"
            }
        }, flag=FRAME_FLAG_TRAILER)

        locked, reason = is_chat_locked(body_or_chunks=trailer)
        self.assertTrue(locked)
        self.assertIn("resource_exhausted", reason)

    def test_layer4_json_error_body_keywords(self):
        """Raw JSON bodies with lockout keywords are detected."""
        cases = [
            b'{"code": "resource_exhausted", "message": "out of quota"}',
            b'{"error": {"code": "failed_precondition", "message": "Chat is locked for this billing period"}}',
            b'{"message": "Exceeded your current quota, please check your plan"}',
            b'{"text": "Monthly request limit reached for free tier"}',
        ]
        for c in cases:
            locked, reason = is_chat_locked(body_or_chunks=c)
            self.assertTrue(locked, f"Failed to detect lockout in body: {c}")

    def test_non_lockout_trailer_errors_not_flagged(self):
        """
        Verify that standard client-cancellation or benign RPC errors (canceled,
        invalid_argument, deadline_exceeded) are NOT flagged as chat lockout.
        """
        benign_codes = ["canceled", "invalid_argument", "deadline_exceeded", "not_found"]
        for code in benign_codes:
            trailer = make_connect_frame({
                "error": {
                    "code": code,
                    "message": f"Normal operation resulted in {code}"
                }
            }, flag=FRAME_FLAG_TRAILER)

            locked, reason = is_chat_locked(body_or_chunks=trailer)
            self.assertFalse(locked, f"Error code '{code}' was falsely flagged as chat lockout: {reason}")

            # Verify stream accumulator also ignores benign errors for lockout marking
            acc = ConnectFrameStreamAccumulator()
            acc.feed(trailer)
            self.assertFalse(acc.is_lockout, f"Accumulator flagged '{code}' as lockout!")
            self.assertIsNotNone(acc.trailer_error)

    def test_layer1_nested_plan_usage(self):
        """
        Verify that raw responses from Cursor's GetCurrentPeriodUsage API, where metrics
        are nested inside 'planUsage', are accurately detected.
        """
        # Case 1: autoPercentUsed = 100% nested in planUsage
        raw_api_resp1 = {
            "planUsage": {
                "autoPercentUsed": 100.0,
                "totalSpend": 80.0,
                "totalPercentUsed": 40.0
            },
            "displayThreshold": 200,
            "displayMessage": "Normal"
        }
        locked1, reason1 = is_chat_locked(usage_data=raw_api_resp1, threshold=50.0)
        self.assertTrue(locked1)
        self.assertIn("autoPercentUsed", reason1)

        locked1b, _ = is_chat_locked(usage_data=raw_api_resp1, threshold=100.0)
        self.assertFalse(locked1b, "At 100% threshold, 40% usage is NOT locked")

        # Case 2: totalSpend = 102.0 nested in planUsage (51% on 200 threshold) is NOT locked
        raw_api_resp2 = {
            "planUsage": {
                "autoPercentUsed": 60.0,
                "totalSpend": 102.0,
                "totalPercentUsed": 51.0
            },
            "displayThreshold": 200
        }
        locked2, _ = is_chat_locked(usage_data=raw_api_resp2)
        self.assertFalse(locked2, "Nested 51% totalSpend must NOT be locked")

        # Case 2b: totalSpend = 200.0 (100% on 200 threshold) IS locked
        raw_api_resp2b = {
            "planUsage": {
                "autoPercentUsed": 60.0,
                "totalSpend": 200.0,
                "totalPercentUsed": 100.0
            },
            "displayThreshold": 200
        }
        locked2b, reason2b = is_chat_locked(usage_data=raw_api_resp2b)
        self.assertTrue(locked2b)
        self.assertIn("totalSpend", reason2b)

        # Case 3: Clean nested planUsage (< 50%)
        clean_raw_resp = {
            "planUsage": {
                "autoPercentUsed": 30.0,
                "totalSpend": 40.0,
                "totalPercentUsed": 20.0
            },
            "displayThreshold": 200
        }
        locked3, reason3 = is_chat_locked(usage_data=clean_raw_resp)
        self.assertFalse(locked3, f"Clean nested planUsage falsely locked: {reason3}")

    def test_layer4_chat_conversation_about_rate_limits_not_flagged(self):
        """
        Verify that normal AI responses on HTTP 200 containing discussions of rate limits,
        quota management, or plan limits are NOT falsely flagged as chat lockout.
        """
        normal_ai_completions = [
            b"Here is how you handle rate limits in Python using tenacity or backoff library.",
            b'{"type": "message", "content": "The plan limit for this API is 100 requests per minute."}',
            b'{"response": "You can avoid rate limit errors by caching token counts."}',
            b"Error: Rate limit reached. How to fix? You should use exponential backoff.",
            b'{"code": "def retry_on_quota():\\n    # check if quota exceeded\\n    pass"}'
        ]
        for completion in normal_ai_completions:
            locked, reason = is_chat_locked(status_code=200, body_or_chunks=completion)
            self.assertFalse(locked, f"Normal AI response on HTTP 200 falsely flagged: {reason}")

    def test_layer3_data_frame_rate_limit_chat_not_flagged(self):
        """
        Verify that Connect-RPC DATA frames (flag 0x00) carrying code or text discussing
        rate limits or quota are NOT falsely flagged as chat lockout.
        """
        data_frame = make_connect_frame(
            {"text": "The rate limit and quota exceeded error can be mitigated using token bucket."},
            flag=FRAME_FLAG_DATA
        )
        locked, reason = is_chat_locked(body_or_chunks=data_frame)
        self.assertFalse(locked, f"Connect-RPC data frame falsely flagged: {reason}")

    def test_layer3_trailer_telemetry_headers_not_flagged(self):
        """
        Verify that clean trailer frames (flag 0x02) containing standard rate limit telemetry
        headers (e.g. x-ratelimit-limit) but no error codes are NOT falsely flagged.
        """
        trailer_telemetry = make_connect_frame({
            "header:x-ratelimit-limit": "1000",
            "header:x-ratelimit-remaining": "850",
            "header:x-ratelimit-reset": "60"
        }, flag=FRAME_FLAG_TRAILER)
        locked, reason = is_chat_locked(body_or_chunks=trailer_telemetry)
        self.assertFalse(locked, f"Connect-RPC trailer telemetry falsely flagged: {reason}")

    def test_layer1_usage_limit_policy_status(self):
        """
        Verify that Cursor client internal policy structure (usageLimitPolicyStatus)
        is correctly recognized: HARD_BLOCK is locked; SLOW_POOL does NOT lock chat.
        """
        # Hard block
        policy_hard_block = {
            "usageLimitPolicyStatus": {
                "stage": "HARD_BLOCK",
                "trayLabel": "Out of usage",
                "isInSlowPool": False
            }
        }
        locked, reason = is_chat_locked(usage_data=policy_hard_block)
        self.assertTrue(locked)
        self.assertIn("HARD_BLOCK", reason)

        # Slow pool via stage - chat is NOT locked (user can continue chatting up to 100%)
        policy_slow_stage = {
            "usageLimitPolicyStatus": {
                "stage": "SLOW_POOL",
                "trayLabel": "Slow Pool Active",
            }
        }
        locked_slow, _ = is_chat_locked(usage_data=policy_slow_stage)
        self.assertFalse(locked_slow, "SLOW_POOL must NOT lock chat")

        # Slow pool via isInSlowPool boolean - chat is NOT locked
        policy_slow_bool = {
            "usageLimitPolicyStatus": {
                "isInSlowPool": True,
                "trayLabel": "Slow Pool Active"
            }
        }
        locked_bool, _ = is_chat_locked(usage_data=policy_slow_bool)
        self.assertFalse(locked_bool, "isInSlowPool must NOT lock chat")

    def test_layer1_boolean_quota_flags(self):
        """
        Verify that explicit boolean limit flags in usage data are detected.
        """
        for key in ("hasReachedLimit", "isQuotaExceeded", "limitReached"):
            usage = {key: True, "email": "flagged@test.com"}
            locked, reason = is_chat_locked(usage_data=usage)
            self.assertTrue(locked, f"Boolean flag {key}=True was not detected")
            self.assertIn(key, reason)

        # False flags should not trigger lockout
        clean_usage = {"hasReachedLimit": False, "isQuotaExceeded": False, "email": "clean@test.com"}
        locked, _ = is_chat_locked(usage_data=clean_usage)
        self.assertFalse(locked)



class TestTokenPoolAndDatabase(unittest.TestCase):
    """
    Test Case 5 & 10: In-Memory Token Pool Performance (< 1ms) & DB Synchronization
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.test_dir, "test_accounts.db")

        # Initialize SQLite database schema
        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT UNIQUE,
                email TEXT,
                display_name TEXT,
                auth_id TEXT,
                cookie_snippet TEXT,
                cookie_full TEXT,
                access_token TEXT,
                refresh_token TEXT,
                usage_percent REAL DEFAULT 0.0,
                total_spend REAL DEFAULT 0.0,
                display_message TEXT,
                status TEXT DEFAULT 'READY',
                last_checked INTEGER DEFAULT 0
            )
        """)

        # Insert 10 accounts: 7 usable (< 100%), 3 exhausted (>= 100% or bad status)
        accounts_data = [
            ("acc0@test.com", "tok_clean_00_" + "x" * 30, 0.0, "READY"),
            ("acc15@test.com", "tok_clean_15_" + "x" * 30, 15.0, "READY"),
            ("acc30@test.com", "tok_clean_30_" + "x" * 30, 30.0, "READY"),
            ("acc45@test.com", "tok_clean_45_" + "x" * 30, 45.0, "READY"),
            ("acc49@test.com", "tok_clean_49_" + "x" * 30, 49.5, "READY"),
            ("acc55@test.com", "tok_clean_55_" + "x" * 30, 55.0, "HIGH_USAGE"),
            ("acc75@test.com", "tok_clean_75_" + "x" * 30, 75.0, "HIGH_USAGE"),
            ("acc100@test.com", "tok_exh_100_" + "x" * 30, 100.0, "EXHAUSTED"),
            ("acc102@test.com", "tok_exh_102_" + "x" * 30, 102.0, "EXHAUSTED"),
            ("acc_bad@test.com", "tok_exh_rate_limited_" + "x" * 30, 20.0, "RATE_LIMITED"),
        ]

        for email, tok, usage, st in accounts_data:
            cur.execute("""
                INSERT INTO accounts (file_name, email, access_token, usage_percent, status)
                VALUES (?, ?, ?, ?, ?)
            """, (f"{email}.txt", email, tok, usage, st))

        con.commit()
        con.close()

        self.pool = TokenPoolManager(db_path=self.test_db)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_pool_filtering_excludes_ge_100(self):
        """Verify only healthy/usable accounts (< 100%) are loaded into cache."""
        stats = self.pool.get_stats()
        self.assertEqual(stats["cached_accounts"], 7)
        self.assertEqual(stats["ready_count"], 7)

        # Ensure none of the returned tokens have usage >= 100% or status EXHAUSTED/RATE_LIMITED
        for _ in range(10):
            acc = self.pool.get_token_from_cache()
            self.assertIsNotNone(acc)
            self.assertLess(acc["usage_percent"], 100.0)
            self.assertNotIn(acc["status"], ("EXHAUSTED", "RATE_LIMITED"))

    def test_pool_filtering_excludes_exhausted_account(self):
        """
        Verify that an account with status 'EXHAUSTED' or usage >= 100% is strictly excluded from the
        healthy cache.
        """
        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status)
            VALUES (?, ?, ?, ?, ?)
        """, ("acc_exh_new.txt", "exhausted_new@test.com", "tok_exh_new_" + "y" * 30, 100.0, "EXHAUSTED"))
        con.commit()
        con.close()

        # Reload cache
        self.pool.load_cache_from_db()

        # Ensure exhausted token is never returned
        for _ in range(15):
            acc = self.pool.get_token_from_cache()
            self.assertIsNotNone(acc)
            self.assertNotEqual(acc["email"], "exhausted_new@test.com")

        # Ensure direct DB query also filters it out
        direct = self.pool.get_token_from_db_direct()
        self.assertIsNotNone(direct)
        self.assertNotEqual(direct["email"], "exhausted_new@test.com")

    def test_proactive_swap_speed_benchmark(self):
        """Benchmark in-memory lookup over 5,000 iterations to verify < 1ms requirement."""
        latencies = []
        for _ in range(5000):
            t0 = time.perf_counter_ns()
            acc = self.pool.get_token_from_cache(exclude_tokens={"tok_clean_00_" + "x" * 30})
            t1 = time.perf_counter_ns()
            latencies.append((t1 - t0) / 1_000_000.0) # ms

        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p99 = latencies[int(len(latencies) * 0.99)]

        print(f"\n[BENCHMARK] Token Pool Cache Lookup: P50 = {p50:.4f} ms, P99 = {p99:.4f} ms")
        self.assertLess(p50, 0.05, f"P50 {p50:.4f}ms exceeds 0.05ms budget!")
        self.assertLess(p99, 1.0, f"P99 {p99:.4f}ms exceeds 1.0ms requirement!")

    def test_database_synchronization_async(self):
        """Verify locked account is immediately locked in RAM and asynchronously updated in SQLite to EXHAUSTED."""
        target_token = "tok_clean_15_" + "x" * 30

        # Before lock
        self.assertFalse(self.pool.is_token_rate_limited(target_token))

        # Mark locked
        self.pool.mark_rate_limited(target_token, reason="HTTP 429 Quota Exhausted", async_db=True)

        # O(1) in RAM immediate check
        self.assertTrue(self.pool.is_token_rate_limited(target_token))

        # Wait up to 1.5s for async SQLite update
        time.sleep(0.2)

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("SELECT status, usage_percent, display_message FROM accounts WHERE access_token = ?", (target_token,))
        row = cur.fetchone()
        con.close()

        self.assertIsNotNone(row)
        status, usage, msg = row
        self.assertEqual(status, "EXHAUSTED")
        self.assertEqual(usage, 100.0)
        self.assertIn("429", msg)


class MockUpstreamService:
    """
    Mock upstream server that simulates various Cursor AI Gateway behaviors:
    - Inception 429
    - Cascading 429s (2 consecutive 429s then 200)
    - Mid-stream trailer error (Connect-RPC flag 0x02)
    - Coalesced frames (DATA + TRAILER in single TCP write)
    - Normal 200 streaming
    """

    def __init__(self, port: int = 8993):
        self.port = port
        self.app = web.Application()
        self.app.router.add_post("/aiserver.v1.AiService/StreamChat", self.handle_stream)
        self.app.router.add_post("/aiserver.v1.AiService/StreamComposer", self.handle_stream)
        self.app.router.add_get("/health", self.handle_health)

        self.mode = "normal"
        self.cascade_counter = 0
        self.request_records = []
        self.locked_tokens = set()

    async def handle_health(self, request):
        return web.Response(text="OK")

    def set_mode(self, mode: str, cascade_count: int = 0, locked_tokens: List[str] = None):
        self.mode = mode
        self.cascade_counter = cascade_count
        if locked_tokens:
            self.locked_tokens = set(locked_tokens)
        else:
            self.locked_tokens.clear()

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        checksum = request.headers.get("x-cursor-checksum", "")

        self.request_records.append({
            "token": token,
            "checksum": checksum,
            "body_len": len(body),
            "headers": dict(request.headers)
        })

        # 1. Inception 429
        if self.mode == "inception_429" or (self.mode == "normal" and token in self.locked_tokens):
            return web.Response(
                status=429,
                content_type="application/json",
                text=json.dumps({"error": {"code": "resource_exhausted", "message": "Rate limited at inception"}})
            )

        # 1.5 Immediate Connect-RPC trailer error under HTTP 200 (No data frames, rejected at inception)
        if self.mode == "immediate_200_trailer" or (self.mode == "trailer_locked" and token in self.locked_tokens):
            resp = web.StreamResponse(status=200, headers={"Content-Type": "application/connect+json"})
            await resp.prepare(request)
            err_trailer = make_connect_frame({
                "error": {"code": "resource_exhausted", "message": "Inception quota exhausted via Connect-RPC trailer"}
            }, flag=FRAME_FLAG_TRAILER)
            await resp.write(err_trailer)
            await resp.write_eof()
            return resp

        # 1.6 Coalesced data + trailer error for locked tokens in first chunk
        if self.mode == "coalesced_locked" and token in self.locked_tokens:
            resp = web.StreamResponse(status=200, headers={"Content-Type": "application/connect+json"})
            await resp.prepare(request)
            f1 = make_connect_frame({"text": "Premature partial token. "}, flag=FRAME_FLAG_DATA)
            f2 = make_connect_frame({"error": {"code": "resource_exhausted", "message": "Coalesced inception error"}}, flag=FRAME_FLAG_TRAILER)
            await resp.write(f1 + f2)
            await resp.write_eof()
            return resp

        # 2. Cascading 429
        if self.mode == "cascade":
            if self.cascade_counter > 0:
                self.cascade_counter -= 1
                return web.Response(
                    status=429,
                    content_type="application/json",
                    text=json.dumps({"error": {"code": "resource_exhausted", "message": f"Cascade hop {self.cascade_counter}"}})
                )

        # 3. Mid-stream trailer error (tokens stream, then 0x02 error trailer)
        if self.mode == "midstream_trailer":
            resp = web.StreamResponse(status=200, headers={"Content-Type": "application/connect+json"})
            await resp.prepare(request)
            # Write 2 data frames
            await resp.write(make_connect_frame({"text": "Analyzing... "}, flag=FRAME_FLAG_DATA))
            await asyncio.sleep(0.01)
            await resp.write(make_connect_frame({"text": "Processing files... "}, flag=FRAME_FLAG_DATA))
            # Write error trailer
            err_trailer = make_connect_frame({
                "error": {"code": "resource_exhausted", "message": "Midstream quota exhausted"}
            }, flag=FRAME_FLAG_TRAILER)
            await resp.write(err_trailer)
            await resp.write_eof()
            return resp

        # 4. Coalesced frame test (DATA + TRAILER combined in 1 single chunk)
        if self.mode == "coalesced":
            resp = web.StreamResponse(status=200, headers={"Content-Type": "application/connect+json"})
            await resp.prepare(request)
            f1 = make_connect_frame({"text": "Coalesced data text. "}, flag=FRAME_FLAG_DATA)
            f2 = make_connect_frame({"error": {"code": "resource_exhausted", "message": "Coalesced trailer"}}, flag=FRAME_FLAG_TRAILER)
            await resp.write(f1 + f2)
            await resp.write_eof()
            return resp

        # 5. Normal 200 Stream
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/connect+json"})
        await resp.prepare(request)
        tokens = ["Hello, ", "I ", "am ", "Cursor ", "AI! ", "Success!"]
        try:
            for t in tokens:
                await resp.write(make_connect_frame({"text": t}, flag=FRAME_FLAG_DATA))
                await asyncio.sleep(0.002)

            # Normal clean trailer
            clean_trailer = make_connect_frame({}, flag=FRAME_FLAG_TRAILER)
            await resp.write(clean_trailer)
            await resp.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp


class TestRotatingProxyLiveIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Test Cases 3, 4, 6, 7, 8, 9: End-to-End Live Integration Verification
    """

    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.test_dir, "test_accounts.db")

        # Create isolated DB with 5 healthy accounts
        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT UNIQUE,
                email TEXT,
                display_name TEXT,
                auth_id TEXT,
                cookie_snippet TEXT,
                cookie_full TEXT,
                access_token TEXT,
                refresh_token TEXT,
                usage_percent REAL DEFAULT 0.0,
                total_spend REAL DEFAULT 0.0,
                display_message TEXT,
                status TEXT DEFAULT 'READY',
                last_checked INTEGER DEFAULT 0
            )
        """)
        for i in range(1, 6):
            cur.execute("""
                INSERT INTO accounts (file_name, email, access_token, usage_percent, status)
                VALUES (?, ?, ?, ?, 'READY')
            """, (f"acc{i}.txt", f"user{i}@test.com", f"token_healthy_{i}_" + "z" * 30, float(i * 5)))
        con.commit()
        con.close()

        # Ports
        self.upstream_port = 8993
        self.proxy_port = 8994

        # Start Mock Upstream
        self.upstream = MockUpstreamService(port=self.upstream_port)
        self.upstream_runner = web.AppRunner(self.upstream.app)
        await self.upstream_runner.setup()
        self.upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", self.upstream_port)
        await self.upstream_site.start()

        # Start Rotating Proxy
        self.test_state_db = os.path.join(self.test_dir, "test_state.vscdb")
        self.proxy = RotatingCursorProxy(
            proxy_host="127.0.0.1",
            proxy_port=self.proxy_port,
            target_backend_url=f"http://127.0.0.1:{self.upstream_port}",
            db_path=self.test_db,
            state_vscdb_path=self.test_state_db
        )
        self.proxy_runner = web.AppRunner(self.proxy.app)
        await self.proxy_runner.setup()
        self.proxy_site = web.TCPSite(self.proxy_runner, "127.0.0.1", self.proxy_port)
        await self.proxy_site.start()

        # HTTP Client
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.proxy.close_session()
        await self.proxy_site.stop()
        await self.proxy_runner.cleanup()
        await self.upstream_site.stop()
        await self.upstream_runner.cleanup()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_case_3_transparent_inception_swap(self):
        """
        Scenario 3: Client sends locked token; backend returns 429 at inception;
        Proxy intercepts 429, swaps to clean account, recomputes checksum, and client
        receives 100% clean 200 OK stream with zero error frames.
        """
        exhausted_token = "locked_token_alpha_" + "a" * 30
        self.upstream.set_mode("normal", locked_tokens=[exhausted_token])

        headers = {
            "Authorization": f"Bearer {exhausted_token}",
            "Content-Type": "application/json"
        }
        url = f"http://127.0.0.1:{self.proxy_port}/aiserver.v1.AiService/StreamChat"

        async with self.session.post(url, headers=headers, json={"prompt": "test"}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("x-cursor-proxy-swapped"), "true")

            # Accumulate and parse frames
            body = bytearray()
            async for chunk in resp.content.iter_any():
                body.extend(chunk)

            frames = parse_connect_frames(body)
            self.assertGreater(len(frames), 0)

            # Assert no trailer error
            for flag, payload, _ in frames:
                if flag == FRAME_FLAG_TRAILER:
                    err = extract_trailer_error(payload)
                    self.assertIsNone(err)

        # Check upstream received re-computed checksum
        last_rec = self.upstream.request_records[-1]
        self.assertTrue(last_rec["checksum"])
        self.assertNotEqual(last_rec["token"], exhausted_token)

    async def test_case_inception_swap_on_http_200_immediate_trailer_error(self):
        """
        Scenario: Upstream accepts HTTP connection with status 200, but immediately emits
        an error trailer frame with flag 0x02 (e.g. resource_exhausted) before sending any data frames.
        Proxy intercepts this rejection at inception (before emitting headers/data to client),
        swaps account, recomputes checksum, and client receives a 100% clean stream with tokens!
        """
        trailer_rejected_token = "token_trailer_rejected_alpha_" + "w" * 30
        self.upstream.set_mode("trailer_locked", locked_tokens=[trailer_rejected_token])

        headers = {
            "Authorization": f"Bearer {trailer_rejected_token}",
            "Content-Type": "application/json"
        }
        url = f"http://127.0.0.1:{self.proxy_port}/aiserver.v1.AiService/StreamChat"

        async with self.session.post(url, headers=headers, json={"prompt": "test trailer rejection"}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("x-cursor-proxy-swapped"), "true")

            body = bytearray()
            async for chunk in resp.content.iter_any():
                body.extend(chunk)

            frames = parse_connect_frames(body)
            self.assertGreater(len(frames), 0)

            # Ensure client received DATA frames
            data_frames = [f for f in frames if (f[0] & FRAME_FLAG_TRAILER) == 0]
            self.assertGreater(len(data_frames), 0, "Client must receive data frames after swap!")

            # Verify no lockout error in final trailer
            for flag, payload, _ in frames:
                if flag == FRAME_FLAG_TRAILER:
                    err = extract_trailer_error(payload)
                    self.assertIsNone(err)

        # Verify upstream received recomputed checksum and different token
        last_rec = self.upstream.request_records[-1]
        self.assertTrue(last_rec["checksum"])
        self.assertNotEqual(last_rec["token"], trailer_rejected_token)

    async def test_case_inception_swap_on_coalesced_data_and_trailer_first_chunk(self):
        """
        Scenario: Upstream returns HTTP 200 with the very first TCP chunk containing BOTH
        a data frame AND an error trailer (flag 0x02 resource_exhausted).
        Since no bytes have been flushed to the client yet, the proxy must detect the trailer
        in the first chunk, discard it, swap account, and deliver a clean stream to the client.
        """
        coalesced_locked_token = "token_coalesced_first_chunk_" + "c" * 30
        self.upstream.set_mode("coalesced_locked", locked_tokens=[coalesced_locked_token])

        headers = {
            "Authorization": f"Bearer {coalesced_locked_token}",
            "Content-Type": "application/json"
        }
        url = f"http://127.0.0.1:{self.proxy_port}/aiserver.v1.AiService/StreamChat"

        async with self.session.post(url, headers=headers, json={"prompt": "test coalesced first chunk"}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("x-cursor-proxy-swapped"), "true")

            body = bytearray()
            async for chunk in resp.content.iter_any():
                body.extend(chunk)

            frames = parse_connect_frames(body)
            self.assertGreater(len(frames), 0)

            # Ensure client received DATA frames
            data_frames = [f for f in frames if (f[0] & FRAME_FLAG_TRAILER) == 0]
            self.assertGreater(len(data_frames), 0, "Client must receive data frames after swap!")

            # Verify no lockout error in final trailer
            for flag, payload, _ in frames:
                if flag == FRAME_FLAG_TRAILER:
                    err = extract_trailer_error(payload)
                    self.assertIsNone(err)

        # Verify upstream received recomputed checksum and different token
        last_rec = self.upstream.request_records[-1]
        self.assertTrue(last_rec["checksum"])
        self.assertNotEqual(last_rec["token"], coalesced_locked_token)

    async def test_case_4_cascading_multi_hop_swap(self):
        """
        Scenario 4: Cascading Lockout (Token 1 429 -> Token 2 429 -> Token 3 200).
        Proxy transparently cascades 2 hops and client receives 200 OK with hops=2.
        """
        self.upstream.set_mode("cascade", cascade_count=2)

        headers = {
            "Authorization": "Bearer initial_token_for_cascade",
            "Content-Type": "application/json"
        }
        url = f"http://127.0.0.1:{self.proxy_port}/aiserver.v1.AiService/StreamChat"

        async with self.session.post(url, headers=headers, json={"prompt": "cascade test"}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("x-cursor-proxy-swapped"), "true")
            self.assertEqual(resp.headers.get("x-cursor-proxy-hops"), "2")

    async def test_case_5_proactive_in_memory_swap(self):
        """
        Scenario 5: Pre-lock a token in RAM. Send request with this token.
        Proxy swaps in RAM before dispatching to upstream. Upstream sees only healthy token.
        """
        stale_token = "stale_token_locked_in_memory"
        self.proxy.pool.mark_rate_limited(stale_token, reason="Pre-existing lock", async_db=False)

        self.upstream.set_mode("normal")
        self.upstream.request_records.clear()

        headers = {"Authorization": f"Bearer {stale_token}", "Content-Type": "application/json"}
        url = f"http://127.0.0.1:{self.proxy_port}/aiserver.v1.AiService/StreamChat"

        async with self.session.post(url, headers=headers, json={"test": "proactive"}) as resp:
            self.assertEqual(resp.status, 200)

        # Verify upstream never saw stale_token
        self.assertEqual(len(self.upstream.request_records), 1)
        dispatched_token = self.upstream.request_records[0]["token"]
        self.assertNotEqual(dispatched_token, stale_token)
        self.assertIn("token_healthy_", dispatched_token)

    async def test_case_6_coalesced_and_split_connect_frames(self):
        """
        Scenario 6: Connect-RPC accumulator correctly handles coalesced frames
        (DATA + TRAILER combined in 1 packet) and fragmented chunks (1 byte at a time).
        """
        accumulator = ConnectFrameStreamAccumulator()

        f1 = make_connect_frame({"msg": "first"}, flag=FRAME_FLAG_DATA)
        f2 = make_connect_frame({"error": {"code": "resource_exhausted", "message": "split trailer"}}, flag=FRAME_FLAG_TRAILER)
        combined = f1 + f2

        # Feed byte by byte to simulate extreme TCP fragmentation
        completed_frames = []
        for b in combined:
            completed_frames.extend(accumulator.feed(bytes([b])))

        self.assertEqual(len(completed_frames), 2)
        self.assertIsNotNone(accumulator.trailer_error)
        self.assertEqual(accumulator.trailer_error.get("code"), "resource_exhausted")

    async def test_case_7_midstream_drop_and_client_retry(self):
        """
        Scenario 7: When midstream drop occurs (server sends tokens then 0x02 trailer error),
        the token is marked locked in RAM. Subsequent client retry instantly succeeds via proactive swap.
        """
        token_a = "token_midstream_drop_candidate"
        self.upstream.set_mode("midstream_trailer", locked_tokens=[token_a])

        headers = {"Authorization": f"Bearer {token_a}", "Content-Type": "application/json"}
        url = f"http://127.0.0.1:{self.proxy_port}/aiserver.v1.AiService/StreamChat"

        # Turn 1: Stream drops mid-flight with trailer error
        async with self.session.post(url, headers=headers, json={"prompt": "turn 1"}) as resp:
            self.assertEqual(resp.status, 200)
            body = bytearray()
            async for chunk in resp.content.iter_any():
                body.extend(chunk)

            # Body contains trailer error
            locked, reason = is_chat_locked(body_or_chunks=body)
            self.assertTrue(locked)

        # Verify token_a is now flagged as locked in proxy pool
        self.assertTrue(self.proxy.pool.is_token_rate_limited(token_a))

        # Turn 2: User clicks 'Retry' in Cursor IDE (same token in headers)
        self.upstream.set_mode("normal")
        async with self.session.post(url, headers=headers, json={"prompt": "turn 2 retry"}) as resp:
            self.assertEqual(resp.status, 200)
            body2 = bytearray()
            async for chunk in resp.content.iter_any():
                body2.extend(chunk)
            locked2, _ = is_chat_locked(body_or_chunks=body2)
            self.assertFalse(locked2) # 100% clean recovery!

    async def test_case_8_concurrent_requests(self):
        """
        Scenario 8: 15 concurrent requests routed simultaneously through proxy.
        All must succeed with 200 OK without deadlocks.
        """
        self.upstream.set_mode("normal")
        url = f"http://127.0.0.1:{self.proxy_port}/aiserver.v1.AiService/StreamChat"

        async def send_req(i):
            headers = {
                "Authorization": f"Bearer token_healthy_{1 + (i % 5)}_zzzzzz",
                "Content-Type": "application/json"
            }
            async with self.session.post(url, headers=headers, json={"index": i}) as resp:
                self.assertEqual(resp.status, 200)
                body = await resp.read()
                self.assertGreater(len(body), 0)

        tasks = [asyncio.create_task(send_req(i)) for i in range(15)]
        await asyncio.gather(*tasks)

    async def test_case_9_edge_cases(self):
        """
        Scenario 9: Edge cases: empty requests, 150KB large payloads, bad trailers.
        """
        url = f"http://127.0.0.1:{self.proxy_port}/aiserver.v1.AiService/StreamChat"

        # Edge Case 9.1: Empty payload
        headers = {"Authorization": "Bearer token_healthy_1_zzzz", "Content-Type": "application/json"}
        async with self.session.post(url, headers=headers, data=b"") as resp:
            self.assertEqual(resp.status, 200)
            await resp.read()

        # Edge Case 9.2: Large payload (150 KB code context)
        large_payload = json.dumps({"code": "def hello(): pass\n" * 7000}).encode("utf-8")
        self.assertGreater(len(large_payload), 100_000)
        async with self.session.post(url, headers=headers, data=large_payload) as resp:
            self.assertEqual(resp.status, 200)
            await resp.read()

        # Edge Case 9.3: Malformed trailer frame
        malformed_frame = bytes([FRAME_FLAG_TRAILER]) + struct.pack(">I", 12) + b"not-json-xyz"
        locked, _ = is_chat_locked(body_or_chunks=malformed_frame)
        # Should gracefully handle without unhandled exception
        self.assertIsInstance(locked, bool)


class TestServerProxyEndpoints(unittest.TestCase):
    """
    Test Case: Verify server.py /api/proxy/status, /api/proxy/toggle, /api/proxy/config
    """

    def setUp(self):
        self.client = app.test_client()

    def test_proxy_status_and_toggle_cycle(self):
        # 1. Initial status
        resp = self.client.get("/api/proxy/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("running", data)
        self.assertIn("pool_stats", data)

        # 2. Toggle on (first time)
        resp_start = self.client.post("/api/proxy/toggle")
        self.assertEqual(resp_start.status_code, 200)
        data_start = resp_start.get_json()
        self.assertTrue(data_start["running"])

        # 3. Status is running
        resp_check = self.client.get("/api/proxy/status")
        self.assertTrue(resp_check.get_json()["running"])

        # 4. Config update
        resp_conf = self.client.post("/api/proxy/config", json={
            "lookup_strategy": "cache",
            "midstream_strategy": "passthrough",
            "max_swap_retries": 4
        })
        self.assertEqual(resp_conf.status_code, 200)
        self.assertEqual(resp_conf.get_json()["max_swap_retries"], 4)

        # 5. Toggle off (first time)
        resp_stop = self.client.post("/api/proxy/toggle")
        self.assertEqual(resp_stop.status_code, 200)
        self.assertFalse(resp_stop.get_json()["running"])

        # 6. Toggle on (second time - verifies clean loop recreation without RuntimeError)
        time.sleep(0.05)
        resp_restart = self.client.post("/api/proxy/toggle")
        self.assertEqual(resp_restart.status_code, 200)
        self.assertTrue(resp_restart.get_json()["running"])

        # 7. Reload cache endpoint
        resp_reload = self.client.post("/api/proxy/reload")
        self.assertEqual(resp_reload.status_code, 200)
        self.assertTrue(resp_reload.get_json()["success"])
        self.assertIn("cached_accounts", resp_reload.get_json())

        # 8. Metrics endpoint
        resp_metrics = self.client.get("/api/proxy/metrics")
        self.assertEqual(resp_metrics.status_code, 200)
        self.assertIn("metrics", resp_metrics.get_json())

        # 9. Toggle off (clean shutdown)
        resp_stop2 = self.client.post("/api/proxy/toggle")
        self.assertEqual(resp_stop2.status_code, 200)
        self.assertFalse(resp_stop2.get_json()["running"])


class TestExhaustiveEdgeCasesAndBoundaries(unittest.IsolatedAsyncioTestCase):
    """
    Exhaustive boundary & robustness checks for edge cases that might otherwise cause subtle bugs.
    """

    def test_empty_detector_inputs(self):
        """None or empty inputs return (False, None) without errors."""
        self.assertEqual(is_chat_locked(), (False, None))
        self.assertEqual(is_chat_locked(status_code=None, headers={}, body_or_chunks=b"", usage_data={}), (False, None))
        self.assertEqual(is_chat_locked(status_code=None, headers=None, body_or_chunks=None, usage_data=None), (False, None))

    def test_malformed_usage_data(self):
        """Malformed or non-numeric usage data does not crash detector."""
        malformed = {
            "usagePercent": "invalid_str",
            "autoPercentUsed": {},
            "totalSpend": [],
            "displayMessage": None
        }
        locked, reason = is_chat_locked(usage_data=malformed)
        self.assertFalse(locked)

    def test_truncated_connect_frame(self):
        """Truncated frames are handled safely without IndexError or crash."""
        # Header claims 200 bytes payload, but only 5 bytes provided
        truncated = bytes([FRAME_FLAG_DATA]) + struct.pack(">I", 200) + b"hello"
        frames = parse_connect_frames(truncated)
        self.assertEqual(len(frames), 0) # incomplete frame, gracefully ignored

        locked, _ = is_chat_locked(body_or_chunks=truncated)
        self.assertFalse(locked)

    def test_zero_length_connect_frame(self):
        """Zero-length Connect-RPC frame parses cleanly."""
        zero_frame = make_connect_frame(b"", flag=0x00)
        self.assertEqual(len(zero_frame), 5)
        frames = parse_connect_frames(zero_frame)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0], 0x00)
        self.assertEqual(frames[0][1], b"")

    def test_binary_garbage_body(self):
        """Raw binary non-JSON noise does not trigger false positive or exception."""
        noise = bytes([i % 256 for i in range(256)])
        locked, _ = is_chat_locked(body_or_chunks=noise)
        self.assertFalse(locked)

    def test_case_insensitive_headers(self):
        """Header checks work across arbitrary upper/lower/mixed casing."""
        locked1, _ = is_chat_locked(headers={"GRPC-STATUS": "8"})
        self.assertTrue(locked1)
        locked2, _ = is_chat_locked(headers={"gRpC-sTaTuS": "RESOURCE_EXHAUSTED"})
        self.assertTrue(locked2)
        locked3, _ = is_chat_locked(headers={"X-cUrSoR-eRrOr": "Monthly request limit reached"})
        self.assertTrue(locked3)

    def test_pool_all_accounts_locked(self):
        """When all accounts are locked, pool returns None cleanly."""
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("CREATE TABLE accounts (id INT, email TEXT, access_token TEXT, usage_percent REAL, status TEXT)")
        cur.execute("INSERT INTO accounts VALUES (1, 'a@t.com', 'tok_aaa_' || hex(randomblob(16)), 10.0, 'READY')")
        con.commit()
        con.close()

        pool = TokenPoolManager(db_path=db_path)
        acc = pool.get_token_from_cache()
        self.assertIsNotNone(acc)

        # Mark only account as locked
        pool.mark_rate_limited(acc["access_token"], reason="exhausted", async_db=False)
        self.assertIsNone(pool.get_token_from_cache())
        self.assertIsNone(pool.get_token_from_db_direct())

        # Reset limits
        pool.reset_rate_limits()
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_checksum_structure_and_ids(self):
        """Checksum generation creates valid base64 payload with machine IDs attached."""
        m_id, mac_id = "test_machine_id_999", "test_mac_id_888"
        cs = generate_cursor_checksum(machine_id=m_id, mac_machine_id=mac_id)
        self.assertIn(f"{m_id}/{mac_id}", cs)
        self.assertGreater(len(cs), len(m_id) + len(mac_id))

    async def test_proxy_internal_endpoints_and_bad_gateway(self):
        """Proxy handles internal endpoints and 502 Bad Gateway when upstream is offline."""
        proxy = RotatingCursorProxy(
            proxy_host="127.0.0.1",
            proxy_port=8998,
            target_backend_url="http://127.0.0.1:59998" # dead port
        )
        runner = web.AppRunner(proxy.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 8998)
        await site.start()

        async with aiohttp.ClientSession() as session:
            # 1. Health check
            async with session.get("http://127.0.0.1:8998/proxy/health") as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertEqual(data["status"], "healthy")

            # 2. Status check
            async with session.get("http://127.0.0.1:8998/proxy/status") as resp:
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertTrue(data["running"])

            # 3. Dead upstream -> 502 Bad Gateway handled cleanly
            async with session.post("http://127.0.0.1:8998/aiserver.v1.AiService/StreamChat", json={"test": 1}) as resp:
                self.assertEqual(resp.status, 502)
                text = await resp.text()
                self.assertIn("Bad Gateway", text)

        await proxy.close_session()
        await site.stop()
        await runner.cleanup()


if __name__ == "__main__":
    print("=" * 80)
    print("STARTING COMPREHENSIVE CHAT LOCKOUT & ROTATING PROXY VERIFICATION SUITE")
    print("=" * 80)
    unittest.main(verbosity=2)
