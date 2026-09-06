"""
Production-Ready Transparent Rotating Proxy for Cursor AI
==========================================================

Acts as a high-performance, transparent intermediary between Cursor IDE
and the upstream Cursor AI Gateway (api2.cursor.sh).

Key Capabilities:
1. Inception Swap:
   Detects quota lockout (429, 402, 403, 401, error bodies) before any bytes are
   emitted to the client. Seamlessly selects the next healthy account (< 50% usage),
   re-computes dynamic x-cursor-checksum, and replays upstream.

2. Proactive RAM Remap:
   Maintains an O(1) set of rate-limited / locked tokens in memory. If Cursor
   sends a request with a known-locked token, swaps it in pure RAM (< 0.05ms)
   before making any cloud network call.

3. Cascading Multi-Hop Retry:
   If Account B is also locked, cascades to Account C automatically (up to max_swap_retries).

4. Streaming Connect-RPC Frame Parser:
   Accurately walks 5-byte Connect-RPC binary frames across arbitrary TCP chunk boundaries.

5. Background SQLite Persistence:
   Marks locked accounts as EXHAUSTED in cursor_accounts.db via non-blocking background
   threads to avoid stalling the async event loop.
"""

import os
import sys
import json
import time
import asyncio
import threading
from typing import Optional, Dict, Any, List, Set, Tuple
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector

from token_pool import TokenPoolManager
from chat_lock_detector import (
    is_chat_locked,
    ConnectFrameStreamAccumulator,
    extract_trailer_error,
    parse_connect_frames,
    is_connect_error_lockout,
    FRAME_FLAG_DATA,
    FRAME_FLAG_TRAILER,
    QUOTA_EXHAUSTION_THRESHOLD
)
from cursor_checksum import generate_cursor_checksum

DEFAULT_PROXY_HOST = os.getenv("CURSOR_PROXY_HOST", "127.0.0.1")
DEFAULT_PROXY_PORT = int(os.getenv("CURSOR_PROXY_PORT", "8999"))
DEFAULT_BACKEND_URL = os.getenv("CURSOR_BACKEND_URL", "https://api2.cursor.sh")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "host"
}


