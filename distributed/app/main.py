"""
Ticket Selling Service - Stage 3 (DISTRIBUTED)

Stateless FastAPI app instance, meant to run as N replicas behind nginx.
No process-local state at all -- Postgres (see db.py) is the only source
of truth, which is what makes running many replicas safe: there's nothing
in any one process's memory that a lock could protect, because there's
nothing left in process memory to protect.

Request flow for POST /buy:
    1. Hybrid rate limiter (see rate_limiter.py) -- reject early with 429
       if the shared token/leaky bucket says no. This is the main defense
       against a 50,000-request burst: most of it never reaches Redis or
       Postgres at all.
    2. Try the Redis Streams path (see queue_stream.py): enqueue, then
       block briefly for the consumer's result.
    3. If Redis is unreachable, OR the queue didn't produce a result
       within the timeout (backlog too deep), fall back to calling
       atomic_buy() directly against Postgres -- the exact same function
       the consumer uses, so this path can't drift from queued
       correctness.

Every app replica also runs a background consumer_loop() task, so the
replicas double as the Redis Streams consumer group's worker pool.
"""

import asyncio
import os
import uuid

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

import cache
import db
from circuit_breaker import RedisCircuitBreaker
from queue_stream import consumer_loop, enqueue_and_wait
from rate_limiter import HybridRateLimiter

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://tickets:tickets@localhost:5433/tickets"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "app-local")
TOTAL_TICKETS = int(os.environ.get("TOTAL_TICKETS", "100"))
QUEUE_WAIT_TIMEOUT_SECONDS = float(os.environ.get("QUEUE_WAIT_TIMEOUT_SECONDS", "5.0"))
DB_POOL_MIN_SIZE = int(os.environ.get("DB_POOL_MIN_SIZE", "2"))
DB_POOL_MAX_SIZE = int(os.environ.get("DB_POOL_MAX_SIZE", "10"))

# RCA follow-up: the DB row lock measured ~190 tx/s in isolation, far above
# what the original defaults (token_capacity=50, token_refill_rate=30,
# leaky_capacity=300, leaky_leak_rate=50) ever let through -- those numbers
# were the actual admission-rate ceiling in the original load test, not
# Postgres. Raised here with real headroom to spare, tunable without a
# rebuild.
RATE_LIMIT_TOKEN_CAPACITY = float(os.environ.get("RATE_LIMIT_TOKEN_CAPACITY", "100"))
RATE_LIMIT_TOKEN_REFILL_RATE = float(os.environ.get("RATE_LIMIT_TOKEN_REFILL_RATE", "80"))
RATE_LIMIT_LEAKY_CAPACITY = float(os.environ.get("RATE_LIMIT_LEAKY_CAPACITY", "400"))
RATE_LIMIT_LEAKY_LEAK_RATE = float(os.environ.get("RATE_LIMIT_LEAKY_LEAK_RATE", "80"))

app = FastAPI(title=f"Ticket Selling Service (Stage 3 - Distributed) [{INSTANCE_ID}]")


class BuyRequest(BaseModel):
    user_id: str
    req_id: str

    @field_validator("req_id")
    @classmethod
    def req_id_must_be_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("req_id must be a valid UUID string")
        return v


