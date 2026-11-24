"""
High-Performance In-Memory & SQLite Token Pool Manager for Cursor AI (v3.0)
===========================================================================
Maintains healthy ready Cursor accounts in pure RAM for sub-0.05ms proactive swaps,
with transparent `enc:v1:` LRU decryption, Two-Tier priority ordering (Tier 1 <50%
before Tier 2 50-99%), cooldown awareness, and non-blocking SQLite persistence
to cursor_accounts.db when an account is locked or exhausted.
"""

import os
import json
import time
import sqlite3
import threading
from typing import Optional, Dict, Any, List, Set, Union, Callable

from app_paths import get_db_path
from token_cipher import decrypt_token, compute_token_hash
from chat_lock_detector import QUOTA_EXHAUSTION_THRESHOLD, QUOTA_WARNING_THRESHOLD


class TokenPoolManager:
    """
    Manages accounts for transparent proxy rotation.
    Combines sub-0.05ms in-memory cache lookups with background SQLite persistence.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        on_token_exhausted: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        if db_path is None:
            self.db_path = get_db_path()
        else:
            self.db_path = os.path.abspath(db_path)

        self.on_token_exhausted = on_token_exhausted
        self.lock = threading.RLock()
        self._memory_cache: List[Dict[str, Any]] = []
        self._rate_limited_tokens: Set[str] = set()
        self._token_map: Dict[str, Dict[str, Any]] = {}
        self._current_index = 0
        self.load_cache_from_db()

    def _hydrate_row(self, row: sqlite3.Row) -> Optional[Dict[str, Any]]:
        """Decrypt access_token/refresh_token and parse optional hardware_fingerprint."""
        d = dict(row)
        raw_tok = d.get("access_token")
        plain_tok = decrypt_token(raw_tok) if raw_tok else None
        if not plain_tok or len(plain_tok) <= 20:
            return None
        d["access_token"] = plain_tok
        if d.get("refresh_token"):
            d["refresh_token"] = decrypt_token(d["refresh_token"])
        fp_raw = d.get("hardware_fingerprint")
        if isinstance(fp_raw, str) and fp_raw.strip():
            try:
                d["fingerprint"] = json.loads(fp_raw)
            except Exception:
                d["fingerprint"] = None
        else:
            d["fingerprint"] = fp_raw if isinstance(fp_raw, dict) else None
        return d

    def load_cache_from_db(self) -> int:
        """Loads healthy accounts from SQLite into RAM, decrypting `enc:v1:` tokens via LRU cache."""
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

                cur.execute("PRAGMA table_info(accounts);")
                cols = {r[1] for r in cur.fetchall()}

                select_cols = ["id", "email", "access_token", "usage_percent", "status"]
                for opt in ("total_spend", "refresh_token", "display_name", "auth_id", "cooldown_until", "hardware_fingerprint"):
                    if opt in cols:
                        select_cols.append(opt)

                now_ts = int(time.time())
                where_parts = [
                    "status IN ('READY', 'HIGH_USAGE')",
                    "(usage_percent IS NULL OR usage_percent < ?)",
                    "access_token IS NOT NULL",
                    "length(access_token) > 20",
                ]
                params: List[Any] = [QUOTA_EXHAUSTION_THRESHOLD]
                if "cooldown_until" in cols:
                    where_parts.append("(cooldown_until IS NULL OR cooldown_until <= ?)")
                    params.append(now_ts)
                if "token_type" in cols:
                    where_parts.append("(token_type IS NULL OR token_type != 'web')")

                sql = f"""
                    SELECT {', '.join(select_cols)}
                    FROM accounts
                    WHERE {' AND '.join(where_parts)}
                    ORDER BY
                        CASE WHEN COALESCE(usage_percent, 0.0) < ? THEN 0 ELSE 1 END ASC,
                        COALESCE(usage_percent, 0.0) ASC,
                        id ASC
                """
                params.append(QUOTA_WARNING_THRESHOLD)
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()

                hydrated = []
                for r in rows:
                    item = self._hydrate_row(r)
                    if item:
                        hydrated.append(item)

                self._memory_cache = hydrated
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
        """Direct SQLite query for next best healthy account, decrypting tokens transparently."""
        excludes = self._normalize_excludes(exclude_tokens)
        t0 = time.perf_counter_ns()
        con = None
        try:
            con = sqlite3.connect(self.db_path, timeout=5.0)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("PRAGMA table_info(accounts);")
            cols = {r[1] for r in cur.fetchall()}

            select_cols = ["id", "email", "access_token", "usage_percent", "status"]
            for opt in ("total_spend", "refresh_token", "display_name", "auth_id", "cooldown_until", "hardware_fingerprint"):
                if opt in cols:
                    select_cols.append(opt)

            now_ts = int(time.time())
            where_parts = [
                "status IN ('READY', 'HIGH_USAGE')",
                "(usage_percent IS NULL OR usage_percent < ?)",
                "access_token IS NOT NULL",
                "length(access_token) > 20",
            ]
            params: List[Any] = [QUOTA_EXHAUSTION_THRESHOLD]
            if "cooldown_until" in cols:
                where_parts.append("(cooldown_until IS NULL OR cooldown_until <= ?)")
                params.append(now_ts)
            if "token_type" in cols:
                where_parts.append("(token_type IS NULL OR token_type != 'web')")

            sql = f"""
                SELECT {', '.join(select_cols)}
                FROM accounts
                WHERE {' AND '.join(where_parts)}
                ORDER BY
                    CASE WHEN COALESCE(usage_percent, 0.0) < ? THEN 0 ELSE 1 END ASC,
                    COALESCE(usage_percent, 0.0) ASC,
                    id ASC
            """
            params.append(QUOTA_WARNING_THRESHOLD)
            cur.execute(sql, tuple(params))

            for row in cur.fetchall():
                item = self._hydrate_row(row)
                if not item:
                    continue
                if item["access_token"] in excludes or item["access_token"] in self._rate_limited_tokens:
                    continue
                t1 = time.perf_counter_ns()
                item["query_time_ns"] = t1 - t0
                item["query_time_ms"] = (t1 - t0) / 1_000_000.0
                return item
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
        """In-memory pool query with fair round-robin and Tier 1 priority (< 0.05ms)."""
        excludes = self._normalize_excludes(exclude_tokens)
        t0 = time.perf_counter_ns()
        with self.lock:
            if not self._memory_cache:
                self.load_cache_from_db()

            n = len(self._memory_cache)
            if n == 0:
                return None

            fallback_hit = None
            fallback_idx = 0

            # Single-pass round-robin: immediately pick Tier 1 (<50%), retain first Tier 2 as fallback
            for i in range(n):
                idx = (self._current_index + i) % n
                acc = self._memory_cache[idx]
                token = acc["access_token"]
                usage = acc.get("usage_percent")
                if (
                    acc.get("status") in ("READY", "HIGH_USAGE")
                    and (usage is None or usage < QUOTA_EXHAUSTION_THRESHOLD)
                    and token not in excludes
                    and token not in self._rate_limited_tokens
                ):
                    if usage is None or usage < QUOTA_WARNING_THRESHOLD:
                        self._current_index = (idx + 1) % n
                        t1 = time.perf_counter_ns()
                        res = acc.copy()
                        res["query_time_ns"] = t1 - t0
                        res["query_time_ms"] = (t1 - t0) / 1_000_000.0
                        return res
                    elif fallback_hit is None:
                        fallback_hit = acc
                        fallback_idx = idx

            if fallback_hit is not None:
                self._current_index = (fallback_idx + 1) % n
                t1 = time.perf_counter_ns()
                res = fallback_hit.copy()
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
        then updates SQLite cursor_accounts.db by account ID / token_hash / access_token
        along with dynamic cooldown duration.
        """
        if not token:
            return

        matched_id: Optional[int] = None
        matched_email: Optional[str] = None
        with self.lock:
            self._rate_limited_tokens.add(token)
            acc = self._token_map.get(token)
            if acc:
                matched_id = acc.get("id")
                matched_email = acc.get("email")
                acc["status"] = "EXHAUSTED"
                acc["usage_percent"] = 100.0
                acc["total_spend"] = max(float(acc.get("total_spend") or 0.0), 100.0)
            for item in self._memory_cache:
                if item["access_token"] == token:
                    matched_id = matched_id or item.get("id")
                    matched_email = matched_email or item.get("email")
                    item["status"] = "EXHAUSTED"
                    item["usage_percent"] = 100.0
                    item["total_spend"] = max(float(item.get("total_spend") or 0.0), 100.0)
                    break

        tok_hash = compute_token_hash(token)
        try:
            from ai_optimizer import DynamicCooldownManager
            cd_seconds = DynamicCooldownManager.calculate_cooldown(reason or "")
        except Exception:
            cd_seconds = 7 * 86400
        now_ts = int(time.time())
        cd_until = now_ts + cd_seconds

        def _persist_to_db():
            con = None
            try:
                con = sqlite3.connect(self.db_path, timeout=10.0)
                cur = con.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL;")
                except Exception:
                    pass
                cur.execute("PRAGMA table_info(accounts);")
                cols = {r[1] for r in cur.fetchall()}

                set_parts = ["status = 'EXHAUSTED'", "usage_percent = 100.0"]
                set_params: List[Any] = []
                if "total_spend" in cols:
                    set_parts.append("total_spend = MAX(COALESCE(total_spend, 0.0), 100.0)")
                if "display_message" in cols:
                    set_parts.append("display_message = ?")
                    set_params.append(reason)
                if "last_checked" in cols:
                    set_parts.append("last_checked = ?")
                    set_params.append(now_ts)
                if "cooldown_until" in cols:
                    set_parts.append("cooldown_until = ?")
                    set_params.append(cd_until)

                if matched_id is not None:
                    where_sql = "id = ?"
                    where_params: List[Any] = [matched_id]
                elif "token_hash" in cols and tok_hash:
                    where_sql = "token_hash = ? OR access_token = ?"
                    where_params = [tok_hash, token]
                else:
                    where_sql = "access_token = ?"
                    where_params = [token]

                cur.execute(
                    f"UPDATE accounts SET {', '.join(set_parts)} WHERE {where_sql}",
                    tuple(set_params + where_params),
                )
                con.commit()
            except Exception as e:
                print(f"[-] Error updating EXHAUSTED status in DB: {e}")
            finally:
                if con:
                    try:
                        con.close()
                    except Exception:
                        pass

            if self.on_token_exhausted:
                try:
                    self.on_token_exhausted({
                        "account_id": matched_id,
                        "email": matched_email,
                        "reason": reason,
                        "cooldown_until": cd_until,
                    })
                except Exception:
                    pass

        if async_db:
            threading.Thread(target=_persist_to_db, daemon=True).start()
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
                "threshold": QUOTA_EXHAUSTION_THRESHOLD,
            }


if __name__ == "__main__":
    pool = TokenPoolManager()
    stats = pool.get_stats()
    print(f"Token pool initialized: {stats['cached_accounts']} accounts loaded ({stats['ready_count']} ready).")
    cache_res = pool.get_token_from_cache()
    if cache_res:
        print(f"In-memory cached lookup: {cache_res['query_time_ms']:.3f} ms (Account: {cache_res['email']})")
