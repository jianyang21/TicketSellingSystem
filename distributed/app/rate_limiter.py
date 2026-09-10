"""
Hybrid rate limiter: token bucket (admission) + leaky bucket (shaping),
combined into a single atomic Redis Lua script.

Why hybrid, and why in that order:
    - Token bucket answers "is this request allowed in AT ALL right now."
      It refills continuously and has capacity for a burst, so a sudden
      spike of legitimate traffic isn't punished just for being bursty.
    - Leaky bucket answers "is downstream ready for another one." It
      drains at a fixed constant rate regardless of how bursty the input
      is, so whatever gets past the token bucket still arrives at the
      queue/database at a smooth, sustainable pace instead of in spikes.
    A request must clear BOTH gates. Order matters: we only spend a token
    after confirming the leaky bucket has room, so a request rejected by
    the leaky bucket doesn't waste a token it could have used on a retry.

Why one Lua script instead of two Redis round trips:
    Redis executes a Lua script as a single atomic operation -- no other
    client's commands can interleave with it. Two separate round trips
    (read tokens, decide, write tokens) would reintroduce exactly the
    check-then-act race this whole project has been about, just inside
    the rate limiter itself instead of the ticket counter.

Resilience: if Redis is unreachable, `allow()` falls back to a local,
in-process hybrid bucket (guarded by an asyncio.Lock) implementing the
identical algorithm. That fallback is per-process, not global -- with N
app replicas each enforcing its own local limit, the effective admitted
rate under a Redis outage is roughly N times the configured rate. That's
a deliberate, documented degradation (some rate limiting beats none),
not a silent correctness gap.
"""

import time
from asyncio import Lock as AsyncLock
from typing import Optional

import redis.asyncio as aioredis
import redis.exceptions

from circuit_breaker import RedisCircuitBreaker

HYBRID_LIMITER_LUA = """
local tb_key = KEYS[1]
local lb_key = KEYS[2]
local now = tonumber(ARGV[1])
local token_capacity = tonumber(ARGV[2])
local token_refill_rate = tonumber(ARGV[3])
local leaky_capacity = tonumber(ARGV[4])
local leaky_leak_rate = tonumber(ARGV[5])

local tb = redis.call('HMGET', tb_key, 'tokens', 'ts')
local tokens = tonumber(tb[1])
local tb_ts = tonumber(tb[2])
if tokens == nil then
    tokens = token_capacity
    tb_ts = now
end
local elapsed_tb = math.max(0, now - tb_ts)
tokens = math.min(token_capacity, tokens + elapsed_tb * token_refill_rate)

if tokens < 1 then
    redis.call('HMSET', tb_key, 'tokens', tokens, 'ts', now)
    redis.call('EXPIRE', tb_key, 300)
    return {0, 'token_bucket_exhausted'}
end

local lb = redis.call('HMGET', lb_key, 'level', 'ts')
local level = tonumber(lb[1])
local lb_ts = tonumber(lb[2])
if level == nil then
    level = 0
    lb_ts = now
end
local elapsed_lb = math.max(0, now - lb_ts)
level = math.max(0, level - elapsed_lb * leaky_leak_rate)

if level >= leaky_capacity then
    -- Rejected by the leaky bucket: persist its decayed level, but do NOT
    -- spend the token we tentatively had available.
    redis.call('HMSET', tb_key, 'tokens', tokens, 'ts', now)
    redis.call('EXPIRE', tb_key, 300)
    redis.call('HMSET', lb_key, 'level', level, 'ts', now)
    redis.call('EXPIRE', lb_key, 300)
    return {0, 'leaky_bucket_full'}
end

tokens = tokens - 1
level = level + 1
redis.call('HMSET', tb_key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', tb_key, 300)
redis.call('HMSET', lb_key, 'level', level, 'ts', now)
redis.call('EXPIRE', lb_key, 300)
return {1, 'ok'}
"""


class HybridRateLimiter:
    def __init__(
        self,
        redis_client: aioredis.Redis,
        token_capacity: float = 50,
        token_refill_rate: float = 30,
        leaky_capacity: float = 300,
        leaky_leak_rate: float = 50,
        circuit: Optional[RedisCircuitBreaker] = None,
    ):
        self.redis = redis_client
        self.circuit = circuit
        self.token_capacity = token_capacity
        self.token_refill_rate = token_refill_rate
        self.leaky_capacity = leaky_capacity
        self.leaky_leak_rate = leaky_leak_rate
        self._script = self.redis.register_script(HYBRID_LIMITER_LUA)

        # Local fallback state (used only when Redis is unreachable).
        self._local_lock = AsyncLock()
        self._local_tokens = token_capacity
        self._local_level = 0.0
        self._local_ts = time.monotonic()

    async def allow(self, key: str = "global") -> tuple[bool, str]:
        if self.circuit is not None and self.circuit.is_open():
            # Known-down: skip the connection attempt entirely rather than
            # rediscovering the same outage (and its DNS-lookup cost) on
            # every request. See circuit_breaker.py.
            return await self._allow_local_fallback()

        now = time.time()
        try:
            result = await self._script(
                keys=[f"ratelimit:tb:{key}", f"ratelimit:lb:{key}"],
                args=[
                    now,
                    self.token_capacity,
                    self.token_refill_rate,
                    self.leaky_capacity,
                    self.leaky_leak_rate,
                ],
            )
            if self.circuit is not None:
                self.circuit.record_success()
            return bool(result[0]), result[1]
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, OSError):
            # OSError also catches socket.gaierror (DNS lookup failure for
            # the Redis hostname), which redis-py does not wrap into its
            # own exception hierarchy.
            if self.circuit is not None:
                await self.circuit.record_failure(self.redis)
            return await self._allow_local_fallback()

    async def _allow_local_fallback(self) -> tuple[bool, str]:
        async with self._local_lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self._local_ts)
            self._local_ts = now

            self._local_tokens = min(
                self.token_capacity, self._local_tokens + elapsed * self.token_refill_rate
            )
            if self._local_tokens < 1:
                return False, "token_bucket_exhausted_local_fallback"

            self._local_level = max(0.0, self._local_level - elapsed * self.leaky_leak_rate)
            if self._local_level >= self.leaky_capacity:
                return False, "leaky_bucket_full_local_fallback"

            self._local_tokens -= 1
            self._local_level += 1
            return True, "ok_local_fallback"
