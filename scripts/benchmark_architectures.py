#!/usr/bin/env python3
"""
CursorRouter v3.0 — Reproducible Architecture & Speed Benchmark Harness
========================================================================
Compares competing architectures across 3 critical performance dimensions:
  1. HTTP Stack & Concurrency (Legacy unpooled requests vs ThreadPool+Session
     vs asyncio+aiohttp HTTP/1.1 vs persistent asyncio+httpx HTTP/2 Multiplexing)
  2. Token Decryption Engine (Uncached PBKDF2 vs Bounded LRU Cache)
  3. SQLite Persistence Layer (Per-row multi-connection commits vs Batched WAL)

Usage:
    python scripts/benchmark_architectures.py --cookies-dir Cookies
    python scripts/benchmark_architectures.py --cookies-dir Cookies --output benchmark.json
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import requests

from async_sync_engine import AsyncCursorSyncEngine
from pkce_auth import extract_jwt_from_session_cookie, parse_cookie_from_file
from token_cipher import clear_decrypt_cache, decrypt_token, encrypt_token

API_BASE = "https://api2.cursor.sh"
GET_ME_URL = f"{API_BASE}/aiserver.v1.DashboardService/GetMe"
USAGE_URL = f"{API_BASE}/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
POLICY_URL = f"{API_BASE}/aiserver.v1.DashboardService/GetUsageLimitPolicyStatus"


def _mask(email: str) -> str:
    if "@" not in email:
        return email[:3] + "***"
    local, domain = email.split("@", 1)
    return f"{local[:3]}***@{domain}"


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Origin": "https://cursor.com",
        "User-Agent": "CursorRouter-Benchmark/3.0",
    }


def load_test_tokens(cookies_dir: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    if not os.path.isdir(cookies_dir):
        return items
    for fname in sorted(os.listdir(cookies_dir)):
        if not fname.lower().endswith((".txt", ".json", ".cookie")):
            continue
        fpath = os.path.join(cookies_dir, fname)
        if not os.path.isfile(fpath):
            continue
        cookie = parse_cookie_from_file(fpath)
        if not cookie:
            continue
        tok = extract_jwt_from_session_cookie(cookie)
        if not tok:
            continue
        items.append({
            "id": len(items) + 1,
            "email": os.path.splitext(fname)[0],
            "cookie_full": cookie,
            "access_token": tok,
        })
    return items


# ---------------------------------------------------------------------------
# Suite 1: Network Transport & Concurrency Benchmark
# ---------------------------------------------------------------------------

def bench_legacy_unpooled_requests(items: List[Dict[str, Any]], workers: int = 5) -> Dict[str, Any]:
    """Legacy architecture: unpooled requests.post per RPC (3 RPCs/account, 5 workers)."""
    def _one(item: Dict[str, Any]) -> bool:
        hdrs = _headers(item["access_token"])
        r1 = requests.post(GET_ME_URL, headers=hdrs, json={}, timeout=10)
        if r1.status_code != 200:
            return False
        requests.post(USAGE_URL, headers=hdrs, json={}, timeout=10)
        requests.post(POLICY_URL, headers=hdrs, json={}, timeout=10)
        return True

    t0 = time.perf_counter()
    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_one, items):
            if res:
                ok += 1
    ms = (time.perf_counter() - t0) * 1000.0
    return {"name": "Legacy unpooled requests (3 RPCs, 5w)", "ms": round(ms, 2), "ok": ok, "total": len(items)}


def bench_threadpool_session(items: List[Dict[str, Any]], workers: int = 12) -> Dict[str, Any]:
    """ThreadPool(12w) with thread-local requests.Session keep-alive (2 RPCs/account)."""
    tls = threading.local()

    def _get_session() -> requests.Session:
        if not hasattr(tls, "s"):
            tls.s = requests.Session()
        return tls.s

    def _one(item: Dict[str, Any]) -> bool:
        s = _get_session()
        hdrs = _headers(item["access_token"])
        r1 = s.post(GET_ME_URL, headers=hdrs, json={}, timeout=10)
        if r1.status_code != 200:
            return False
        s.post(USAGE_URL, headers=hdrs, json={}, timeout=10)
        return True

    t0 = time.perf_counter()
    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_one, items):
            if res:
                ok += 1
    ms = (time.perf_counter() - t0) * 1000.0
    return {"name": "ThreadPool(12w) + requests.Session (2 RPCs)", "ms": round(ms, 2), "ok": ok, "total": len(items)}


def bench_async_h2_engine(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """v3.0 Winning Architecture: persistent asyncio + httpx HTTP/2 multiplexed engine."""
    engine = AsyncCursorSyncEngine(max_concurrency=16, timeout_sec=10.0)
    results_out: List[Dict[str, Any]] = []

    # Cold pass (full 2-RPC sync: GetMe + GetCurrentPeriodUsage)
    t0 = time.perf_counter()
    res_cold = engine.sync_accounts(items, hydrate_missing_profile=True)
    ms_cold = (time.perf_counter() - t0) * 1000.0
    ok_cold = sum(1 for r in res_cold if r.get("success"))
    results_out.append({
        "name": "v3.0 Persistent httpx HTTP/2 (Cold 2-RPC)",
        "ms": round(ms_cold, 2),
        "ok": ok_cold,
        "total": len(items),
    })

    # Warm pass (1-RPC fast refresh over coalesced H2 stream)
    t1 = time.perf_counter()
    res_warm = engine.sync_accounts(items, hydrate_missing_profile=False)
    ms_warm = (time.perf_counter() - t1) * 1000.0
    ok_warm = sum(1 for r in res_warm if r.get("success"))
    results_out.append({
        "name": "v3.0 Persistent httpx HTTP/2 (Warm 1-RPC)",
        "ms": round(ms_warm, 2),
        "ok": ok_warm,
        "total": len(items),
    })
    return results_out


# ---------------------------------------------------------------------------
# Suite 2: Token Cryptography Benchmark (Uncached vs LRU Cache)
# ---------------------------------------------------------------------------

def bench_crypto(account_count: int = 100) -> Dict[str, Any]:
    encrypted_fields = [
        encrypt_token(f"eyJhbGciOiJIUzI1NiJ9.dummy_payload_{i}.signature_{i}")
        for i in range(account_count * 3)
    ]

    # Cold pass (empty LRU cache = full PBKDF2 derivation per field)
    clear_decrypt_cache()
    t0 = time.perf_counter()
    for val in encrypted_fields:
        decrypt_token(val)
    uncached_ms = (time.perf_counter() - t0) * 1000.0

    # Warm pass (bounded LRU cache hit)
    t1 = time.perf_counter()
    for val in encrypted_fields:
        decrypt_token(val)
    cached_ms = (time.perf_counter() - t1) * 1000.0

    return {
        "fields": len(encrypted_fields),
        "uncached_pbkdf2_ms": round(uncached_ms, 2),
        "lru_cached_ms": round(cached_ms, 4),
        "speedup_x": round(uncached_ms / max(cached_ms, 1e-6), 1),
    }


# ---------------------------------------------------------------------------
# Suite 3: SQLite Persistence Benchmark (Per-Row vs Single Batched WAL)
# ---------------------------------------------------------------------------

def bench_sqlite(row_count: int = 50) -> Dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="cr_bench_sql_")
    db_legacy = os.path.join(tmp, "legacy.db")
    db_wal = os.path.join(tmp, "wal.db")

    schema = """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            usage_percent REAL,
            total_spend REAL,
            status TEXT
        );
    """

    for path, wal in ((db_legacy, False), (db_wal, True)):
        con = sqlite3.connect(path)
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(schema)
        con.executemany(
            "INSERT INTO accounts (email, usage_percent, total_spend, status) VALUES (?, 0, 0, 'PENDING')",
            [(f"user_{i}@example.com",) for i in range(row_count)],
        )
        con.commit()
        con.close()

    # Legacy: 3 separate open/commit/close per row
    t0 = time.perf_counter()
    for i in range(1, row_count + 1):
        for sql in (
            "UPDATE accounts SET usage_percent = ? WHERE id = ?",
            "UPDATE accounts SET total_spend = ? WHERE id = ?",
            "UPDATE accounts SET status = 'READY' WHERE id = ?",
        ):
            con = sqlite3.connect(db_legacy)
            if "?" in sql and sql.count("?") == 2:
                con.execute(sql, (float(i), i))
            else:
                con.execute(sql, (i,))
            con.commit()
            con.close()
    legacy_ms = (time.perf_counter() - t0) * 1000.0

    # v3.0: Single batched WAL transaction
    t1 = time.perf_counter()
    con = sqlite3.connect(db_wal)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    with con:
        con.executemany(
            "UPDATE accounts SET usage_percent = ?, total_spend = ?, status = 'READY' WHERE id = ?",
            [(float(i), float(i), i) for i in range(1, row_count + 1)],
        )
    con.close()
    wal_ms = (time.perf_counter() - t1) * 1000.0

    try:
        os.remove(db_legacy)
        for ext in ("", "-wal", "-shm"):
            p = db_wal + ext
            if os.path.exists(p):
                os.remove(p)
        os.rmdir(tmp)
    except OSError:
        pass

    return {
        "rows": row_count,
        "legacy_per_row_ms": round(legacy_ms, 2),
        "batched_wal_ms": round(wal_ms, 2),
        "speedup_x": round(legacy_ms / max(wal_ms, 1e-6), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="CursorRouter v3.0 Architecture Benchmark")
    parser.add_argument("--cookies-dir", default=os.path.join(ROOT_DIR, "Cookies"), help="Path to cookie files folder")
    parser.add_argument("--skip-network", action="store_true", help="Run only local Crypto + SQLite suites")
    parser.add_argument("--output", default="", help="Optional JSON output file")
    args = parser.parse_args()

    report: Dict[str, Any] = {"network": [], "crypto": {}, "sqlite": {}}

    print("=== CursorRouter v3.0 Architecture Benchmark ===")
    if not args.skip_network:
        items = load_test_tokens(args.cookies_dir)
        if not items:
            print(f"[Network] No parseable cookie files found in {args.cookies_dir} — skipping Suite 1.")
        else:
            print(f"[Suite 1] Loaded {len(items)} cookie(s): {', '.join(_mask(x['email']) for x in items)}")
            r_legacy = bench_legacy_unpooled_requests(items)
            r_pool = bench_threadpool_session(items)
            r_h2 = bench_async_h2_engine(items)
            report["network"] = [r_legacy, r_pool, *r_h2]
            for row in report["network"]:
                print(f"  - {row['name']:<46} {row['ms']:>8.2f} ms  ({row['ok']}/{row['total']} OK)")

    report["crypto"] = bench_crypto(100)
    print(
        f"[Suite 2] Crypto ({report['crypto']['fields']} fields): "
        f"Uncached={report['crypto']['uncached_pbkdf2_ms']} ms vs "
        f"LRU={report['crypto']['lru_cached_ms']} ms ({report['crypto']['speedup_x']}x faster)"
    )

    report["sqlite"] = bench_sqlite(50)
    print(
        f"[Suite 3] SQLite ({report['sqlite']['rows']} rows): "
        f"Per-row={report['sqlite']['legacy_per_row_ms']} ms vs "
        f"Batched WAL={report['sqlite']['batched_wal_ms']} ms ({report['sqlite']['speedup_x']}x faster)"
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"Saved report -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
