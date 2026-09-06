"""
High-Performance In-Memory & SQLite Token Pool Manager for Cursor AI
====================================================================

Maintains healthy (< 50% usage) ready Cursor accounts in pure RAM for
sub-0.05ms proactive swaps, with non-blocking asynchronous SQLite persistence
to cursor_accounts.db when an account is locked or exhausted.
"""

import os
import time
import sqlite3
import threading
from typing import Optional, Dict, Any, List, Set, Union
from chat_lock_detector import QUOTA_EXHAUSTION_THRESHOLD

class TokenPoolManager:
    """
    Manages accounts for transparent rotation.
    Combines sub-0.05ms in-memory cache lookups with background SQLite persistence.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            self.db_path = os.getenv("CURSOR_ACCOUNTS_DB", os.path.abspath(os.path.join(root_dir, "cursor_accounts.db")))
        else:
            self.db_path = os.path.abspath(db_path)

        self.lock = threading.RLock()
        self._memory_cache: List[Dict[str, Any]] = []
        self._rate_limited_tokens: Set[str] = set()
        self._token_map: Dict[str, Dict[str, Any]] = {}
        self._current_index = 0
        self.load_cache_from_db()

    def load_cache_from_db(self) -> int:
        """Loads ready accounts (< 50% usage) from SQLite into memory for sub-millisecond lookups."""
        if not os.path.exists(self.db_path):
            return 0
        with self.lock:
            con = None
            try:
                con = sqlite3.connect(self.db_path, timeout=10.0)
                con.row_factory = sqlite3.Row
                cur = con.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL;")
                except Exception:
                    pass
                try:
                    cur.execute("""
                        SELECT id, email, access_token, usage_percent, total_spend, status
                        FROM accounts
                        WHERE status IN ('READY', 'HIGH_USAGE') 
                          AND (usage_percent IS NULL OR usage_percent < ?) 
                          AND access_token IS NOT NULL 
                          AND length(access_token) > 20
                        ORDER BY usage_percent ASC, id ASC
                    """, (QUOTA_EXHAUSTION_THRESHOLD,))
                except sqlite3.OperationalError:
                    # Fallback if total_spend column is not present in legacy schema
                    cur.execute("""
                        SELECT id, email, access_token, usage_percent, status
                        FROM accounts
                        WHERE status IN ('READY', 'HIGH_USAGE') 
                          AND (usage_percent IS NULL OR usage_percent < ?) 
                          AND access_token IS NOT NULL 
                          AND length(access_token) > 20
                        ORDER BY usage_percent ASC, id ASC
                    """, (QUOTA_EXHAUSTION_THRESHOLD,))

                rows = cur.fetchall()
                self._memory_cache = [dict(r) for r in rows]
                self._token_map = {acc["access_token"]: acc for acc in self._memory_cache}
                self._current_index = 0
                return len(self._memory_cache)
            except Exception as e:
                print(f"[-] Error loading cache from DB: {e}")
                return 0
            finally:
                if con:
                    try:
                        con.close()
                    except Exception:
                        pass

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
        con = None
        try:
            con = sqlite3.connect(self.db_path, timeout=5.0)
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            try:
                if excludes:
                    placeholders = ",".join("?" for _ in excludes)
                    cur.execute(f"""
                        SELECT id, email, access_token, usage_percent, total_spend, status
                        FROM accounts
                        WHERE status IN ('READY', 'HIGH_USAGE') 
                          AND (usage_percent IS NULL OR usage_percent < ?) 
                          AND access_token NOT IN ({placeholders}) 
                          AND length(access_token) > 20
                        ORDER BY usage_percent ASC, id ASC
                        LIMIT 1
                    """, (QUOTA_EXHAUSTION_THRESHOLD, *excludes))
                else:
                    cur.execute("""
                        SELECT id, email, access_token, usage_percent, total_spend, status
                        FROM accounts
                        WHERE status IN ('READY', 'HIGH_USAGE') 
                          AND (usage_percent IS NULL OR usage_percent < ?) 
                          AND length(access_token) > 20
                        ORDER BY usage_percent ASC, id ASC
                        LIMIT 1
                    """, (QUOTA_EXHAUSTION_THRESHOLD,))
            except sqlite3.OperationalError:
                # Fallback for tables without total_spend column
                if excludes:
                    placeholders = ",".join("?" for _ in excludes)
                    cur.execute(f"""
                        SELECT id, email, access_token, usage_percent, status
                        FROM accounts
                        WHERE status IN ('READY', 'HIGH_USAGE') 
                          AND (usage_percent IS NULL OR usage_percent < ?) 
                          AND access_token NOT IN ({placeholders}) 
                          AND length(access_token) > 20
                        ORDER BY usage_percent ASC, id ASC
                        LIMIT 1
                    """, (QUOTA_EXHAUSTION_THRESHOLD, *excludes))
                else:
                    cur.execute("""
                        SELECT id, email, access_token, usage_percent, status
                        FROM accounts
                        WHERE status IN ('READY', 'HIGH_USAGE') 
                          AND (usage_percent IS NULL OR usage_percent < ?) 
                          AND length(access_token) > 20
                        ORDER BY usage_percent ASC, id ASC
                        LIMIT 1
                    """, (QUOTA_EXHAUSTION_THRESHOLD,))

            row = cur.fetchone()
            t1 = time.perf_counter_ns()

            if row:
                res = dict(row)
                res["query_time_ns"] = t1 - t0
                res["query_time_ms"] = (t1 - t0) / 1_000_000.0
                return res
        except Exception as e:
            print(f"[-] Direct DB lookup error: {e}")
        finally:
            if con:
                try:
                    con.close()
                except Exception:
                    pass

        return None

    def get_token_from_cache(self, exclude_tokens: Optional[Union[str, Set[str], List[str]]] = None) -> Optional[Dict[str, Any]]:
        """In-memory pool query with round-robin fair distribution and multi-token exclusion (< 0.05ms)."""
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
                if (
                    acc.get("status") in ("READY", "HIGH_USAGE")
                    and (acc.get("usage_percent") is None or acc.get("usage_percent", 0.0) < QUOTA_EXHAUSTION_THRESHOLD)
                    and token not in excludes 
                    and token not in self._rate_limited_tokens
                ):
                    self._current_index = (idx + 1) % n
                    t1 = time.perf_counter_ns()
                    res = acc.copy()
                    res["query_time_ns"] = t1 - t0
                    res["query_time_ms"] = (t1 - t0) / 1_000_000.0
                    return res

        return None

    def is_token_rate_limited(self, token: str) -> bool:
        """O(1) check if a token is known to be locked or exhausted."""
        if not token:
            return True
        with self.lock:
            if token in self._rate_limited_tokens:
                return True
            acc = self._token_map.get(token)
            if acc and acc.get("status") in ("EXHAUSTED", "RATE_LIMITED"):
                return True
        return False

    is_token_locked = is_token_rate_limited

    def get_rate_limited_tokens(self) -> Set[str]:
        with self.lock:
            return set(self._rate_limited_tokens)

    def mark_rate_limited(self, token: str, reason: str = "Chat Lockout / Quota Exhausted", async_db: bool = True):
        """
        Marks account as EXHAUSTED instantly in RAM (O(1)),
        then updates SQLite cursor_accounts.db asynchronously without blocking the event loop.
        """
        if not token:
            return

        with self.lock:
            self._rate_limited_tokens.add(token)
            if token in self._token_map:
                self._token_map[token]["status"] = "EXHAUSTED"
                self._token_map[token]["usage_percent"] = 100.0
                self._token_map[token]["total_spend"] = max(float(self._token_map[token].get("total_spend") or 0.0), 100.0)
            for acc in self._memory_cache:
                if acc["access_token"] == token:
                    acc["status"] = "EXHAUSTED"
                    acc["usage_percent"] = 100.0
                    acc["total_spend"] = max(float(acc.get("total_spend") or 0.0), 100.0)
                    break

        def _persist_to_db():
            con = None
            try:
                con = sqlite3.connect(self.db_path, timeout=10.0)
                cur = con.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL;")
                except Exception:
                    pass
                try:
                    cur.execute("""
                        UPDATE accounts
                        SET status = 'EXHAUSTED', display_message = ?, usage_percent = 100.0, total_spend = MAX(COALESCE(total_spend, 0.0), 100.0), last_checked = ?
                        WHERE access_token = ?
                    """, (reason, int(time.time()), token))
                except sqlite3.OperationalError:
                    try:
                        cur.execute("""
                            UPDATE accounts
                            SET status = 'EXHAUSTED', usage_percent = 100.0, total_spend = 100.0
                            WHERE access_token = ?
                        """, (token,))
                    except sqlite3.OperationalError:
                        cur.execute("""
                            UPDATE accounts
                            SET status = 'EXHAUSTED', usage_percent = 100.0
                            WHERE access_token = ?
                        """, (token,))
                con.commit()
            except Exception as e:
                print(f"[-] Error updating EXHAUSTED status in DB: {e}")
            finally:
                if con:
                    try:
                        con.close()
                    except Exception:
                        pass

        if async_db:
            t = threading.Thread(target=_persist_to_db, daemon=True)
            t.start()
        else:
            _persist_to_db()

    mark_locked = mark_rate_limited

    def reset_rate_limits(self):
        """Clears in-memory rate limited state and reloads cache from SQLite."""
        with self.lock:
            self._rate_limited_tokens.clear()
            self.load_cache_from_db()

    def get_stats(self) -> Dict[str, Any]:
        with self.lock:
            ready_count = sum(
                1 for a in self._memory_cache 
                if a.get("status") in ("READY", "HIGH_USAGE") 
                and (a.get("usage_percent") is None or a.get("usage_percent", 0.0) < QUOTA_EXHAUSTION_THRESHOLD)
                and a["access_token"] not in self._rate_limited_tokens
            )
            return {
                "cached_accounts": len(self._memory_cache),
                "ready_count": ready_count,
                "rate_limited_count": len(self._rate_limited_tokens),
                "threshold": QUOTA_EXHAUSTION_THRESHOLD
            }

if __name__ == "__main__":
    pool = TokenPoolManager()
    stats = pool.get_stats()
    print(f"Token pool initialized: {stats['cached_accounts']} accounts loaded ({stats['ready_count']} ready).")
    cache_res = pool.get_token_from_cache()
    if cache_res:
        print(f"In-memory cached lookup: {cache_res['query_time_ms']:.3f} ms (Account: {cache_res['email']})")
