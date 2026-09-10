"""
Minimal circuit breaker guarding Redis access.

Without this, every request under a Redis outage independently attempts a
fresh connection (DNS lookup + TCP connect) and only falls back after that
attempt fails. Under concurrency, those failing lookups pile up on
asyncio's default executor thread pool (used for the blocking
socket.getaddrinfo() call), which has a small bounded size -- most
lookups fail fast, but whichever ones land behind a backlog can take
several seconds, occasionally long enough for an upstream timeout (e.g.
nginx's proxy_read_timeout) to cut the request off entirely. A
`socket_connect_timeout` on the Redis client does NOT help here: DNS
resolution happens before a socket exists, so no socket-level timeout
bounds it.

The breaker fixes this by remembering "Redis was unreachable" for a short
cooldown window: once open, callers skip the connection attempt entirely
and go straight to their fallback path, instead of re-discovering the
same outage on every single request. After the cooldown, the next caller
gets to try again (closing the breaker on success, or re-opening it on
failure) -- this is what lets the system detect recovery automatically.

Second problem this also fixes: redis-py's connection pool does not
always self-heal after a sustained outage. A long-lived client whose
pooled connections broke while Redis was down can stay wedged (retrying
against stale internal connection state) even after Redis is verifiably
back -- a brand new client created at that point connects fine, but the
existing one doesn't, until something forces it to drop its pool and
build fresh connections. So record_failure() also (best-effort) tears
down the connection pool, ensuring the next attempt after the cooldown
starts from a clean slate instead of potentially retrying the same wedged
connections forever.
"""

import time


class RedisCircuitBreaker:
    def __init__(self, cooldown_seconds: float = 2.0):
        self.cooldown_seconds = cooldown_seconds
        self._open_until = 0.0

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    async def record_failure(self, redis_client=None) -> None:
        self._open_until = time.monotonic() + self.cooldown_seconds
        if redis_client is not None:
            try:
                await redis_client.connection_pool.disconnect(inuse_connections=True)
            except Exception:
                pass  # best-effort; a failure here doesn't affect the fallback decision

    def record_success(self) -> None:
        self._open_until = 0.0