class RotatingCursorProxy:
    """
    Production-ready transparent proxy server for Cursor IDE.
    """

    def __init__(
        self,
        proxy_host: str = DEFAULT_PROXY_HOST,
        proxy_port: int = DEFAULT_PROXY_PORT,
        target_backend_url: str = DEFAULT_BACKEND_URL,
        db_path: Optional[str] = None,
        state_vscdb_path: Optional[str] = None
    ):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.target_backend_url = target_backend_url.rstrip("/")
        self.pool = TokenPoolManager(db_path=db_path)
        self.state_vscdb_path = state_vscdb_path

        # Configurable routing & recovery strategies
        self.lookup_strategy = "cache"       # "cache" (<0.05ms) or "sqlite" (direct DB)
        self.midstream_strategy = "passthrough" # "passthrough" (standard), "full_buffer", "attempt_splice"
        self.max_swap_retries = 5
        self.recompute_checksum = True

        # Metrics storage
        self.metrics_log: List[Dict[str, Any]] = []
        self._metrics_lock = asyncio.Lock()

        # aiohttp web application factory
        self.app = self._build_app()
        self.session: Optional[ClientSession] = None

        # Server runner state for programmatic lifecycle control
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._is_running = False

    def _build_app(self) -> web.Application:
        """Constructs a clean aiohttp Application instance bound to the active loop."""
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle_proxy_request)
        return app

    async def init_session(self):
        if self.session is None or self.session.closed:
            # Configure high-throughput persistent connector
            connector = TCPConnector(
                limit=100,
                keepalive_timeout=60.0,
                enable_cleanup_closed=True
            )
            timeout = ClientTimeout(total=120.0, connect=10.0)
            self.session = ClientSession(connector=connector, timeout=timeout)

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def set_lookup_strategy(self, strategy: str):
        if strategy in ("cache", "sqlite"):
            self.lookup_strategy = strategy

    def set_midstream_strategy(self, strategy: str):
        if strategy in ("passthrough", "full_buffer", "attempt_splice"):
            self.midstream_strategy = strategy

    def _get_replacement_token(self, exclude_tokens: Set[str]) -> Optional[Dict[str, Any]]:
        """Retrieves replacement healthy token from RAM cache or SQLite direct query."""
        if self.lookup_strategy == "cache":
            return self.pool.get_token_from_cache(exclude_tokens=exclude_tokens)
        else:
            return self.pool.get_token_from_db_direct(exclude_tokens=exclude_tokens)

    def _sync_swapped_token_to_cursor_state(self, new_acc: Dict[str, Any], wait: bool = False) -> bool:
        """Dong bo tai khoan moi duoc swap vao state.vscdb cua Cursor de Cursor IDE luon dong bo."""
        if not new_acc or not new_acc.get("access_token"):
            return False
            
        def _sync_work():
            try:
                from cursor_storage import CursorStorageManager
                sm = CursorStorageManager(db_path=self.state_vscdb_path)
                profile = {
                    "email": new_acc.get("email"),
                    "displayName": new_acc.get("display_name") or new_acc.get("name"),
                    "authId": new_acc.get("auth_id"),
                }
                sm.inject_full_profile(new_acc["access_token"], new_acc.get("refresh_token"), profile)
                if not self.state_vscdb_path:
                    try:
                        from cursor_settings import CursorSettingsManager
                        CursorSettingsManager().spoof_storage_ids()
                    except Exception:
                        pass
                return True
            except Exception as e:
                print(f"[-] Proxy loi sync sang state.vscdb: {e}")
                return False

        if wait:
            return _sync_work()
        else:
            threading.Thread(target=_sync_work, daemon=True).start()
            return True

    async def handle_proxy_request(self, request: web.Request) -> web.StreamResponse:
        await self.init_session()
        t0 = time.perf_counter_ns()

        rel_path = request.match_info.get("path", "")

        # ---------------------------------------------------------------------
        # INTERNAL CONTROL & TELEMETRY ENDPOINTS
        # ---------------------------------------------------------------------
        if rel_path in ("proxy/health", "health"):
            return web.json_response({
                "status": "healthy",
                "service": "RotatingCursorProxy",
                "uptime": time.time(),
                "proxy_port": self.proxy_port
            })

        if rel_path == "proxy/status":
            return web.json_response({
                "running": True,
                "proxy_host": self.proxy_host,
                "proxy_port": self.proxy_port,
                "target_backend_url": self.target_backend_url,
                "lookup_strategy": self.lookup_strategy,
                "midstream_strategy": self.midstream_strategy,
                "max_swap_retries": self.max_swap_retries,
                "pool_stats": self.pool.get_stats()
            })

        if rel_path == "proxy/metrics":
            async with self._metrics_lock:
                return web.json_response(self.metrics_log[-100:])

        if rel_path == "proxy/reset_metrics":
            async with self._metrics_lock:
                self.metrics_log.clear()
            self.pool.reset_rate_limits()
            return web.json_response({"status": "metrics_and_limits_reset"})

        if rel_path == "proxy/set_config":
            try:
                body = await request.read()
                data = json.loads(body.decode("utf-8")) if body else {}
                if "lookup_strategy" in data:
                    self.set_lookup_strategy(data["lookup_strategy"])
                if "midstream_strategy" in data:
                    self.set_midstream_strategy(data["midstream_strategy"])
                if "max_swap_retries" in data:
                    self.max_swap_retries = int(data["max_swap_retries"])
                if "recompute_checksum" in data:
                    self.recompute_checksum = bool(data["recompute_checksum"])
                return web.json_response({
                    "status": "config_updated",
                    "lookup_strategy": self.lookup_strategy,
                    "midstream_strategy": self.midstream_strategy,
                    "max_swap_retries": self.max_swap_retries,
                    "recompute_checksum": self.recompute_checksum
                })
            except Exception as e:
                return web.Response(status=400, text=f"Invalid config: {e}")

        # ---------------------------------------------------------------------
        # URL ROUTING & REQUEST PREPARATION
        # ---------------------------------------------------------------------
        if rel_path.startswith("http://") or rel_path.startswith("https://"):
            target_url = rel_path
        else:
            target_url = f"{self.target_backend_url}/{rel_path}"

        if request.query_string and "?" not in target_url:
            target_url = f"{target_url}?{request.query_string}"

        req_body = await request.read()

        # Extract incoming token
        incoming_auth = request.headers.get("Authorization", "")
        current_token = incoming_auth.replace("Bearer ", "").strip()
        orig_token = current_token

        # ---------------------------------------------------------------------
        # PROACTIVE RAM SWAP (< 0.05ms)
        # ---------------------------------------------------------------------
        proactive_swap = False
        t_proactive_start = None
        best_acc = None

        if not current_token or self.pool.is_token_rate_limited(current_token):
            t_proactive_start = time.perf_counter_ns()
            best_acc = self._get_replacement_token(exclude_tokens={current_token})
            if best_acc:
                current_token = best_acc["access_token"]
                proactive_swap = True

        # Build headers for upstream attempt, stripping hop-by-hop & host headers case-insensitively
        headers_attempt = {
            k: v for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP_HEADERS
        }
        if current_token:
            headers_attempt["Authorization"] = f"Bearer {current_token}"

        has_checksum = any(k.lower() == "x-cursor-checksum" for k in headers_attempt)
        if (proactive_swap and self.recompute_checksum) or (not has_checksum):
            for k in list(headers_attempt.keys()):
                if k.lower() == "x-cursor-checksum":
                    del headers_attempt[k]
            headers_attempt["x-cursor-checksum"] = generate_cursor_checksum()

        metric: Dict[str, Any] = {
            "path": rel_path,
            "query": request.query_string,
            "t0": t0,
            "swap_occurred": proactive_swap,
            "proactive_swap": proactive_swap,
            "swap_latency_ms": 0.0,
            "db_query_time_ms": 0.0,
            "swap_hops": 1 if proactive_swap else 0,
            "first_status": None,
            "final_status": None,
            "ttft_ms": 0.0,
            "total_ms": 0.0,
            "initial_token": (orig_token[:15] + "...") if orig_token else "none",
            "final_token": (current_token[:15] + "...") if current_token else "none",
            "lookup_strategy": self.lookup_strategy,
            "midstream_strategy": self.midstream_strategy
        }

        if proactive_swap and t_proactive_start:
            metric["swap_latency_ms"] = (time.perf_counter_ns() - t_proactive_start) / 1_000_000.0
            if best_acc:
                metric["db_query_time_ms"] = best_acc.get("query_time_ms", 0.0)

        # If configured for full-buffer strategy, route to buffered handler
        if self.midstream_strategy == "full_buffer":
            return await self._handle_full_buffer(request, target_url, headers_attempt, req_body, current_token, metric, t0)

        # ---------------------------------------------------------------------
        # STREAMING PATH: UPSTREAM DISPATCH & INCEPTION SWAP
        # ---------------------------------------------------------------------
        attempted_tokens = {orig_token, current_token}
        swap_hops = metric["swap_hops"]
        first_chunk = b""

        while True:
            try:
                backend_resp = await self.session.request(
                    method=request.method,
                    url=target_url,
                    headers=headers_attempt,
                    data=req_body
                )
            except Exception as e:
                return web.Response(status=502, text=f"Bad Gateway: {str(e)}")

            if metric["first_status"] is None:
                metric["first_status"] = backend_resp.status

            # Check Layer 2 HTTP status code & response headers
            locked, lock_reason = is_chat_locked(status_code=backend_resp.status, headers=dict(backend_resp.headers))

            # If HTTP status is 200, peek first chunk to catch immediate Connect-RPC trailer rejection
            if not locked and backend_resp.status == 200:
                try:
                    first_chunk = await backend_resp.content.readany()
                except Exception:
                    first_chunk = b""

                if first_chunk:
                    frames = parse_connect_frames(first_chunk)
                    if frames:
                        # Inspect trailer frames: if ANY trailer indicates lockout at inception, swap immediately!
                        for flag, payload, _ in frames:
                            if (flag & FRAME_FLAG_TRAILER) != 0:
                                err = extract_trailer_error(payload)
                                if err:
                                    is_lock, r = is_connect_error_lockout(err)
                                    if is_lock:
                                        locked = True
                                        lock_reason = f"Connect-RPC immediate trailer rejection: {r}"
                                        break
                    else:
                        # Non-framed unary response (e.g. JSON), pass status_code=200 to avoid false positives
                        locked, lock_reason = is_chat_locked(status_code=backend_resp.status, body_or_chunks=first_chunk)

            # Inception swap if locked and retries remain
            if locked and swap_hops < self.max_swap_retries:
                if not backend_resp.closed:
                    backend_resp.close()

                t_swap_start = time.perf_counter_ns()
                self.pool.mark_rate_limited(
                    current_token,
                    reason=lock_reason or f"HTTP {backend_resp.status} lockout at inception",
                    async_db=True
                )
                try:
                    from smart_task_filter import mark_task_interrupted
                    mark_task_interrupted(
                        reason=lock_reason or f"HTTP {backend_resp.status} lockout at inception",
                        source="proxy_inception"
                    )
                except Exception:
                    pass

                new_acc = self._get_replacement_token(exclude_tokens=attempted_tokens)
                if not new_acc:
                    metric["final_status"] = backend_resp.status
                    return web.Response(
                        status=429,
                        content_type="application/json",
                        text=json.dumps({
                            "error": {
                                "code": "resource_exhausted",
                                "message": "All accounts in Cursor pool are exhausted (< 50% threshold)."
                            }
                        })
                    )

                metric["db_query_time_ms"] += new_acc.get("query_time_ms", 0.0)
                new_token = new_acc["access_token"]
                self._sync_swapped_token_to_cursor_state(new_acc)
                attempted_tokens.add(new_token)
                current_token = new_token

                headers_attempt["Authorization"] = f"Bearer {new_token}"
                if self.recompute_checksum:
                    for k in list(headers_attempt.keys()):
                        if k.lower() == "x-cursor-checksum":
                            del headers_attempt[k]
                    headers_attempt["x-cursor-checksum"] = generate_cursor_checksum()

                t_swap_end = time.perf_counter_ns()
                metric["swap_latency_ms"] += (t_swap_end - t_swap_start) / 1_000_000.0
                metric["swap_occurred"] = True
                swap_hops += 1
                metric["swap_hops"] = swap_hops
                metric["final_token"] = new_token[:15] + "..."
                first_chunk = b""
                continue

            break

        metric["final_status"] = backend_resp.status

        # ---------------------------------------------------------------------
        # STREAMING UPSTREAM RESPONSE TO CLIENT
        # ---------------------------------------------------------------------
        resp_headers = {}
        for k, v in backend_resp.headers.items():
            if k.lower() not in HOP_BY_HOP_HEADERS:
                resp_headers[k] = v

        if metric["swap_occurred"]:
            resp_headers["x-cursor-proxy-swapped"] = "true"
            resp_headers["x-cursor-proxy-hops"] = str(metric["swap_hops"])

        client_resp = web.StreamResponse(status=backend_resp.status, headers=resp_headers)
        await client_resp.prepare(request)

        first_chunk_sent = False
        bytes_streamed = 0
        chunks_received = 0
        accumulator = ConnectFrameStreamAccumulator()

        # Emit peeked first chunk if present
        if first_chunk:
            metric["ttft_ms"] = (time.perf_counter_ns() - t0) / 1_000_000.0
            first_chunk_sent = True
            chunks_received += 1
            bytes_streamed += len(first_chunk)
            accumulator.feed(first_chunk)
            if accumulator.is_lockout:
                metric["midstream_error"] = accumulator.trailer_error
                self.pool.mark_rate_limited(
                    current_token,
                    reason=f"Connect-RPC midstream trailer lockout: {accumulator.lockout_reason}",
                    async_db=True
                )
            try:
                await client_resp.write(first_chunk)
            except (ConnectionResetError, asyncio.CancelledError):
                pass

        try:
            async for chunk in backend_resp.content.iter_any():
                if not first_chunk_sent:
                    metric["ttft_ms"] = (time.perf_counter_ns() - t0) / 1_000_000.0
                    first_chunk_sent = True

                chunks_received += 1
                bytes_streamed += len(chunk)

                # Feed through Connect-RPC accumulator
                completed_frames = accumulator.feed(chunk)
                if accumulator.is_lockout:
                    # Midstream lockout detected!
                    err_info = accumulator.trailer_error or {}
                    metric["midstream_error"] = err_info
                    self.pool.mark_rate_limited(
                        current_token,
                        reason=f"Connect-RPC midstream lockout: {accumulator.lockout_reason}",
                        async_db=True
                    )
                    try:
                        from smart_task_filter import mark_task_interrupted
                        mark_task_interrupted(
                            reason=f"Connect-RPC midstream lockout: {accumulator.lockout_reason}",
                            source="proxy_midstream"
                        )
                    except Exception:
                        pass

                # Check if client requested naive splicing attempt
                if accumulator.is_lockout and self.midstream_strategy == "attempt_splice":
                    metric["midstream_splice_attempted"] = True
                    new_acc = self.pool.get_token_from_cache(exclude_tokens=attempted_tokens)
                    if new_acc:
                        splice_headers = dict(headers_attempt)
                        splice_headers["Authorization"] = f"Bearer {new_acc['access_token']}"
                        if self.recompute_checksum:
                            splice_headers["x-cursor-checksum"] = generate_cursor_checksum()
                        splice_resp = await self.session.request(
                            method=request.method,
                            url=target_url,
                            headers=splice_headers,
                            data=req_body
                        )
                        async for s_chunk in splice_resp.content.iter_any():
                            await client_resp.write(s_chunk)
                    break

                try:
                    await client_resp.write(chunk)
                except (ConnectionResetError, asyncio.CancelledError):
                    break
        except Exception as e:
            metric["stream_exception"] = str(e)
        finally:
            try:
                await client_resp.write_eof()
            except Exception:
                pass
            if backend_resp and not backend_resp.closed:
                backend_resp.close()
            if not accumulator.is_lockout and backend_resp and backend_resp.status == 200:
                try:
                    from smart_task_filter import mark_task_completed
                    mark_task_completed(source="proxy_stream_success")
                except Exception:
                    pass

        t_end = time.perf_counter_ns()
        metric["total_ms"] = (t_end - t0) / 1_000_000.0
        metric["bytes_streamed"] = bytes_streamed
        metric["chunks_received"] = chunks_received

        async with self._metrics_lock:
            self.metrics_log.append(metric)
            if len(self.metrics_log) > 1000:
                self.metrics_log = self.metrics_log[-500:]

        return client_resp

    async def _handle_full_buffer(
        self,
        request: web.Request,
        target_url: str,
        headers: Dict[str, str],
        req_body: bytes,
        current_token: str,
        metric: Dict[str, Any],
        t0: int
    ) -> web.Response:
        """
        Full-Response Buffering strategy:
        Captures the entire response in memory before emitting to the client.
        Survives midstream trailer drops at the cost of TTFT.
        """
        max_retries = self.max_swap_retries
        active_token = current_token
        attempted_tokens = {current_token}
        swap_hops = metric["swap_hops"]

        for attempt in range(max_retries):
            headers["Authorization"] = f"Bearer {active_token}"
            if self.recompute_checksum:
                for k in list(headers.keys()):
                    if k.lower() == "x-cursor-checksum":
                        del headers[k]
                headers["x-cursor-checksum"] = generate_cursor_checksum()

            try:
                backend_resp = await self.session.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    data=req_body
                )
            except Exception as e:
                return web.Response(status=502, text=f"Bad Gateway: {e}")

            # Check inception lockout
            locked, lock_reason = is_chat_locked(status_code=backend_resp.status, headers=dict(backend_resp.headers))
            if locked:
                await backend_resp.read()
                t_swap_start = time.perf_counter_ns()
                self.pool.mark_rate_limited(active_token, reason=lock_reason or "HTTP lockout at inception", async_db=True)
                new_acc = self._get_replacement_token(exclude_tokens=attempted_tokens)
                if not new_acc:
                    return web.Response(status=429, text="Pool exhausted.")
                active_token = new_acc["access_token"]
                self._sync_swapped_token_to_cursor_state(new_acc)
                attempted_tokens.add(active_token)
                metric["swap_latency_ms"] += (time.perf_counter_ns() - t_swap_start) / 1_000_000.0
                metric["swap_occurred"] = True
                swap_hops += 1
                metric["swap_hops"] = swap_hops
                continue

            # Read full body and inspect for Connect-RPC midstream trailer error
            body = await backend_resp.read()
            locked_body, lock_reason_body = is_chat_locked(status_code=backend_resp.status, body_or_chunks=body)

            if locked_body:
                t_swap_start = time.perf_counter_ns()
                self.pool.mark_rate_limited(active_token, reason=lock_reason_body or "Midstream trailer error", async_db=True)
                new_acc = self._get_replacement_token(exclude_tokens=attempted_tokens)
                if not new_acc:
                    return web.Response(status=429, text="Pool exhausted after midstream drop.")
                active_token = new_acc["access_token"]
                self._sync_swapped_token_to_cursor_state(new_acc)
                attempted_tokens.add(active_token)
                metric["swap_latency_ms"] += (time.perf_counter_ns() - t_swap_start) / 1_000_000.0
                metric["swap_occurred"] = True
                swap_hops += 1
                metric["swap_hops"] = swap_hops
                metric["midstream_recovered_via_buffer"] = True
                continue

            # Successful response received
            resp_headers = {}
            for k, v in backend_resp.headers.items():
                if k.lower() not in HOP_BY_HOP_HEADERS:
                    resp_headers[k] = v

            if metric["swap_occurred"]:
                resp_headers["x-cursor-proxy-swapped"] = "true"
                resp_headers["x-cursor-proxy-hops"] = str(metric["swap_hops"])

            metric["ttft_ms"] = (time.perf_counter_ns() - t0) / 1_000_000.0
            metric["total_ms"] = metric["ttft_ms"]
            metric["final_status"] = backend_resp.status

            async with self._metrics_lock:
                self.metrics_log.append(metric)
                if len(self.metrics_log) > 1000:
                    self.metrics_log = self.metrics_log[-500:]

            return web.Response(status=backend_resp.status, headers=resp_headers, body=body)

        return web.Response(status=500, text="Failed after maximum retries.")

    # -------------------------------------------------------------------------
    # PROGRAMMATIC LIFECYCLE MANAGEMENT (FOR SERVER.PY INTEGRATION)
    # -------------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._is_running

    def start_in_thread(self) -> bool:
        """Starts the proxy server inside a dedicated background daemon thread."""
        if self._is_running:
            return False

        ready_event = threading.Event()
        error_container = []

        def _run_server():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            async def _start():
                try:
                    await self.init_session()
                    self.app = self._build_app()
                    self._runner = web.AppRunner(self.app)
                    await self._runner.setup()
                    self._site = web.TCPSite(self._runner, self.proxy_host, self.proxy_port, reuse_address=True)
                    for attempt in range(5):
                        try:
                            await self._site.start()
                            break
                        except OSError:
                            if attempt == 4:
                                raise
                            await asyncio.sleep(0.1)
                    self._is_running = True
                    ready_event.set()
                except Exception as ex:
                    error_container.append(ex)
                    ready_event.set()

            loop.run_until_complete(_start())
            if not error_container:
                try:
                    loop.run_forever()
                finally:
                    loop.run_until_complete(self._cleanup_async())
                    loop.close()

        self._thread = threading.Thread(target=_run_server, daemon=True)
        self._thread.start()
        ready_event.wait(timeout=10.0)

        if error_container:
            self._is_running = False
            raise error_container[0]

        return self._is_running

    async def _cleanup_async(self):
        try:
            await self.close_session()
        except Exception:
            pass
        try:
            if self._site:
                await self._site.stop()
        except Exception:
            pass
        try:
            if self._runner:
                await self._runner.cleanup()
        except Exception:
            pass
        self._site = None
        self._runner = None
        self.session = None
        self._is_running = False

    def stop_thread(self):
        """Signals the background server thread to cleanly stop and cleans up."""
        if not self._is_running or not self._loop:
            return

        def _stop():
            self._is_running = False
            self._loop.stop()

        self._loop.call_soon_threadsafe(_stop)
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._loop = None
        self._is_running = False


# Alias for backward compatibility with test_proxy scripts
TransparentCursorProxy = RotatingCursorProxy


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cursor Rotating Proxy Server")
    parser.add_argument("--host", default=DEFAULT_PROXY_HOST, help="Proxy host")
    parser.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT, help="Proxy port")
    parser.add_argument("--backend", default=DEFAULT_BACKEND_URL, help="Upstream backend URL")
    args = parser.parse_args()

    proxy = RotatingCursorProxy(proxy_host=args.host, proxy_port=args.port, target_backend_url=args.backend)
    print(f"[*] Starting Rotating Cursor Proxy on http://{proxy.proxy_host}:{proxy.proxy_port} -> {proxy.target_backend_url}...")
    web.run_app(proxy.app, host=proxy.proxy_host, port=proxy.proxy_port)


if __name__ == "__main__":
    main()
