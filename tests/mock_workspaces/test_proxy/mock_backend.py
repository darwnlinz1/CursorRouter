import struct
import json
import asyncio
from aiohttp import web

# Connect-RPC Frame Helpers
def make_connect_frame(payload_dict: dict, flag: int = 0x00) -> bytes:
    data = json.dumps(payload_dict).encode("utf-8")
    return bytes([flag]) + struct.pack(">I", len(data)) + data

DATA_FRAME = 0x00
TRAILER_FRAME = 0x02

class MockCursorBackend:
    def __init__(self, host: str = "127.0.0.1", port: int = 8089):
        self.host = host
        self.port = port
        self.app = web.Application()
        self.app.router.add_post("/aiserver.v1.AiService/StreamChat", self.handle_stream_chat)
        self.app.router.add_post("/aiserver.v1.AiService/StreamComposer", self.handle_stream_chat)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_post("/set_mode", self.handle_set_mode)
        self.app.router.add_get("/stats", self.handle_stats)
        
        # State
        self.current_mode = "inception_429"  # "inception_429", "midstream_429", "midstream_abrupt", "coalesced_trailer", "normal_200"
        self.request_history = []
        self.token_a = "EXHAUSTED_TOKEN_A"
        self.exhausted_tokens = set()
        self.cascade_remaining = 0

    async def handle_health(self, request):
        return web.Response(text="OK", status=200)

    async def handle_set_mode(self, request):
        data = await request.json()
        if "mode" in data:
            self.current_mode = data["mode"]
        if "token_a" in data:
            self.token_a = data["token_a"]
        if "exhausted_tokens" in data:
            self.exhausted_tokens = set(data["exhausted_tokens"])
        if "cascade_remaining" in data:
            self.cascade_remaining = int(data["cascade_remaining"])
        return web.json_response({
            "status": "updated",
            "mode": self.current_mode,
            "token_a": self.token_a,
            "exhausted_tokens": list(self.exhausted_tokens),
            "cascade_remaining": self.cascade_remaining
        })

    async def handle_stats(self, request):
        return web.json_response({
            "mode": self.current_mode,
            "requests_count": len(self.request_history),
            "history": self.request_history[-20:]
        })

    async def handle_stream_chat(self, request: web.Request):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip()
        body = await request.read()
        
        self.request_history.append({
            "token": token,
            "time": asyncio.get_event_loop().time(),
            "content_type": request.headers.get("Content-Type", ""),
            "length": len(body),
            "query_string": request.query_string,
            "headers": dict(request.headers)
        })

        is_account_exhausted = (
            token == self.token_a or 
            "exhausted" in token.lower() or 
            token in self.exhausted_tokens or
            self.cascade_remaining > 0
        )

        if self.cascade_remaining > 0:
            self.cascade_remaining -= 1

        # Case 1: Account hitting 429 at Inception
        if is_account_exhausted and self.current_mode in ("inception_429", "cascade_429"):
            error_body = {
                "code": "resource_exhausted",
                "message": f"Monthly usage quota exceeded for token '{token[:15]}...'. Please upgrade or wait for quota reset."
            }
            return web.Response(
                status=429,
                content_type="application/json",
                text=json.dumps(error_body)
            )

        # Case 2: Account hitting 429 Mid-Stream (Tokens stream then error trailer)
        if is_account_exhausted and self.current_mode == "midstream_429":
            response = web.StreamResponse(
                status=200,
                reason="OK",
                headers={
                    "Content-Type": "application/connect+json",
                    "Connect-Protocol-Version": "1"
                }
            )
            await response.prepare(request)
            
            # Stream 5 initial tokens
            partial_tokens = ["Analyzing ", "project ", "files ", "and ", "context... "]
            for tok in partial_tokens:
                frame = make_connect_frame({"text": tok}, flag=DATA_FRAME)
                await response.write(frame)
                await asyncio.sleep(0.01) # simulate 10ms per token
                
            # Send Connect-RPC error trailer (flag 0x02)
            error_trailer = make_connect_frame({
                "error": {
                    "code": "resource_exhausted",
                    "message": "Quota limit reached mid-stream."
                }
            }, flag=TRAILER_FRAME)
            await response.write(error_trailer)
            await response.write_eof()
            return response

        # Case 3: Coalesced chunk simulation (DATA frame + TRAILER frame together in single TCP write)
        if self.current_mode == "coalesced_trailer":
            response = web.StreamResponse(
                status=200,
                reason="OK",
                headers={
                    "Content-Type": "application/connect+json",
                    "Connect-Protocol-Version": "1"
                }
            )
            await response.prepare(request)
            
            f1 = make_connect_frame({"text": "Frame one data. "}, flag=DATA_FRAME)
            f2 = make_connect_frame({"error": {"code": "resource_exhausted", "message": "Coalesced error trailer"}}, flag=TRAILER_FRAME)
            # Write both frames combined in a SINGLE chunk!
            await response.write(f1 + f2)
            await response.write_eof()
            return response

        # Case 4: Account hitting abrupt socket drop mid-stream
        if is_account_exhausted and self.current_mode == "midstream_abrupt":
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "application/connect+json"}
            )
            await response.prepare(request)
            for tok in ["Thinking ", "deeply... "]:
                frame = make_connect_frame({"text": tok}, flag=DATA_FRAME)
                await response.write(frame)
                await asyncio.sleep(0.01)
            # Abruptly close connection without EOF
            request.transport.close()
            return response

        # Case 5: Successful generation (Account B or valid token)
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "application/connect+json",
                "Connect-Protocol-Version": "1"
            }
        )
        await response.prepare(request)
        
        full_tokens = [
            "Hello! ", "I ", "am ", "Claude-3.5-Sonnet ",
            "streaming ", "via ", "Account ", "B. ",
            "The ", "transparent ", "proxy ", "swap ",
            "was ", "100% ", "seamless! "
        ]
        
        try:
            for tok in full_tokens:
                frame = make_connect_frame({"text": tok}, flag=DATA_FRAME)
                await response.write(frame)
                await asyncio.sleep(0.005) # simulate 5ms token generation
                
            # Normal End-of-Stream trailer frame
            eos_frame = make_connect_frame({}, flag=TRAILER_FRAME)
            await response.write(eos_frame)
            await response.write_eof()
        except Exception:
            pass
        return response

if __name__ == "__main__":
    server = MockCursorBackend()
    print(f"Starting Mock Cursor Backend on {server.host}:{server.port}...")
    web.run_app(server.app, host=server.host, port=server.port)
