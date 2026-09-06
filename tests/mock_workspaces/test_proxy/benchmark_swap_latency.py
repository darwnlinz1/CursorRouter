import time
import json
import struct
import statistics
import asyncio
import aiohttp
from typing import List, Dict, Any

def parse_connect_frames(raw_bytes: bytes) -> List[Dict[str, Any]]:
    frames = []
    idx = 0
    while idx + 5 <= len(raw_bytes):
        flag = raw_bytes[idx]
        flen = int.from_bytes(raw_bytes[idx+1:idx+5], byteorder="big")
        payload = raw_bytes[idx+5:idx+5+flen]
        try:
            p_json = json.loads(payload.decode("utf-8"))
        except Exception:
            p_json = {"raw": repr(payload)}
        frames.append({
            "flag": flag,
            "length": flen,
            "payload": p_json
        })
        idx += 5 + flen
    return frames

def calculate_stats(data: List[float]) -> Dict[str, float]:
    if not data:
        return {}
    sorted_d = sorted(data)
    n = len(sorted_d)
    return {
        "count": n,
        "mean": statistics.mean(sorted_d),
        "std_dev": statistics.stdev(sorted_d) if n > 1 else 0.0,
        "min": min(sorted_d),
        "median_p50": statistics.median(sorted_d),
        "p90": sorted_d[int(n * 0.90)],
        "p95": sorted_d[int(n * 0.95)] if int(n * 0.95) < n else sorted_d[-1],
        "p99": sorted_d[int(n * 0.99)] if int(n * 0.99) < n else sorted_d[-1],
        "max": max(sorted_d)
    }

def print_stat_table(title: str, stats: Dict[str, float], unit: str = "ms"):
    print(f"\n{title}")
    print("-" * 65)
    print(f"| {'Metric':<18} | {'Value':<15} | {'Target / Budget':<20} |")
    print("-" * 65)
    print(f"| {'Min':<18} | {stats.get('min', 0):>10.3f} {unit:<4} | {'< 20 ms':<20} |")
    print(f"| {'P50 (Median)':<18} | {stats.get('median_p50', 0):>10.3f} {unit:<4} | {'< 20 ms':<20} |")
    print(f"| {'Mean':<18} | {stats.get('mean', 0):>10.3f} {unit:<4} | {'< 20 ms':<20} |")
    print(f"| {'P90':<18} | {stats.get('p90', 0):>10.3f} {unit:<4} | {'< 20 ms':<20} |")
    print(f"| {'P95':<18} | {stats.get('p95', 0):>10.3f} {unit:<4} | {'< 20 ms':<20} |")
    print(f"| {'P99':<18} | {stats.get('p99', 0):>10.3f} {unit:<4} | {'< 20 ms':<20} |")
    print(f"| {'Max':<18} | {stats.get('max', 0):>10.3f} {unit:<4} | {'< 20 ms':<20} |")
    print(f"| {'Std Dev':<18} | {stats.get('std_dev', 0):>10.3f} {unit:<4} | {'--':<20} |")
    print("-" * 65)

