"""
Manual demonstration script for Stage 1's intentional race conditions.

Not part of the service itself - just a client-side script to fire a burst
of concurrent /buy requests at a running server and show that:
  1. sold_count can exceed total_tickets (overselling), and/or
  2. duplicate ticket_numbers can be handed out (via the sold_count race
     and/or the idempotency check-then-act race).

Usage:
    python buyer/race_test.py
Assumes the server is already running at http://127.0.0.1:8000.
"""

import concurrent.futures
import uuid

import requests

BASE_URL = "http://127.0.0.1:8000"
TOTAL_TICKETS = 100  # must match the server's TOTAL_TICKETS
NUM_REQUESTS = 250  # fired concurrently, well above TOTAL_TICKETS


def reset():
    r = requests.post(f"{BASE_URL}/reset")
    r.raise_for_status()
    print("reset:", r.json())


def buy_unique(_i):
    # Each request uses its own fresh req_id (simulates NUM_REQUESTS
    # different users/attempts racing for the last few tickets).
    req_id = str(uuid.uuid4())
    r = requests.post(f"{BASE_URL}/buy", json={"user_id": f"user-{_i}", "req_id": req_id})
    return r.json()


def buy_same_req_id(req_id, i):
    # Simulates the SAME logical buy attempt (e.g. a client retry) firing
    # concurrently with itself under the same req_id.
    r = requests.post(f"{BASE_URL}/buy", json={"user_id": "retry-user", "req_id": req_id})
    return r.json()


def main():
    print("=== Demonstrating overselling race (unique req_ids) ===")
    reset()
    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_REQUESTS) as ex:
        results = list(ex.map(buy_unique, range(NUM_REQUESTS)))

    successes = [r for r in results if r["status"] == "success"]
    ticket_numbers = [r["ticket_number"] for r in successes]
    print(f"Total requests fired: {NUM_REQUESTS}")
    print(f"Successful buys:      {len(successes)} (expected <= {TOTAL_TICKETS} if not oversold)")
    print(f"Distinct ticket #s:   {len(set(ticket_numbers))}")
    print(f"Duplicate ticket #s?  {len(ticket_numbers) != len(set(ticket_numbers))}")
    if len(successes) > TOTAL_TICKETS:
        print(">>> OVERSOLD: more tickets sold than exist! <<<")

    print()
    print("=== Demonstrating idempotency race (same req_id, concurrent) ===")
    reset()
    shared_req_id = str(uuid.uuid4())
    with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_REQUESTS) as ex:
        futures = [ex.submit(buy_same_req_id, shared_req_id, i) for i in range(NUM_REQUESTS)]
        results = [f.result() for f in futures]

    successes = [r for r in results if r["status"] == "success"]
    ticket_numbers = set(r["ticket_number"] for r in successes if r["ticket_number"] is not None)
    print(f"Requests with SAME req_id fired: {NUM_REQUESTS}")
    print(f"Responses with status=success:   {len(successes)} (expected exactly 1 if idempotent)")
    print(f"Distinct ticket numbers granted: {ticket_numbers}")
    if len(successes) > 1:
        print(">>> IDEMPOTENCY BROKEN: same req_id sold more than once! <<<")


if __name__ == "__main__":
    main()
