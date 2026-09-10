"""
Postgres access layer: the ground truth for ticket state and idempotency.

atomic_buy() is the ONLY place ticket state is ever mutated, and it is
called from two places that must behave identically:
  1. The Redis Streams consumer loop (the normal, queued path).
  2. The /buy handler's direct fallback, used when Redis is unreachable.
Sharing this single function is what guarantees the fallback path can
never diverge from the queued path's correctness -- there's no separate
"fallback logic" to keep in sync, just a different caller.
"""

import asyncpg

from cache import invalidate_status_cache

_CREATE_POOL_LOCK_NOTE = """
Correctness note: the SELECT ... FOR UPDATE below takes a row lock on the
singleton tickets_state row for the duration of the transaction. Any other
transaction (in this process, another app replica, or the consumer worker)
that tries to FOR UPDATE that same row blocks until this one commits or
rolls back. That's what makes this the distributed-system equivalent of
Stage 2's threading.Lock: instead of one process serializing its own
threads, Postgres now serializes every writer across every process that
touches this table.
"""


async def create_pool(database_url: str, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    # min_size/max_size are PER PROCESS. With --workers > 1 per app replica
    # (see main.py / Dockerfile), each worker gets its own pool, so the
    # cluster-wide connection count is (replicas * workers * max_size) --
    # keep this modest and raise Postgres's max_connections to match
    # rather than defaulting this back up to a large per-process number.
    return await asyncpg.create_pool(database_url, min_size=min_size, max_size=max_size)


async def atomic_buy(pool: asyncpg.Pool, user_id: str, req_id: str, redis_client=None) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT total_tickets, sold_count FROM tickets_state WHERE id = 1 FOR UPDATE"
            )
            total_tickets, sold_count = row["total_tickets"], row["sold_count"]

            # Idempotency check happens WHILE HOLDING the row lock above, so
            # no concurrent transaction can insert a competing row for this
            # req_id between this check and the insert below. The UNIQUE
            # constraint on sold_tickets.req_id is still the ultimate ground
            # truth (see the except clause below) -- this lock is what makes
            # that constraint's failure path effectively unreachable rather
            # than something we depend on happening under contention.
            existing = await conn.fetchrow(
                "SELECT ticket_number FROM sold_tickets WHERE req_id = $1", req_id
            )
            if existing is not None:
                return {
                    "status": "already_processed",
                    "ticket_number": existing["ticket_number"],
                    "remaining_tickets": total_tickets - sold_count,
                }

            if sold_count >= total_tickets:
                return {
                    "status": "sold_out",
                    "ticket_number": None,
                    "remaining_tickets": 0,
                }

            new_sold_count = sold_count + 1
            await conn.execute(
                "UPDATE tickets_state SET sold_count = $1 WHERE id = 1", new_sold_count
            )
            try:
                await conn.execute(
                    "INSERT INTO sold_tickets (req_id, user_id, ticket_number) "
                    "VALUES ($1, $2, $3)",
                    req_id,
                    user_id,
                    new_sold_count,
                )
            except asyncpg.UniqueViolationError:
                # Ground truth backstop: if this is ever reached, the row
                # lock above failed to serialize two writers for the same
                # req_id. Re-raising rolls back the whole transaction
                # (including the sold_count increment), so no ticket is
                # lost or double-counted even in that scenario.
                raise

    # Outside the pool/transaction context (state is already durably
    # committed): a ticket was actually sold, so the cached GET /status
    # snapshot is now stale. Invalidate it here rather than inside the
    # transaction so a slow Redis call can never extend how long the row
    # lock above is held. "already_processed" and "sold_out" above return
    # early without reaching here because they didn't change anything --
    # nothing to invalidate.
    await invalidate_status_cache(redis_client)
    return {
        "status": "success",
        "ticket_number": new_sold_count,
        "remaining_tickets": total_tickets - new_sold_count,
    }


async def reset_state(pool: asyncpg.Pool, total_tickets: int, redis_client=None) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT total_tickets FROM tickets_state WHERE id = 1 FOR UPDATE")
            await conn.execute(
                "UPDATE tickets_state SET total_tickets = $1, sold_count = 0", total_tickets
            )
            await conn.execute("DELETE FROM sold_tickets")

    await invalidate_status_cache(redis_client)
    return {
        "total_tickets": total_tickets,
        "sold_count": 0,
        "remaining_tickets": total_tickets,
    }


async def get_status(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        state_row = await conn.fetchrow(
            "SELECT total_tickets, sold_count FROM tickets_state WHERE id = 1"
        )
        ticket_rows = await conn.fetch(
            "SELECT req_id, user_id, ticket_number FROM sold_tickets ORDER BY ticket_number"
        )

    processed_requests = {}
    user_tickets: dict = {}
    ticket_numbers_issued = []
    for r in ticket_rows:
        req_id = str(r["req_id"])
        processed_requests[req_id] = r["ticket_number"]
        user_tickets.setdefault(r["user_id"], []).append(r["ticket_number"])
        ticket_numbers_issued.append(r["ticket_number"])

    return {
        "total_tickets": state_row["total_tickets"],
        "sold_count": state_row["sold_count"],
        "remaining_tickets": state_row["total_tickets"] - state_row["sold_count"],
        "ticket_numbers_issued": ticket_numbers_issued,
        "processed_requests": processed_requests,
        "user_tickets": user_tickets,
    }
