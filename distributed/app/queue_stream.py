"""
Redis Streams-backed queue in front of the ticket database.

Why a queue at all, given Postgres already serializes writers correctly
via SELECT ... FOR UPDATE: under a burst of tens of thousands of requests,
having every one of them open a Postgres transaction and block on that
row lock simultaneously would exhaust the connection pool and pile up
enormous queueing INSIDE Postgres, invisible to any client and impossible
to shed cleanly. XADD is cheap and near-instant regardless of burst size;
a bounded set of consumers then drains the stream into Postgres at a
sustainable pace. The queue moves the pile-up to a place (Redis, in
memory) built for exactly this, and the rate limiter upstream controls
how fast the pile grows in the first place.

Durability: the stream is the durable record of "accepted but not yet
sold" work. As long as Redis's AOF (appendonly yes, appendfsync everysec)
persisted the XADD before a crash, that entry survives a Redis restart in
the stream's backlog (visible via XPENDING/XLEN) and gets reprocessed by
consumer_loop() on recovery -- see redis.conf / docker-compose.yml.
`appendfsync everysec` means at most ~1s of accepted-but-unflushed writes
could theoretically be lost in a true crash (not a clean restart); this is
a deliberate throughput/durability trade-off, not an oversight -- `always`
would fsync every single XADD and cost meaningfully more latency for a
one-second worst case that a client will simply retry against.

Request/response bridging: /buy needs a synchronous answer, but Streams
are asynchronous by nature. We bridge the two with a per-request Redis
list (`result:{req_id}`): the consumer RPUSHes the outcome there after
writing to Postgres, and the producer BLPOPs it with a timeout. If no
result shows up in time (consumer backlog, or Redis itself going away
mid-wait), the caller (main.py) falls back to the direct database path.
"""

import asyncio
import json

import redis.asyncio as aioredis
import redis.exceptions

from db import atomic_buy

# Redis client errors and a bare connection refusal both surface as
# redis.exceptions.ConnectionError, but a DNS lookup failure for the
# hostname itself (e.g. the "redis" service being stopped in Docker)
# surfaces as socket.gaierror, which is a plain OSError -- redis-py does
# NOT wrap it. Catching only ConnectionError would let that case slip
# through uncaught here.
REDIS_UNAVAILABLE_ERRORS = (redis.exceptions.ConnectionError, OSError)

STREAM_NAME = "buy_requests"
GROUP_NAME = "sellers"


async def ensure_group(redis_client: aioredis.Redis) -> None:
    try:
        await redis_client.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def enqueue_and_wait(
    redis_client: aioredis.Redis, user_id: str, req_id: str, timeout: float
) -> dict | None:
    """
    Enqueues the buy attempt and blocks (up to `timeout` seconds) for the
    consumer's result. Returns None on timeout -- the caller decides what
    to do next (main.py falls back to the direct DB path). Lets
    redis.exceptions.ConnectionError propagate so the caller's except
    block (the Redis-down fallback) can catch it.
    """
    await redis_client.xadd(STREAM_NAME, {"user_id": user_id, "req_id": req_id})
    result_key = f"result:{req_id}"
    popped = await redis_client.blpop([result_key], timeout=timeout)
    if popped is None:
        return None
    _, raw = popped
    return json.loads(raw)


async def consumer_loop(redis_client: aioredis.Redis, pool, consumer_name: str, circuit=None) -> None:
    """
    Outer supervisor for _run_consumer(). This background task must never
    permanently die: if it does (some exception we didn't anticipate
    propagates out), this consumer silently vanishes from the `sellers`
    group forever -- XINFO GROUPS would show 0 active consumers -- and
    nobody would notice until the stream backlog quietly grows. So
    literally any exception here just costs a 1s retry and a fresh
    _run_consumer() call (which re-does ensure_group() from scratch),
    rather than being allowed to end the task.
    """
    while True:
        try:
            await _run_consumer(redis_client, pool, consumer_name, circuit)
        except asyncio.CancelledError:
            raise
        except Exception:
            if circuit is not None:
                await circuit.record_failure(redis_client)
            await asyncio.sleep(1)


async def _process_entry(redis_client: aioredis.Redis, pool, entry_id, fields: dict) -> None:
    user_id = fields["user_id"]
    req_id = fields["req_id"]
    try:
        result = await atomic_buy(pool, user_id, req_id, redis_client=redis_client)
    except Exception as e:  # noqa: BLE001 - must not crash the consumer loop
        result = {"status": "error", "ticket_number": None, "error": str(e)}

    result_key = f"result:{req_id}"
    try:
        await redis_client.rpush(result_key, json.dumps(result))
        await redis_client.expire(result_key, 30)
        await redis_client.xack(STREAM_NAME, GROUP_NAME, entry_id)
    except REDIS_UNAVAILABLE_ERRORS:
        # Redis died between processing and acking. The DB write already
        # committed; the entry will be redelivered (it stays pending) once
        # Redis is back, and atomic_buy()'s idempotency check makes
        # reprocessing it safe -- it will just return "already_processed"
        # the second time.
        pass


async def _run_consumer(redis_client: aioredis.Redis, pool, consumer_name: str, circuit) -> None:
    """
    Runs forever (until something raises) as one member of the `sellers`
    consumer group. Redis Streams consumer groups guarantee each stream
    entry is delivered to exactly one consumer in the group, so running
    this in every app replica turns the replicas into a worker pool for
    free -- no separate worker service needed. Reconnection on Redis
    outages is handled by simply retrying; when Redis comes back,
    XREADGROUP resumes exactly where the group's cursor left off,
    including any entries that were never acknowledged.

    `circuit`, if given, is shared with the rate limiter and the /buy
    handler (see circuit_breaker.py): while it's open we skip XREADGROUP
    entirely rather than repeatedly rediscovering the same outage, so
    this loop doesn't add to the pile of failing DNS/connection attempts
    during an outage.
    """
    await ensure_group(redis_client)
    while True:
        if circuit is not None and circuit.is_open():
            await asyncio.sleep(1)
            continue
        try:
            response = await redis_client.xreadgroup(
                GROUP_NAME, consumer_name, {STREAM_NAME: ">"}, count=10, block=1000
            )
            if circuit is not None:
                circuit.record_success()
        except REDIS_UNAVAILABLE_ERRORS:
            if circuit is not None:
                await circuit.record_failure(redis_client)
            await asyncio.sleep(1)
            continue
        except redis.exceptions.ResponseError:
            await ensure_group(redis_client)
            continue

        if not response:
            continue

        for _stream, entries in response:
            # RCA follow-up: this used to be a sequential for-loop awaiting
            # one atomic_buy() at a time, serializing a whole batch even
            # though Postgres's row lock (the actual serialization point)
            # can be queued from many callers at once. Dispatching the
            # batch concurrently lets each entry queue for the lock as soon
            # as it's ready instead of waiting its turn behind this
            # consumer's own earlier entries in program order too.
            await asyncio.gather(*[
                _process_entry(redis_client, pool, entry_id, fields)
                for entry_id, fields in entries
            ])
