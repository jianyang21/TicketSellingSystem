"""
Black-box load client / correctness prober for the ticket-selling service.

Fires a configurable number of concurrent POST /buy requests at a running
instance of the service (main.py or main_safe.py -- it doesn't know or
care which), deliberately replaying a fraction of req_ids multiple times
to probe idempotency handling under concurrency, then reads GET /status
and checks four correctness invariants against it. Reports throughput and
latency percentiles regardless of pass/fail.

It is intentionally a pure HTTP client: it never imports or inspects the
service's internals, only sends requests and reads whatever /status
exposes. That's what lets the exact same script be pointed at the naive
implementation (port 8000) and the fixed one (port 8001) to compare them.

Usage:
    python buyer/load_client.py --base-url http://127.0.0.1:8000
    python buyer/load_client.py --base-url http://127.0.0.1:8001 \
        --num-unique 300 --concurrency 60 --duplicate-fraction 0.3 --replay-count 4

Exit code is 0 if all four invariants pass, 1 otherwise -- so this can be
wired into CI as a correctness gate, not just a manual demo.
"""

import argparse
import concurrent.futures
import random
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests


@dataclass
class RequestResult:
    user_id: str
    req_id: str
    latency_ms: float
    http_status: int
    body_status: Optional[str]  # "success" | "already_processed" | "sold_out" | None on error
    ticket_number: Optional[int]
    error: Optional[str] = None


def build_request_plan(
    num_unique: int,
    duplicate_fraction: float,
    replay_count: int,
    num_users: int,
    seed: int,
) -> List[tuple]:
    """
    Builds the list of (user_id, req_id) pairs to fire. `num_unique`
    distinct logical buy attempts (unique req_ids) are generated; a
    `duplicate_fraction` slice of them gets replayed `replay_count` times
    each under the SAME req_id (simulating a client retrying after a
    timeout, or a double-click/double-tap). The whole plan is shuffled so
    replays land interleaved with fresh attempts instead of clustered,
    maximizing the chance concurrent workers actually collide on them.
    """
    rng = random.Random(seed)
    plan: List[tuple] = []
    for _ in range(num_unique):
        req_id = str(uuid.uuid4())
        user_id = f"user-{rng.randrange(num_users)}"
        copies = replay_count if rng.random() < duplicate_fraction else 1
        plan.extend([(user_id, req_id)] * copies)
    rng.shuffle(plan)
    return plan


def fire_one(base_url: str, user_id: str, req_id: str, timeout: float) -> RequestResult:
    start = time.perf_counter()
    try:
        r = requests.post(
            f"{base_url}/buy",
            json={"user_id": user_id, "req_id": req_id},
            timeout=timeout,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        body = r.json()
        return RequestResult(
            user_id=user_id,
            req_id=req_id,
            latency_ms=latency_ms,
            http_status=r.status_code,
            body_status=body.get("status"),
            ticket_number=body.get("ticket_number"),
        )
    except Exception as e:  # network error, timeout, etc.
        latency_ms = (time.perf_counter() - start) * 1000
        return RequestResult(
            user_id=user_id,
            req_id=req_id,
            latency_ms=latency_ms,
            http_status=-1,
            body_status=None,
            ticket_number=None,
            error=str(e),
        )


def run_load(base_url: str, plan: List[tuple], concurrency: int, timeout: float):
    results: List[RequestResult] = []
    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(fire_one, base_url, u, r, timeout) for (u, r) in plan]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())
    wall_elapsed = time.perf_counter() - wall_start
    return results, wall_elapsed


