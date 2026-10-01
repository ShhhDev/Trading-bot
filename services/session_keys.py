"""
session_keys.py
================
Holds DECRYPTED private keys in memory only, per (telegram_user_id, wallet_id),
for a short TTL after the user unlocks with their PIN. Never persisted.

This is what lets a user unlock once and then tap Buy/Sell repeatedly without
re-typing their PIN every time -- while keeping the plaintext key out of the
database entirely and out of memory once the TTL expires.

Process restart = cache is gone = user must unlock again. That's intentional.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock

DEFAULT_TTL_SECONDS = 15 * 60  # 15 min, configurable per user later


@dataclass
class _CachedKey:
    plaintext: str
    expires_at: float


class SessionKeyStore:
    def __init__(self) -> None:
        self._store: dict[tuple[int, int], _CachedKey] = {}
        self._lock = Lock()

    def put(self, user_id: int, wallet_id: int, plaintext: str, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        with self._lock:
            self._store[(user_id, wallet_id)] = _CachedKey(plaintext, time.time() + ttl)

    def get(self, user_id: int, wallet_id: int) -> str | None:
        with self._lock:
            entry = self._store.get((user_id, wallet_id))
            if entry is None:
                return None
            if entry.expires_at < time.time():
                del self._store[(user_id, wallet_id)]
                return None
            return entry.plaintext

    def revoke(self, user_id: int, wallet_id: int) -> None:
        with self._lock:
            self._store.pop((user_id, wallet_id), None)

    def revoke_all_for_user(self, user_id: int) -> None:
        with self._lock:
            keys_to_remove = [k for k in self._store if k[0] == user_id]
            for k in keys_to_remove:
                del self._store[k]

    def sweep_expired(self) -> int:
        """Call periodically from a scheduler job. Returns count removed."""
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._store.items() if v.expires_at < now]
            for k in expired:
                del self._store[k]
            return len(expired)


# Process-wide singleton. In a multi-worker deployment this must instead be
# something like an in-memory-only Redis instance with no RDB/AOF persistence
# (still never a durable store), scoped so keys never leave a trusted host.
session_keys = SessionKeyStore()
