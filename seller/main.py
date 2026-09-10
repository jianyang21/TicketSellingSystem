"""
Ticket Selling Service - Stage 2 (FIXED / SERIALIZED)
================================================================

This is the fix for the two races deliberately built into main.py
(Stage 1). It is byte-for-byte the same domain logic, running with the
same single uvicorn worker -- the only change is that /buy's entire
check-then-act sequence (idempotency check, oversell check, sold_count
increment, bookkeeping writes) now runs inside one process-wide
threading.Lock (buy_lock).

Why one lock fixes both races at once:
    Stage 1 had two separate check-then-act windows (idempotency, and
    oversell/increment) that could each be entered by multiple threads
    concurrently, because Starlette runs each sync `def` endpoint on its
    own OS thread from a thread pool. A lock does not remove that
    concurrency -- threads still get scheduled onto the endpoint in
    parallel -- but by acquiring buy_lock at the very top of the
    function and holding it until the response is fully decided, only
    one thread at a time is ever inside the "check + simulated datastore
    latency + act" sequence for *either* race. Every other thread blocks
    on lock.acquire() until it's released, so the check any thread sees
    is guaranteed to reflect every write that happened before it, and no
    two threads can act on a check that's since gone stale.

Trade-off, and why it matters for the load-test writeup:
    This still keeps DATASTORE_LATENCY_SECONDS (a simulated ~3ms
    datastore round trip) on the *same* code path as Stage 1, so the
    comparison between the naive and fixed runs is apples-to-apples:
    identical per-request work, the only difference is serialization.
    But because the lock is held across that simulated latency, /buy
    here is now fully serialized -- only one request can be "inside the
    datastore" at a time, regardless of how many arrive concurrently.
    Throughput is therefore capped at roughly
    1 / (2 * DATASTORE_LATENCY_SECONDS) requests/sec no matter how much
    concurrency the client throws at it. That's the real cost of
    correctness here, and it's the seed of the "where's the bottleneck"
    question for the next stage: the lock (and whatever sits behind it)
    is the bottleneck, by construction.
"""

import threading
import time
import uuid
from typing import Dict, Optional

from fastapi import FastAPI
from pydantic import BaseModel, field_validator

app = FastAPI(title="Ticket Selling Service (Stage 2 - Fixed, Single Lock)")

TOTAL_TICKETS = 100  # total tickets available each time we /reset

# Same simulated datastore round-trip latency as Stage 1 -- kept identical
# so a naive-vs-fixed load test comparison isolates the effect of the lock,
# not a difference in per-request work.
DATASTORE_LATENCY_SECONDS = 0.003

state: Dict[str, object] = {
    "total_tickets": TOTAL_TICKETS,
    "sold_count": 0,
    "processed_requests": {},  # type: Dict[str, int]
    "user_tickets": {},  # type: Dict[str, list]
}

# Single process-wide lock guarding all reads/writes of `state` that
# participate in the buy decision. Fine for a single uvicorn worker; it
# would NOT coordinate across multiple worker processes or machines (that
# needs a real datastore's transactions/atomic ops instead of an
# in-process lock) -- out of scope for this stage.
buy_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Request / response models (identical to Stage 1)
# ---------------------------------------------------------------------------

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
    Wipes all existing state and starts fresh with 0 sold. Locked so a
    reset can't interleave with an in-flight /buy.
    """
    with buy_lock:
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

    FIXED: the entire check-then-act sequence below -- idempotency check,
    oversell check, increment, and bookkeeping -- runs under buy_lock.
    Only one thread executes any of it at a time, so every check any
    caller sees is up to date and no two callers can act on the same
    stale read. This is what makes both Stage 1 races (overselling, and
    duplicate tickets for one req_id) impossible here, no matter how
    many concurrent requests arrive.
    """
    with buy_lock:
        already_processed = req.req_id in state["processed_requests"]
        ticket_number_if_already_processed = state["processed_requests"].get(req.req_id)

        time.sleep(DATASTORE_LATENCY_SECONDS)  # simulated datastore round trip

        if already_processed:
            return BuyResponse(
                status="already_processed",
                ticket_number=ticket_number_if_already_processed,
                user_id=req.user_id,
                req_id=req.req_id,
                remaining_tickets=state["total_tickets"] - state["sold_count"],
                message="This req_id was already processed. Returning the same ticket.",
            )

        sold_out = state["sold_count"] >= state["total_tickets"]

        time.sleep(DATASTORE_LATENCY_SECONDS)  # simulated datastore round trip

        if sold_out:
            return BuyResponse(
                status="sold_out",
                ticket_number=None,
                user_id=req.user_id,
                req_id=req.req_id,
                remaining_tickets=0,
                message="Sold out. No tickets remaining.",
            )

        state["sold_count"] += 1
        ticket_number = state["sold_count"]
        state["processed_requests"][req.req_id] = ticket_number
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
    invariants purely by reading HTTP responses.
    """
    with buy_lock:
        ticket_numbers_issued = list(state["processed_requests"].values())
        return {
            "total_tickets": state["total_tickets"],
            "sold_count": state["sold_count"],
            "remaining_tickets": state["total_tickets"] - state["sold_count"],
            "ticket_numbers_issued": ticket_numbers_issued,
            "processed_requests": state["processed_requests"],
            "user_tickets": state["user_tickets"],
        }