def percentile(sorted_values: List[float], pct: float) -> float:
    if not sorted_values:
        return float("nan")
    k = (len(sorted_values) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def print_perf_summary(results: List[RequestResult], wall_elapsed: float, concurrency: int) -> None:
    latencies = sorted(r.latency_ms for r in results)
    errors = [r for r in results if r.error]
    print("=== Load Summary ===")
    print(f"Requests fired:        {len(results)}")
    print(f"Concurrency (threads): {concurrency}")
    print(f"Wall-clock duration:   {wall_elapsed:.3f}s")
    print(f"Throughput:            {len(results) / wall_elapsed:.1f} req/s")
    print(f"Transport errors:      {len(errors)}")
    if latencies:
        print(
            f"Latency ms:  min={latencies[0]:.2f}  "
            f"median={statistics.median(latencies):.2f}  "
            f"p99={percentile(latencies, 99):.2f}  "
            f"max={latencies[-1]:.2f}"
        )
    print()


def check_invariants(base_url: str, results: List[RequestResult], timeout: float) -> bool:
    r = requests.get(f"{base_url}/status", timeout=timeout)
    r.raise_for_status()
    status = r.json()

    total_tickets = status["total_tickets"]
    sold_count = status["sold_count"]
    ticket_numbers_issued = status["ticket_numbers_issued"]
    processed_requests: Dict[str, int] = status["processed_requests"]
    user_tickets: Dict[str, list] = status["user_tickets"]

    findings = []

    # INV1: no overselling.
    inv1_pass = sold_count <= total_tickets and len(ticket_numbers_issued) <= total_tickets
    findings.append((
        "INV1 No overselling (sold_count <= total_tickets)",
        inv1_pass,
        f"sold_count={sold_count} total_tickets={total_tickets} "
        f"tickets_issued={len(ticket_numbers_issued)}",
    ))

    # INV2: no duplicate ticket numbers handed out to different requests.
    inv2_pass = len(ticket_numbers_issued) == len(set(ticket_numbers_issued))
    dupes = len(ticket_numbers_issued) - len(set(ticket_numbers_issued))
    findings.append((
        "INV2 No duplicate ticket numbers issued",
        inv2_pass,
        f"issued={len(ticket_numbers_issued)} distinct={len(set(ticket_numbers_issued))} dupes={dupes}",
    ))

    # INV3: idempotency -- every req_id we sent always got back the SAME
    # ticket_number across all its (successful) responses, and that value
    # matches what the server durably recorded for it.
    per_req_tickets: Dict[str, set] = {}
    for res in results:
        if res.body_status in ("success", "already_processed") and res.ticket_number is not None:
            per_req_tickets.setdefault(res.req_id, set()).add(res.ticket_number)
    conflicting = {rid: nums for rid, nums in per_req_tickets.items() if len(nums) > 1}
    mismatched_vs_server = [
        rid for rid, nums in per_req_tickets.items()
        if processed_requests.get(rid) is not None and processed_requests[rid] not in nums
    ]
    inv3_pass = not conflicting and not mismatched_vs_server
    findings.append((
        "INV3 Idempotency holds (same req_id -> same ticket, always)",
        inv3_pass,
        f"req_ids_with_conflicting_tickets={len(conflicting)} "
        f"mismatched_vs_server={len(mismatched_vs_server)}",
    ))

    # INV4: internal bookkeeping is self-consistent -- the count of sold
    # tickets, the count of durable req_id->ticket records, and the total
    # tickets attributed to users should all agree. If a race let a
    # duplicate req_id sell twice, sold_count gets double-incremented but
    # the dict write for that req_id collapses to one entry, so this
    # invariant catches it even when INV3 doesn't observe the collision
    # directly.
    user_ticket_total = sum(len(v) for v in user_tickets.values())
    inv4_pass = sold_count == len(processed_requests) == user_ticket_total
    findings.append((
        "INV4 Bookkeeping consistent (sold_count == processed_requests == user_tickets)",
        inv4_pass,
        f"sold_count={sold_count} processed_requests={len(processed_requests)} "
        f"user_ticket_total={user_ticket_total}",
    ))

    print("=== Invariant Checks ===")
    all_pass = True
    for name, passed, detail in findings:
        all_pass = all_pass and passed
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        print(f"       {detail}")
    print()
    print(f"Overall: {'PASS' if all_pass else 'FAIL'}")
    return all_pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Load client / correctness prober for the ticket seller")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--num-unique", type=int, default=200,
                         help="number of distinct logical buy attempts (unique req_ids)")
    parser.add_argument("--concurrency", type=int, default=50,
                         help="thread pool size / max in-flight requests")
    parser.add_argument("--duplicate-fraction", type=float, default=0.2,
                         help="fraction of req_ids that get replayed multiple times")
    parser.add_argument("--replay-count", type=int, default=3,
                         help="how many times a duplicated req_id is sent")
    parser.add_argument("--num-users", type=int, default=20,
                         help="size of the simulated user pool")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-reset", action="store_true",
                         help="skip calling /reset before firing the load")
    args = parser.parse_args()

    if not args.no_reset:
        r = requests.post(f"{args.base_url}/reset", timeout=args.timeout)
        r.raise_for_status()
        print(f"Reset: {r.json()}\n")

    plan = build_request_plan(
        args.num_unique, args.duplicate_fraction, args.replay_count, args.num_users, args.seed
    )
    print(
        f"Plan: {args.num_unique} unique req_ids, ~{args.duplicate_fraction * 100:.0f}% "
        f"replayed {args.replay_count}x each -> {len(plan)} total HTTP requests, "
        f"concurrency={args.concurrency}\n"
    )

    results, wall_elapsed = run_load(args.base_url, plan, args.concurrency, args.timeout)
    print_perf_summary(results, wall_elapsed, args.concurrency)
    passed = check_invariants(args.base_url, results, args.timeout)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
