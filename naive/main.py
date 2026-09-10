"""
Ticket Selling Service - Stage 1 (NAIVE / INTENTIONALLY RACY)
================================================================

Purpose of this stage:
    This is a deliberately naive, single-process, in-memory implementation
    of a ticket selling service. It is built to run with a SINGLE uvicorn
    worker but is still NOT safe under concurrency, because Python can
    switch between coroutines/threads at `await` points (and, if you ever
    ran this with multiple threads/workers, at arbitrary bytecode points
    too). The whole point of Stage 1 is to have a *working but flawed*
    baseline that we can later fix with proper locking / atomic
    operations / transactions in later stages.

Known race conditions baked in on purpose (search for "NAIVE:" comments):
    1. Overselling: the "check remaining tickets" step and the "decrement
       remaining tickets" step are two separate, non-atomic operations.
       Two concurrent requests can both pass the check before either one
       performs the decrement, resulting in more tickets sold than exist.
    2. Idempotency check-then-act gap: checking whether a req_id has
       already been processed, and recording that req_id as processed,
       are also two separate steps. Two concurrent requests with the same
       req_id can both see "not seen yet" and both sell a ticket.

No locks (threading.Lock, asyncio.Lock), no atomic compare-and-swap, no
database transactions are used anywhere in this file. That is intentional
for Stage 1.
"""

import time
import uuid
from typing import Dict, Optional

from fastapi import FastAPI
from pydantic import BaseModel, field_validator

app = FastAPI(title="Ticket Selling Service (Stage 1 - Naive)")

# ---------------------------------------------------------------------------
# In-memory "database". Single dict of global state, reset on /reset.
# NAIVE: This is plain process memory, not a real datastore. It vanishes on
# restart and cannot be shared across multiple worker processes. That is
# fine for Stage 1 since we are explicitly running with a single worker.
# ---------------------------------------------------------------------------

TOTAL_TICKETS = 100  # total tickets available each time we /reset

# Simulated datastore round-trip latency, inserted between each "check" read
# and its corresponding "act" write in /buy below. Real check-then-act races
# exist here regardless of this constant -- Starlette already runs each sync
# endpoint on its own OS thread, so two requests can genuinely interleave.
# But pure in-memory dict reads/writes take low-single-digit microseconds,
# so a short load test can go a long time without a thread's context switch
# landing inside that tiny window. This constant stands in for a real
# datastore call (a network round trip to Postgres/Redis/etc. almost never
# costs less than this) so the race window is wide enough to be reliably
# observed in a short, deliberate load test rather than only in sustained
# production traffic. It is NOT what causes the race -- removing it would
# leave the race intact, just harder to trigger on demand.
DATASTORE_LATENCY_SECONDS = 0.003

state: Dict[str, object] = {
    "total_tickets": TOTAL_TICKETS,
    "sold_count": 0,
    # req_id (str) -> ticket_number (int) that was sold for that request.
    # This is what makes /buy idempotent: same req_id should always map
    # back to the same ticket_number, no matter how many times it's called.
    "processed_requests": {},  # type: Dict[str, int]
    # user_id (str) -> list of ticket_numbers purchased by that user.
    "user_tickets": {},  # type: Dict[str, list]
}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class BuyRequest(BaseModel):
    user_id: str
    req_id: str

    @field_validator("req_id")
    @classmethod
    def req_id_must_be_uuid(cls, v: str) -> str:
        # Validate that req_id is a well-formed UUID string. This is what
        # the client is expected to generate once per logical "buy attempt"
        # and resend unchanged on retries, so we can dedupe on it.
        try:
            uuid.UUID(v)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("req_id must be a valid UUID string")
        return v


class BuyResponse(BaseModel):
    status: str  # "success" | "already_processed" | "sold_out"
    ticket_number: Optional[int] = None
    user_id: str
    req_id: str
    remaining_tickets: int
    message: str


class ResetResponse(BaseModel):
    status: str
    total_tickets: int
    sold_count: int
    remaining_tickets: int
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/reset", response_model=ResetResponse)
def reset():
    """
    Wipes all existing state and starts fresh with 0 sold.
    Returns the new ticket counts.
    """
    state["total_tickets"] = TOTAL_TICKETS
    state["sold_count"] = 0
    state["processed_requests"] = {}
    state["user_tickets"] = {}

    return ResetResponse(
        status="ok",
        total_tickets=state["total_tickets"],
        sold_count=state["sold_count"],
        remaining_tickets=state["total_tickets"] - state["sold_count"],
        message="State reset. Starting fresh with 0 sold.",
    )


