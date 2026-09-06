import time
import json
import asyncio
from typing import Optional, Dict, Any, List
from aiohttp import web, ClientSession, ClientTimeout
from token_pool import TokenPoolManager

class TransparentCursorProxy:
    def __init__(
        self,
        proxy_host: str = "127.0.0.1",
        proxy_port: int = 8080,
        target_backend_url: str = "http://127.0.0.1:8089",
        db_path: Optional[str] = None
    ):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.target_backend_url = target_backend_url.rstrip("/")
        self.pool = TokenPoolManager(db_path=db_path)
        
        # Configuration
        self.lookup_strategy = "cache" # "cache" or "sqlite"
        self.midstream_strategy = "passthrough" # "passthrough", "full_buffer", "attempt_splice"
        self.max_swap_retries = 5
        
        # Metrics storage
        self.metrics_log: List[Dict[str, Any]] = []
        self.lock = asyncio.Lock()
        
        # aiohttp app
        self.app = web.Application()
        self.app.router.add_route("*", "/{path:.*}", self.handle_proxy_request)
        self.session: Optional[ClientSession] = None

    async def init_session(self):
        if self.session is None or self.session.closed:
            # High-performance client session with persistent keep-alive connections
            timeout = ClientTimeout(total=60.0, connect=5.0)
            self.session = ClientSession(timeout=timeout)

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def set_lookup_strategy(self, strategy: str):
        """Set to 'cache' (in-memory) or 'sqlite' (direct DB query)."""
        if strategy in ("cache", "sqlite"):
            self.lookup_strategy = strategy

    def set_midstream_strategy(self, strategy: str):
        """Set midstream handling strategy."""
        if strategy in ("passthrough", "full_buffer", "attempt_splice"):
            self.midstream_strategy = strategy

    async def handle_proxy_request(self, request: web.Request) -> web.StreamResponse:
        await self.init_session()
        t0 = time.perf_counter_ns()
        
        # Read incoming path, query string, and body
        rel_path = request.match_info.get("path", "")
        
        # Support both reverse proxy and forward proxy (absolute URL)
        if rel_path.startswith("http://") or rel_path.startswith("https://"):
            target_url = rel_path
        else:
            target_url = f"{self.target_backend_url}/{rel_path}"
            
        if request.query_string and "?" not in target_url:
            target_url = f"{target_url}?{request.query_string}"
            
        req_body = await request.read()
        
        # Health / config check endpoints on the proxy itself
        if rel_path == "proxy/health":
            return web.Response(text="Proxy OK", status=200)
        if rel_path == "proxy/metrics":
            return web.json_response(self.metrics_log)
        if rel_path == "proxy/reset_metrics":
            async with self.lock:
                self.metrics_log.clear()
            self.pool.reset_rate_limits()
            return web.json_response({"status": "metrics_cleared"})
        if rel_path == "proxy/config":
            return web.json_response({
                "target_backend_url": self.target_backend_url,
                "lookup_strategy": self.lookup_strategy,
                "midstream_strategy": self.midstream_strategy,
                "pool_stats": self.pool.get_stats()
            })
        if rel_path == "proxy/set_config":
            try:
                data = json.loads(req_body.decode("utf-8"))
                if "lookup_strategy" in data:
                    self.set_lookup_strategy(data["lookup_strategy"])
                if "midstream_strategy" in data:
                    self.set_midstream_strategy(data["midstream_strategy"])
                if "max_swap_retries" in data:
                    self.max_swap_retries = int(data["max_swap_retries"])
                return web.json_response({
                    "status": "config_updated",
                    "lookup_strategy": self.lookup_strategy,
                    "midstream_strategy": self.midstream_strategy,
                    "max_swap_retries": self.max_swap_retries
                })
            except Exception as e:
                return web.Response(status=400, text=f"Invalid config json: {e}")

        # Extract Authorization header
        incoming_auth = request.headers.get("Authorization", "")
        current_token = incoming_auth.replace("Bearer ", "").strip()
        orig_token = current_token
        
        # Proactive Rate-Limit Check: If token is empty or already known to be exhausted in pool,
        # swap immediately in memory before wasting a round-trip to the cloud backend!
        proactive_swap = False
        t_proactive_swap_start = None
        if not current_token or self.pool.is_token_rate_limited(current_token):
            t_proactive_swap_start = time.perf_counter_ns()
            best_acc = (
                self.pool.get_token_from_cache(exclude_tokens={current_token}) 
                if self.lookup_strategy == "cache" 
                else self.pool.get_token_from_db_direct(exclude_tokens={current_token})
            )
            if best_acc:
                current_token = best_acc["access_token"]
                proactive_swap = True

        headers_attempt = dict(request.headers)
        headers_attempt["Authorization"] = f"Bearer {current_token}"
        # Filter out hop-by-hop headers
        headers_attempt.pop("Host", None)
        headers_attempt.pop("Content-Length", None)

        metric = {
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
            "initial_token": orig_token[:15] + "..." if orig_token else "none",
            "final_token": current_token[:15] + "..." if current_token else "none",
            "lookup_strategy": self.lookup_strategy,
            "midstream_strategy": self.midstream_strategy
        }

        if proactive_swap and t_proactive_swap_start:
            metric["swap_latency_ms"] = (time.perf_counter_ns() - t_proactive_swap_start) / 1_000_000.0
            if best_acc:
                metric["db_query_time_ms"] = best_acc.get("query_time_ms", 0.0)

        # Case: Full Buffer Strategy (Buffers whole response before sending to client)
        if self.midstream_strategy == "full_buffer":
            return await self._handle_full_buffer(request, target_url, headers_attempt, req_body, current_token, metric, t0)

        # Normal Streaming Path
        try:
            backend_resp = await self.session.request(
                method=request.method,
                url=target_url,
                headers=headers_attempt,
                data=req_body
            )
        except Exception as e:
            return web.Response(status=502, text=f"Bad Gateway: {str(e)}")

        metric["first_status"] = backend_resp.status

        # ---------------------------------------------------------------------
        # SCENARIO A: 429 AT INCEPTION (Multi-Hop Cascading Retry Loop)
        # ---------------------------------------------------------------------
        attempted_tokens = {orig_token, current_token}
        swap_hops = metric["swap_hops"]

        while backend_resp.status in (429, 401, 402) and swap_hops < self.max_swap_retries:
            await backend_resp.read() # Consume error response body
            t_swap_start = time.perf_counter_ns()
            
            # 1. Mark exhausted account (O(1) in RAM, non-blocking background SQLite persist)
            self.pool.mark_rate_limited(current_token, reason=f"HTTP {backend_resp.status} at inception", async_db=True)
            
            # 2. Select replacement account excluding all attempted tokens
            if self.lookup_strategy == "cache":
                new_acc = self.pool.get_token_from_cache(exclude_tokens=attempted_tokens)
            else:
                new_acc = self.pool.get_token_from_db_direct(exclude_tokens=attempted_tokens)
                
            if not new_acc:
                metric["final_status"] = 429
                return web.Response(status=429, text="All accounts in pool are exhausted.")
                
            metric["db_query_time_ms"] += new_acc.get("query_time_ms", 0.0)
            new_token = new_acc["access_token"]
            attempted_tokens.add(new_token)
            current_token = new_token
            
            # 3. Swap header and re-dispatch
            headers_attempt["Authorization"] = f"Bearer {new_token}"
            
            t_swap_end = time.perf_counter_ns()
            metric["swap_latency_ms"] += (t_swap_end - t_swap_start) / 1_000_000.0
            metric["swap_occurred"] = True
            swap_hops += 1
            metric["swap_hops"] = swap_hops
            metric["final_token"] = new_token[:15] + "..."
            
            # Re-dispatch request
            backend_resp = await self.session.request(
                method=request.method,
                url=target_url,
                headers=headers_attempt,
                data=req_body
            )

        metric["final_status"] = backend_resp.status

        # ---------------------------------------------------------------------
        # STREAMING BACKEND RESPONSE TO CLIENT
        # ---------------------------------------------------------------------
        resp_headers = {}
        for k, v in backend_resp.headers.items():
            if k.lower() not in ("content-length", "transfer-encoding", "content-encoding"):
                resp_headers[k] = v
                
        if metric["swap_occurred"]:
            resp_headers["x-cursor-proxy-swapped"] = "true"
            resp_headers["x-cursor-proxy-hops"] = str(metric["swap_hops"])

        client_resp = web.StreamResponse(status=backend_resp.status, headers=resp_headers)
        await client_resp.prepare(request)

        first_chunk_sent = False
        bytes_streamed = 0
        chunks_received = 0
        midstream_error_detected = False
        frame_accumulator = bytearray()

        try:
            async for chunk in backend_resp.content.iter_any():
                if not first_chunk_sent:
                    metric["ttft_ms"] = (time.perf_counter_ns() - t0) / 1_000_000.0
                    first_chunk_sent = True
                    
                chunks_received += 1
                bytes_streamed += len(chunk)
                frame_accumulator.extend(chunk)
                
                # Robust Connect-RPC envelope framing parser across arbitrary TCP chunk boundaries
                while len(frame_accumulator) >= 5:
                    flag = frame_accumulator[0]
                    flen = int.from_bytes(frame_accumulator[1:5], byteorder="big")
                    total_frame_len = 5 + flen
                    if len(frame_accumulator) < total_frame_len:
                        break # Incomplete frame, wait for next TCP chunk
                    
                    frame_payload = frame_accumulator[5:total_frame_len]
                    if flag == 0x02: # Trailer frame
                        try:
                            trailer_json = json.loads(frame_payload.decode("utf-8", errors="ignore"))
                            if "error" in trailer_json:
                                midstream_error_detected = True
                                metric["midstream_error"] = trailer_json["error"]
                                self.pool.mark_rate_limited(
                                    current_token, 
                                    reason=f"Connect-RPC midstream trailer error: {trailer_json['error'].get('code', 'unknown')}", 
                                    async_db=True
                                )
                        except Exception:
                            pass
                    del frame_accumulator[:total_frame_len]
                
                # Check midstream strategy
                if midstream_error_detected and self.midstream_strategy == "attempt_splice":
                    metric["midstream_splice_attempted"] = True
                    new_acc = self.pool.get_token_from_cache(exclude_tokens=attempted_tokens)
                    if new_acc:
                        splice_headers = dict(headers_attempt)
                        splice_headers["Authorization"] = f"Bearer {new_acc['access_token']}"
                        splice_resp = await self.session.request(
                            method=request.method,
                            url=target_url,
                            headers=splice_headers,
                            data=req_body
                        )
                        async for s_chunk in splice_resp.content.iter_any():
                            await client_resp.write(s_chunk)
                    break

                await client_resp.write(chunk)
                
        except Exception as e:
            metric["stream_exception"] = str(e)
        finally:
            try:
                await client_resp.write_eof()
            except Exception:
                pass

        t_end = time.perf_counter_ns()
        metric["total_ms"] = (t_end - t0) / 1_000_000.0
        metric["bytes_streamed"] = bytes_streamed
        metric["chunks_received"] = chunks_received

        async with self.lock:
            self.metrics_log.append(metric)

        return client_resp

    async def _handle_full_buffer(self, request, target_url, headers, req_body, current_token, metric, t0):
        """Buffered strategy: captures whole response before sending to client to survive midstream drops."""
        max_retries = self.max_swap_retries
        active_token = current_token
        attempted_tokens = {current_token}
        swap_hops = metric["swap_hops"]

        for attempt in range(max_retries):
            headers["Authorization"] = f"Bearer {active_token}"
            backend_resp = await self.session.request(
                method=request.method,
                url=target_url,
                headers=headers,
                data=req_body
            )
            
            # If 429 at inception
            if backend_resp.status in (429, 401, 402):
                await backend_resp.read()
                t_swap_start = time.perf_counter_ns()
                self.pool.mark_rate_limited(active_token, reason=f"HTTP {backend_resp.status} at inception", async_db=True)
                new_acc = (
                    self.pool.get_token_from_cache(exclude_tokens=attempted_tokens)
                    if self.lookup_strategy == "cache"
                    else self.pool.get_token_from_db_direct(exclude_tokens=attempted_tokens)
                )
                if not new_acc:
                    return web.Response(status=429, text="Pool exhausted.")
                active_token = new_acc["access_token"]
                attempted_tokens.add(active_token)
                metric["swap_latency_ms"] += (time.perf_counter_ns() - t_swap_start) / 1_000_000.0
                metric["swap_occurred"] = True
                swap_hops += 1
                metric["swap_hops"] = swap_hops
                continue

            # Read full body and inspect for Connect-RPC midstream error
            body = await backend_resp.read()
            has_error_trailer = False
            
            # Search for trailer frame flag 0x02 in body using Connect-RPC envelope boundaries
            idx = 0
            while idx + 5 <= len(body):
                flag = body[idx]
                flen = int.from_bytes(body[idx+1:idx+5], byteorder="big")
                if flag == 0x02: # Trailer frame
                    payload = body[idx+5:idx+5+flen]
                    try:
                        t_obj = json.loads(payload.decode("utf-8", errors="ignore"))
                        if "error" in t_obj:
                            has_error_trailer = True
                    except Exception:
                        pass
                idx += 5 + flen

            if has_error_trailer:
                # Quota hit midstream! Discard buffer, swap account, retry!
                t_swap_start = time.perf_counter_ns()
                self.pool.mark_rate_limited(active_token, reason="Connect-RPC trailer error midstream", async_db=True)
                new_acc = (
                    self.pool.get_token_from_cache(exclude_tokens=attempted_tokens)
                    if self.lookup_strategy == "cache"
                    else self.pool.get_token_from_db_direct(exclude_tokens=attempted_tokens)
                )
                if not new_acc:
                    return web.Response(status=429, text="Pool exhausted after midstream drop.")
                active_token = new_acc["access_token"]
                attempted_tokens.add(active_token)
                metric["swap_latency_ms"] += (time.perf_counter_ns() - t_swap_start) / 1_000_000.0
                metric["swap_occurred"] = True
                swap_hops += 1
                metric["swap_hops"] = swap_hops
                metric["midstream_recovered_via_buffer"] = True
                continue

            # Successfully received valid full response!
            resp_headers = {}
            for k, v in backend_resp.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "content-encoding"):
                    resp_headers[k] = v
                    
            if metric["swap_occurred"]:
                resp_headers["x-cursor-proxy-swapped"] = "true"
                resp_headers["x-cursor-proxy-hops"] = str(metric["swap_hops"])
                
            metric["ttft_ms"] = (time.perf_counter_ns() - t0) / 1_000_000.0
            metric["total_ms"] = metric["ttft_ms"]
            metric["final_status"] = backend_resp.status
            async with self.lock:
                self.metrics_log.append(metric)
                
            return web.Response(status=backend_resp.status, headers=resp_headers, body=body)

        return web.Response(status=500, text="Failed after retries.")

if __name__ == "__main__":
    proxy = TransparentCursorProxy()
    print(f"Starting Transparent Cursor Proxy on {proxy.proxy_host}:{proxy.proxy_port} -> {proxy.target_backend_url}...")
    web.run_app(proxy.app, host=proxy.proxy_host, port=proxy.proxy_port)