@app.on_event("startup")
async def startup() -> None:
    app.state.pg_pool = await db.create_pool(DATABASE_URL, min_size=DB_POOL_MIN_SIZE, max_size=DB_POOL_MAX_SIZE)
    # socket_connect_timeout bounds the initial TCP connect phase only, so
    # it can stay short: when Redis (or DNS for its hostname) is
    # unreachable, we need to find that out in well under a second rather
    # than linger on the OS/DNS resolver's own retry timeout.
    #
    # socket_timeout bounds EVERY socket read, including BLPOP's own
    # server-side blocking wait -- it must be longer than the longest
    # blocking command we issue (BLPOP with timeout=QUEUE_WAIT_TIMEOUT_SECONDS
    # in queue_stream.py), or a completely healthy BLPOP that legitimately
    # found nothing within its own timeout gets misread as Redis being
    # down, which needlessly trips the circuit breaker. Give it a 1s
    # buffer over that.
    app.state.redis = aioredis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=QUEUE_WAIT_TIMEOUT_SECONDS + 1.0,
    )
    app.state.redis_circuit = RedisCircuitBreaker(cooldown_seconds=2.0)
    app.state.limiter = HybridRateLimiter(
        app.state.redis,
        token_capacity=RATE_LIMIT_TOKEN_CAPACITY,
        token_refill_rate=RATE_LIMIT_TOKEN_REFILL_RATE,
        leaky_capacity=RATE_LIMIT_LEAKY_CAPACITY,
        leaky_leak_rate=RATE_LIMIT_LEAKY_LEAK_RATE,
        circuit=app.state.redis_circuit,
    )
    # RCA follow-up: running with --workers > 1 (see Dockerfile) means
    # multiple OS processes share this same INSTANCE_ID env var. Redis
    # Streams consumer-group membership is keyed by consumer NAME, so two
    # processes registering under the identical name would be indistinguishable
    # to XREADGROUP/XPENDING -- append the PID to keep each worker's consumer
    # identity unique within the group.
    consumer_name = f"{INSTANCE_ID}-{os.getpid()}"
    app.state.consumer_task = asyncio.create_task(
        consumer_loop(
            app.state.redis, app.state.pg_pool, consumer_name=consumer_name, circuit=app.state.redis_circuit
        )
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    if app.state.consumer_task is not None:
        app.state.consumer_task.cancel()
    await app.state.pg_pool.close()
    await app.state.redis.aclose()


@app.get("/health")
async def health():
    return {"status": "ok", "instance": INSTANCE_ID}


@app.post("/reset")
async def reset():
    result = await db.reset_state(app.state.pg_pool, TOTAL_TICKETS, redis_client=app.state.redis)

    # Best-effort: clear the stream and recreate the consumer group so a
    # fresh test run doesn't reprocess stale backlog from a prior run.
    # Not required for correctness -- atomic_buy() is idempotent either
    # way -- just keeps demo runs clean.
    if not app.state.redis_circuit.is_open():
        try:
            from queue_stream import GROUP_NAME, STREAM_NAME

            await app.state.redis.delete(STREAM_NAME)
            await app.state.redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
            app.state.redis_circuit.record_success()
        except Exception:
            await app.state.redis_circuit.record_failure(app.state.redis)

    return {
        "status": "ok",
        "instance": INSTANCE_ID,
        "message": "State reset. Starting fresh with 0 sold.",
        **result,
    }


@app.post("/buy")
async def buy(req: BuyRequest):
    allowed, reason = await app.state.limiter.allow()
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "status": "rate_limited",
                "reason": reason,
                "user_id": req.user_id,
                "req_id": req.req_id,
                "ticket_number": None,
                "instance": INSTANCE_ID,
            },
        )

    path = "queue"
    result = None
    if app.state.redis_circuit.is_open():
        # Known-down: don't pay for another failed connection attempt (and
        # its DNS-lookup cost) on this request too -- go straight to the
        # database fallback. See circuit_breaker.py.
        path = "db_fallback_redis_down"
    else:
        try:
            result = await enqueue_and_wait(
                app.state.redis, req.user_id, req.req_id, timeout=QUEUE_WAIT_TIMEOUT_SECONDS
            )
            app.state.redis_circuit.record_success()
        except (aioredis.RedisError, OSError):
            await app.state.redis_circuit.record_failure(app.state.redis)
            path = "db_fallback_redis_down"

    if result is None and path == "queue":
        path = "db_fallback_queue_timeout"

    if result is None:
        result = await db.atomic_buy(app.state.pg_pool, req.user_id, req.req_id, redis_client=app.state.redis)

    return {
        **result,
        "user_id": req.user_id,
        "req_id": req.req_id,
        "path": path,
        "instance": INSTANCE_ID,
    }


@app.get("/status")
async def status():
    result = await cache.get_cached_status(app.state.redis, app.state.pg_pool)
    return result
