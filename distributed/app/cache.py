"""
Redis-backed read cache for the ticket-status data.

Postgres remains the sole source of truth (see db.py). This cache exists
only to avoid re-querying Postgres -- a full scan of sold_tickets plus a
point read of tickets_state -- on every single GET /status call, which in
a real ticket sale is called far more often than tickets are actually
sold (clients poll "how many are left" constantly; the state only
changes on a successful /buy).

Pattern: cache-aside with invalidate-on-write, deliberately NOT patched
in place.
    - A read (get_cached_status) tries Redis first; on a hit, Postgres is
      never touched at all.
    - On a miss -- whether nothing has been cached yet, the safety-net
      TTL expired, or Redis lost its data entirely (restarted without
      AOF/RDB persistence, was flushed, or is a fresh replacement
      instance) -- it transparently falls back to Postgres, recomputes
      the authoritative status, and repopulates the cache before
      returning. This *is* the reconciliation path: there is no separate
      "repair Redis" step to run. A cache miss for any reason, including
      total data loss, self-heals on the very next read.
    - Every successful write (a ticket sale, or /reset) invalidates the
      cache instead of patching it. Patching a shared Redis value from
      multiple concurrent writers (multiple app replicas, multiple
      uvicorn workers per replica) without a cross-process lock around
      the read-modify-write cycle can silently lose updates; invalidation
      sidesteps that entirely, at the cost of one extra Postgres read on
      the next GET /status after a write. Since writes (sales) are rare
      relative to reads (status polling), that trade is the whole point.

Resilience: every Redis call here is best-effort (bare try/except). If
Redis is down, a read just falls through to Postgres -- which is correct
regardless, Postgres is the ground truth -- and an invalidate silently
no-ops, since there's nothing to invalidate if Redis is unreachable and
the next successful read will reconcile from Postgres anyway.
"""

import json

STATUS_CACHE_KEY = "status_cache:v1"
# Safety net only. Write-path invalidation (see invalidate_status_cache)
# is what actually keeps this fresh; the TTL just bounds how stale a
# cache entry can ever get if an invalidation call is ever missed (e.g.
# the process crashes between the Postgres commit and the invalidate).
STATUS_CACHE_TTL_SECONDS = 30


async def get_cached_status(redis_client, pool) -> dict:
    if redis_client is not None:
        try:
            raw = await redis_client.get(STATUS_CACHE_KEY)
            if raw is not None:
                return json.loads(raw)
        except Exception:
            pass  # Redis unreachable or holding a corrupt entry -- reconcile below

    # Cache miss for any reason, or Redis unreachable: Postgres is ground
    # truth, always safe to recompute the full status from it.
    import db  # local import: avoids a hard import cycle with db.py

    status = await db.get_status(pool)

    if redis_client is not None:
        try:
            await redis_client.set(STATUS_CACHE_KEY, json.dumps(status), ex=STATUS_CACHE_TTL_SECONDS)
        except Exception:
            pass  # best-effort repopulation; a failure here just means the next read reconciles again

    return status


async def invalidate_status_cache(redis_client) -> None:
    if redis_client is None:
        return
    try:
        await redis_client.delete(STATUS_CACHE_KEY)
    except Exception:
        pass
