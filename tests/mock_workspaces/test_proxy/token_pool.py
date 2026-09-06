import sqlite3
import time
import os
import threading
from typing import Optional, Dict, Any, List, Set, Union

QUOTA_EXHAUSTION_THRESHOLD = float(os.getenv("CURSOR_QUOTA_THRESHOLD", "100.0"))

class TokenPoolManager:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            self.db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cursor_accounts.db"))
        else:
            self.db_path = os.path.abspath(db_path)
            
        self.lock = threading.RLock()
        self._memory_cache: List[Dict[str, Any]] = []
        self._rate_limited_tokens: Set[str] = set()
        self._token_map: Dict[str, Dict[str, Any]] = {}
        self._current_index = 0
        self.load_cache_from_db()

    def load_cache_from_db(self) -> int:
        """Loads ready accounts from SQLite into memory for sub-millisecond lookups."""
        if not os.path.exists(self.db_path):
            return 0
        with self.lock:
            con = sqlite3.connect(self.db_path, timeout=10.0)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("""
                SELECT id, email, access_token, usage_percent, status
                FROM accounts
                WHERE status IN ('READY', 'HIGH_USAGE') AND usage_percent < ? AND access_token IS NOT NULL AND length(access_token) > 20
                ORDER BY usage_percent ASC, id ASC
            """, (QUOTA_EXHAUSTION_THRESHOLD,))
            rows = cur.fetchall()
            self._memory_cache = [dict(r) for r in rows]
            self._token_map = {acc["access_token"]: acc for acc in self._memory_cache}
            self._current_index = 0
            con.close()
            return len(self._memory_cache)

    def _normalize_excludes(self, exclude_tokens: Optional[Union[str, Set[str], List[str]]]) -> Set[str]:
        if exclude_tokens is None:
            return set()
        if isinstance(exclude_tokens, str):
            return {exclude_tokens}
        return set(exclude_tokens)

    def get_token_from_db_direct(self, exclude_tokens: Optional[Union[str, Set[str], List[str]]] = None) -> Optional[Dict[str, Any]]:
        """Direct SQLite query for next best READY account. Measures raw DB query time."""
        excludes = self._normalize_excludes(exclude_tokens)
        t0 = time.perf_counter_ns()
        con = sqlite3.connect(self.db_path, timeout=5.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        
        if excludes:
            placeholders = ",".join("?" for _ in excludes)
            cur.execute(f"""
                SELECT id, email, access_token, usage_percent, status
                FROM accounts
                WHERE status IN ('READY', 'HIGH_USAGE') AND usage_percent < ? AND access_token NOT IN ({placeholders}) AND length(access_token) > 20
                ORDER BY usage_percent ASC, id ASC
                LIMIT 1
            """, (QUOTA_EXHAUSTION_THRESHOLD, *excludes))
        else:
            cur.execute("""
                SELECT id, email, access_token, usage_percent, status
                FROM accounts
                WHERE status IN ('READY', 'HIGH_USAGE') AND usage_percent < ? AND length(access_token) > 20
                ORDER BY usage_percent ASC, id ASC
                LIMIT 1
            """, (QUOTA_EXHAUSTION_THRESHOLD,))
            
        row = cur.fetchone()
        con.close()
        t1 = time.perf_counter_ns()
        
        if row:
            res = dict(row)
            res["query_time_ns"] = t1 - t0
            res["query_time_ms"] = (t1 - t0) / 1_000_000.0
            return res
        return None

    def get_token_from_cache(self, exclude_tokens: Optional[Union[str, Set[str], List[str]]] = None) -> Optional[Dict[str, Any]]:
        """In-memory pool query with round-robin fair distribution and multi-token exclusion."""
        excludes = self._normalize_excludes(exclude_tokens)
        t0 = time.perf_counter_ns()
        with self.lock:
            if not self._memory_cache:
                self.load_cache_from_db()
                
            n = len(self._memory_cache)
            if n == 0:
                return None
                
            # Fair round-robin search starting from self._current_index
            for i in range(n):
                idx = (self._current_index + i) % n
                acc = self._memory_cache[idx]
                token = acc["access_token"]
                if acc["status"] == "READY" and token not in excludes and token not in self._rate_limited_tokens:
                    self._current_index = (idx + 1) % n
                    t1 = time.perf_counter_ns()
                    res = acc.copy()
                    res["query_time_ns"] = t1 - t0
                    res["query_time_ms"] = (t1 - t0) / 1_000_000.0
                    return res
        t1 = time.perf_counter_ns()
        return None

    def is_token_rate_limited(self, token: str) -> bool:
        """O(1) check if a token is known to be exhausted."""
        with self.lock:
            if token in self._rate_limited_tokens:
                return True
            acc = self._token_map.get(token)
            if acc and acc.get("status") == "RATE_LIMITED":
                return True
        return False

    def get_rate_limited_tokens(self) -> Set[str]:
        with self.lock:
            return set(self._rate_limited_tokens)

    def mark_rate_limited(self, token: str, reason: str = "429 Rate Limit", async_db: bool = True):
        """Marks account as RATE_LIMITED instantly in memory, then persists to SQLite asynchronously."""
        with self.lock:
            self._rate_limited_tokens.add(token)
            if token in self._token_map:
                self._token_map[token]["status"] = "RATE_LIMITED"
                self._token_map[token]["usage_percent"] = 100.0
            for acc in self._memory_cache:
                if acc["access_token"] == token:
                    acc["status"] = "RATE_LIMITED"
                    acc["usage_percent"] = 100.0
                    break
                    
        def _persist_to_db():
            try:
                con = sqlite3.connect(self.db_path, timeout=5.0)
                cur = con.cursor()
                cur.execute("""
                    UPDATE accounts
                    SET status = 'RATE_LIMITED', display_message = ?, usage_percent = 100.0
                    WHERE access_token = ?
                """, (reason, token))
                con.commit()
                con.close()
            except Exception as e:
                print(f"[-] Error updating rate limited status in DB: {e}")

        if async_db:
            # Asynchronous background write avoids blocking the event loop or delaying HTTP requests
            t = threading.Thread(target=_persist_to_db, daemon=True)
            t.start()
        else:
            _persist_to_db()

    def reset_rate_limits(self):
        """Clears in-memory rate limited state."""
        with self.lock:
            self._rate_limited_tokens.clear()
            self.load_cache_from_db()

    def get_stats(self) -> Dict[str, Any]:
        with self.lock:
            ready_count = sum(
                1 for a in self._memory_cache 
                if a["status"] == "READY" and a["access_token"] not in self._rate_limited_tokens
            )
            return {
                "cached_accounts": len(self._memory_cache),
                "ready_count": ready_count,
                "rate_limited_count": len(self._rate_limited_tokens)
            }

if __name__ == "__main__":
    pool = TokenPoolManager()
    stats = pool.get_stats()
    print(f"Token pool initialized: {stats['cached_accounts']} accounts loaded ({stats['ready_count']} ready).")
    db_res = pool.get_token_from_db_direct()
    if db_res:
        print(f"Direct SQLite lookup: {db_res['query_time_ms']:.3f} ms (Account: {db_res['email']})")
    cache_res = pool.get_token_from_cache()
    if cache_res:
        print(f"In-memory cached lookup: {cache_res['query_time_ms']:.3f} ms (Account: {cache_res['email']})")