@app.post("/buy", response_model=BuyResponse)
def buy(req: BuyRequest):
    """
    Attempt to buy a single ticket for user_id, deduped by req_id.

    Intended semantics (NOT safely enforced here, see comments below):
      - First call with a given req_id: sells a ticket (if available) and
        remembers which ticket_number was sold for that req_id.
      - Any subsequent call with the SAME req_id: returns the SAME
        ticket_number, without selling another ticket.
      - If tickets are sold out, returns a "sold_out" response instead of
        raising an error.
    """

    # -----------------------------------------------------------------
    # NAIVE / RACE #1: Idempotency check-then-act gap.
    #
    # We *check* whether this req_id has already been processed here, and
    # only *act* on that (return early, or fall through to sell) after a
    # simulated datastore round trip. Between the check and the act,
    # another request with the exact same req_id can run concurrently,
    # also fail to find it here, and also proceed to sell a ticket.
    # Result: the same "logical" buy attempt (same req_id) can sell two
    # different tickets instead of being deduped to one. A correct
    # implementation would need this check + insert to be a single atomic
    # operation (e.g. an atomic "insert if absent" backed by a DB unique
    # constraint or a lock).
    # -----------------------------------------------------------------
    already_processed = req.req_id in state["processed_requests"]
    ticket_number_if_already_processed = state["processed_requests"].get(req.req_id)

    time.sleep(DATASTORE_LATENCY_SECONDS)  # simulated datastore round trip; see comment above DATASTORE_LATENCY_SECONDS

    if already_processed:
        return BuyResponse(
            status="already_processed",
            ticket_number=ticket_number_if_already_processed,
            user_id=req.user_id,
            req_id=req.req_id,
            remaining_tickets=state["total_tickets"] - state["sold_count"],
            message="This req_id was already processed. Returning the same ticket.",
        )

    # -----------------------------------------------------------------
    # NAIVE / RACE #2: Overselling via check-then-act on sold_count.
    #
    # We *read* sold_count here to decide if tickets remain, and only
    # *act* on that decision (reject as sold out, or fall through to
    # increment) after a simulated datastore round trip. Between the read
    # and the write, any number of other concurrent requests can perform
    # the same read, see the same "space available" answer, and all
    # proceed to sell a ticket. This is the classic TOCTOU (time-of-check
    # to time-of-use) bug and is exactly how overselling happens in real
    # systems that skip locking/atomic operations. A correct
    # implementation would use something like an atomic
    # decrement-if-positive (DB row lock, SELECT ... FOR UPDATE, atomic
    # UPDATE ... WHERE remaining > 0, or an in-process lock/semaphore).
    # -----------------------------------------------------------------
    sold_out = state["sold_count"] >= state["total_tickets"]

    time.sleep(DATASTORE_LATENCY_SECONDS)  # simulated datastore round trip; widens the race window above

    if sold_out:
        return BuyResponse(
            status="sold_out",
            ticket_number=None,
            user_id=req.user_id,
            req_id=req.req_id,
            remaining_tickets=0,
            message="Sold out. No tickets remaining.",
        )

    # --- "Sell" the ticket -------------------------------------------------
    # NAIVE: sold_count is read again and incremented here, non-atomically
    # with respect to the check above. ticket_number is derived from
    # sold_count + 1, so a race in sold_count can also produce duplicate
    # ticket numbers being handed out to different requests.
    state["sold_count"] += 1
    ticket_number = state["sold_count"]

    # Record this req_id as processed AFTER selling, widening the race
    # window described above even further.
    state["processed_requests"][req.req_id] = ticket_number

    # Track which tickets this user holds (not itself part of the
    # overselling/idempotency race, just bookkeeping).
    state["user_tickets"].setdefault(req.user_id, []).append(ticket_number)

    return BuyResponse(
        status="success",
        ticket_number=ticket_number,
        user_id=req.user_id,
        req_id=req.req_id,
        remaining_tickets=state["total_tickets"] - state["sold_count"],
        message="Ticket purchased successfully.",
    )


@app.get("/status")
def status():
    """
    Debug/introspection endpoint: exposes the full internal state so an
    external client (see scripts/load_client.py) can verify correctness
    invariants (no overselling, no duplicate ticket numbers, idempotency
    held, internal bookkeeping consistent) purely by reading HTTP
    responses, without reaching into process memory.
    """
    ticket_numbers_issued = list(state["processed_requests"].values())
    return {
        "total_tickets": state["total_tickets"],
        "sold_count": state["sold_count"],
        "remaining_tickets": state["total_tickets"] - state["sold_count"],
        "ticket_numbers_issued": ticket_numbers_issued,
        "processed_requests": state["processed_requests"],
        "user_tickets": state["user_tickets"],
    }