async def run_benchmark(
    proxy_url: str = "http://127.0.0.1:8080",
    mock_url: str = "http://127.0.0.1:8089",
    iterations: int = 50
) -> Dict[str, Any]:
    print("=" * 75)
    print("STARTING TRANSPARENT LOCAL PROXY LATENCY & SWAP BENCHMARK")
    print("=" * 75)

    timeout = aiohttp.ClientTimeout(total=30.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1. Health check
        async with session.get(f"{mock_url}/health") as resp:
            assert resp.status == 200, f"Mock backend not ready: {resp.status}"
        async with session.get(f"{proxy_url}/proxy/health") as resp:
            assert resp.status == 200, f"Proxy not ready: {resp.status}"
        print("[+] Backend and Proxy are healthy.")

        chat_req = {
            "conversation": [{"text": "Hello Cursor benchmark", "type": 1}],
            "modelDetails": {"modelName": "claude-3.5-sonnet"}
        }

        # ---------------------------------------------------------------------
        # TEST 1: Baseline Normal Pass-Through (Valid Token, No 429)
        # ---------------------------------------------------------------------
        print("\n--- TEST 1: Baseline Normal Pass-Through (Valid Account B) ---")
        baseline_latencies = []
        baseline_ttfts = []
        
        headers_b = {
            "Authorization": "Bearer VALID_ACCOUNT_B",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1"
        }
        
        for i in range(15):
            t0 = time.perf_counter_ns()
            async with session.post(
                f"{proxy_url}/aiserver.v1.AiService/StreamChat",
                headers=headers_b,
                json=chat_req
            ) as resp:
                assert resp.status == 200
                first_chunk = True
                t_first = 0
                async for chunk in resp.content.iter_any():
                    if first_chunk:
                        t_first = time.perf_counter_ns()
                        first_chunk = False
                t_total = time.perf_counter_ns()
                
                baseline_ttfts.append((t_first - t0) / 1_000_000.0)
                baseline_latencies.append((t_total - t0) / 1_000_000.0)

        base_ttft_stats = calculate_stats(baseline_ttfts)
        base_total_stats = calculate_stats(baseline_latencies)
        print_stat_table("Baseline TTFT (Normal 200 Pass-Through)", base_ttft_stats)

        # ---------------------------------------------------------------------
        # TEST 2: 429 Inception Swap Benchmark (In-Memory Cache)
        # ---------------------------------------------------------------------
        print(f"\n--- TEST 2: Inception 429 Swap Latency - In-Memory Cache ({iterations} runs) ---")
        async with session.post(f"{mock_url}/set_mode", json={"mode": "inception_429", "token_a": "EXHAUSTED_TOKEN_A"}) as r:
            await r.read()
        async with session.post(f"{proxy_url}/proxy/set_config", json={"lookup_strategy": "cache", "midstream_strategy": "passthrough"}) as r:
            await r.read()
        async with session.post(f"{proxy_url}/proxy/reset_metrics") as r:
            await r.read()
        
        headers_a = {
            "Authorization": "Bearer EXHAUSTED_TOKEN_A",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1"
        }

        cache_client_ttfts = []
        cache_client_totals = []

        for i in range(iterations):
            t0 = time.perf_counter_ns()
            async with session.post(
                f"{proxy_url}/aiserver.v1.AiService/StreamChat",
                headers=headers_a,
                json=chat_req
            ) as resp:
                assert resp.status == 200, f"Expected 200 from proxy after swap, got {resp.status}"
                first_chunk = True
                t_first = 0
                body = bytearray()
                async for chunk in resp.content.iter_any():
                    if first_chunk:
                        t_first = time.perf_counter_ns()
                        first_chunk = False
                    body.extend(chunk)
                t_total = time.perf_counter_ns()
                
                cache_client_ttfts.append((t_first - t0) / 1_000_000.0)
                cache_client_totals.append((t_total - t0) / 1_000_000.0)
                
            frames = parse_connect_frames(body)
            assert len(frames) > 5, "Expected stream frames from swapped account"

        async with session.get(f"{proxy_url}/proxy/metrics") as resp:
            all_metrics = await resp.json()
            swap_metrics = [m for m in all_metrics if m.get("swap_occurred") and m.get("lookup_strategy") == "cache"]
            cache_swap_latencies = [m["swap_latency_ms"] for m in swap_metrics[-iterations:]]

        cache_swap_stats = calculate_stats(cache_swap_latencies)
        cache_ttft_stats = calculate_stats(cache_client_ttfts)
        print_stat_table("In-Memory Swap Pure Execution Latency", cache_swap_stats)
        print_stat_table("In-Memory Swap End-to-End Client TTFT", cache_ttft_stats)

        # ---------------------------------------------------------------------
        # TEST 3: 429 Inception Swap Benchmark (Direct SQLite DB Queries)
        # ---------------------------------------------------------------------
        print(f"\n--- TEST 3: Inception 429 Swap Latency - Direct SQLite Query ({iterations} runs) ---")
        async with session.post(f"{proxy_url}/proxy/set_config", json={"lookup_strategy": "sqlite", "midstream_strategy": "passthrough"}) as r:
            await r.read()
        async with session.post(f"{proxy_url}/proxy/reset_metrics") as r:
            await r.read()

        sqlite_client_ttfts = []
        sqlite_client_totals = []

        for i in range(iterations):
            t0 = time.perf_counter_ns()
            async with session.post(
                f"{proxy_url}/aiserver.v1.AiService/StreamChat",
                headers=headers_a,
                json=chat_req
            ) as resp:
                assert resp.status == 200, f"Expected 200 from proxy after swap, got {resp.status}"
                first_chunk = True
                t_first = 0
                body = bytearray()
                async for chunk in resp.content.iter_any():
                    if first_chunk:
                        t_first = time.perf_counter_ns()
                        first_chunk = False
                    body.extend(chunk)
                t_total = time.perf_counter_ns()
                
                sqlite_client_ttfts.append((t_first - t0) / 1_000_000.0)
                sqlite_client_totals.append((t_total - t0) / 1_000_000.0)

        async with session.get(f"{proxy_url}/proxy/metrics") as resp:
            all_metrics = await resp.json()
            sqlite_swap_metrics = [m for m in all_metrics if m.get("swap_occurred") and m.get("lookup_strategy") == "sqlite"]
            sqlite_swap_latencies = [m["swap_latency_ms"] for m in sqlite_swap_metrics[-iterations:]]
            sqlite_db_times = [m["db_query_time_ms"] for m in sqlite_swap_metrics[-iterations:]]

        sqlite_swap_stats = calculate_stats(sqlite_swap_latencies)
        sqlite_db_stats = calculate_stats(sqlite_db_times)
        sqlite_ttft_stats = calculate_stats(sqlite_client_ttfts)
        
        print_stat_table("Direct SQLite Raw DB Query Latency", sqlite_db_stats)
        print_stat_table("Direct SQLite Total Swap Execution Latency", sqlite_swap_stats)
        print_stat_table("Direct SQLite End-to-End Client TTFT", sqlite_ttft_stats)

        # ---------------------------------------------------------------------
        # TEST 4: Proactive Memory Remap (Token Already Known Rate-Limited)
        # ---------------------------------------------------------------------
        print(f"\n--- TEST 4: Proactive In-Memory Swap Benchmark ({iterations} runs) ---")
        # Token is already marked rate-limited in memory, proxy swaps instantly in RAM
        async with session.post(f"{proxy_url}/proxy/set_config", json={"lookup_strategy": "cache", "midstream_strategy": "passthrough"}) as r:
            await r.read()
        async with session.post(f"{proxy_url}/proxy/reset_metrics") as r:
            await r.read()
        # Trigger inception 429 on KNOWN_EXHAUSTED_BENCH once so proxy learns it is exhausted
        async with session.post(f"{mock_url}/set_mode", json={"mode": "inception_429", "token_a": "KNOWN_EXHAUSTED_BENCH"}) as r:
            await r.read()
        async with session.post(f"{proxy_url}/aiserver.v1.AiService/StreamChat", headers={"Authorization": "Bearer KNOWN_EXHAUSTED_BENCH"}, json=chat_req) as r:
            await r.read()
        
        # Now run proactive benchmarks (mock backend in normal_200 mode)
        async with session.post(f"{mock_url}/set_mode", json={"mode": "normal_200"}) as r:
            await r.read()
            
        proactive_ttfts = []
        for i in range(iterations):
            t0 = time.perf_counter_ns()
            async with session.post(
                f"{proxy_url}/aiserver.v1.AiService/StreamChat",
                headers={"Authorization": "Bearer KNOWN_EXHAUSTED_BENCH"},
                json=chat_req
            ) as resp:
                assert resp.status == 200
                first_chunk = True
                t_first = 0
                async for chunk in resp.content.iter_any():
                    if first_chunk:
                        t_first = time.perf_counter_ns()
                        first_chunk = False
                proactive_ttfts.append((t_first - t0) / 1_000_000.0)

        async with session.get(f"{proxy_url}/proxy/metrics") as resp:
            all_metrics = await resp.json()
            pro_metrics = [m for m in all_metrics if m.get("proactive_swap")]
            pro_swap_latencies = [m["swap_latency_ms"] for m in pro_metrics[-iterations:]]

        pro_swap_stats = calculate_stats(pro_swap_latencies)
        pro_ttft_stats = calculate_stats(proactive_ttfts)
        print_stat_table("Proactive RAM Remap Pure Swap Latency", pro_swap_stats)
        print_stat_table("Proactive RAM Remap Client TTFT", pro_ttft_stats)

        # ---------------------------------------------------------------------
        # TEST 5: Cascading 2-Hop Inception Swap Benchmark (2 consecutive 429s -> 200)
        # ---------------------------------------------------------------------
        print(f"\n--- TEST 5: Multi-Hop Cascading Swap Latency (2 Consecutive 429s in 1 request) ---")
        cascade_swap_latencies = []
        cascade_ttfts = []
        
        for i in range(15):
            async with session.post(f"{mock_url}/set_mode", json={"mode": "cascade_429", "cascade_remaining": 2, "token_a": f"CASCADE_TOK_{i}"}) as r:
                await r.read()
            t0 = time.perf_counter_ns()
            async with session.post(
                f"{proxy_url}/aiserver.v1.AiService/StreamChat",
                headers={"Authorization": f"Bearer CASCADE_TOK_{i}"},
                json=chat_req
            ) as resp:
                assert resp.status == 200
                first_chunk = True
                t_first = 0
                async for chunk in resp.content.iter_any():
                    if first_chunk:
                        t_first = time.perf_counter_ns()
                        first_chunk = False
                cascade_ttfts.append((t_first - t0) / 1_000_000.0)

        async with session.get(f"{proxy_url}/proxy/metrics") as resp:
            all_metrics = await resp.json()
            cascade_metrics = [m for m in all_metrics if m.get("swap_hops", 0) >= 2]
            cascade_swap_latencies = [m["swap_latency_ms"] for m in cascade_metrics[-15:]]

        cascade_swap_stats = calculate_stats(cascade_swap_latencies)
        cascade_ttft_stats = calculate_stats(cascade_ttfts)
        print_stat_table("Cascading (2 Hops) Total Swap Latency", cascade_swap_stats)
        print_stat_table("Cascading (2 Hops) Client TTFT", cascade_ttft_stats)

    return {
        "base_ttft_stats": base_ttft_stats,
        "cache_swap_stats": cache_swap_stats,
        "cache_ttft_stats": cache_ttft_stats,
        "sqlite_db_stats": sqlite_db_stats,
        "sqlite_swap_stats": sqlite_swap_stats,
        "sqlite_ttft_stats": sqlite_ttft_stats,
        "pro_swap_stats": pro_swap_stats,
        "pro_ttft_stats": pro_ttft_stats,
        "cascade_swap_stats": cascade_swap_stats,
        "cascade_ttft_stats": cascade_ttft_stats
    }

if __name__ == "__main__":
    asyncio.run(run_benchmark(iterations=50))
