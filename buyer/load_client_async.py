"""
Async, high-concurrency version of load_client.py for testing the
distributed stack (nginx + N app replicas + Redis Streams + Postgres) at
realistic scale (tens of thousands of requests). The thread-pool-based
load_client.py tops out in the low thousands of requests before OS thread
overhead dominates; this uses asyncio + httpx.AsyncClient with a bounded
semaphore instead, so 50,000 requests is cheap to fire.

Same four correctness invariants as load_client.py, checked the same way
via GET /status. The one addition: this version also reports a
BREAKDOWN of response categories (success / already_processed / sold_out
/ rate_limited / error), because at this scale the rate limiter is
expected and intended to reject the overwhelming majority of requests --
that's it doing its job, not a failure.

Usage:
    python buyer/load_client_async.py --base-url http://127.0.0.1:8080 \
        --num-unique 40000 --duplicate-fraction 0.1 --replay-count 3 \
        --max-inflight 500
"""

import argparse
import asyncio
import random
import statistics
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx


@dataclass
class RequestResult:
    user_id: str
    req_id: str
    latency_ms: float
    http_status: int
    body_status: Optional[str]
    ticket_number: Optional[int]
    error: Optional[str] = None


def build_request_plan(num_unique, duplicate_fraction, replay_count, num_users, seed):
    rng = random.Random(seed)
    plan: List[tuple] = []
    for _ in range(num_unique):
        req_id = str(uuid.uuid4())
        user_id = f"user-{rng.randrange(num_users)}"
        copies = replay_count if rng.random() < duplicate_fraction else 1
        plan.extend([(user_id, req_id)] * copies)
    rng.shuffle(plan)
    return plan


async def fire_one(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, base_url, user_id, req_id, timeout, max_retries=2
) -> RequestResult:
    """
    Fires one buy attempt, retrying transport-level failures (not HTTP
    error responses -- those are legitimate answers) up to `max_retries`
    times with the SAME req_id. This isn't just a load-generator
    convenience: it's exactly the scenario req_id idempotency exists for
    -- "I don't know if that went through, let me safely ask again" -- so
    exercising it here is realistic client behavior, not a test artifact.
    A transport failure (connection refused/reset, empty body from a
    keep-alive race) is genuinely ambiguous about server-side effect;
    retrying with a fresh req_id would risk buying twice, which is the
    exact bug this whole project is about avoiding.
    """
    async with sem:
        start = time.perf_counter()
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                r = await client.post(
                    f"{base_url}/buy", json={"user_id": user_id, "req_id": req_id}, timeout=timeout
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
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    await asyncio.sleep(0.05 * (attempt + 1))
        latency_ms = (time.perf_counter() - start) * 1000
        return RequestResult(
            user_id=user_id,
            req_id=req_id,
            latency_ms=latency_ms,
            http_status=-1,
            body_status=None,
            ticket_number=None,
            error=str(last_error),
        )


async def run_load(base_url, plan, max_inflight, timeout):
    sem = asyncio.Semaphore(max_inflight)
    limits = httpx.Limits(max_connections=max_inflight, max_keepalive_connections=max_inflight)
    wall_start = time.perf_counter()
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [fire_one(client, sem, base_url, u, r, timeout) for (u, r) in plan]
        results = await asyncio.gather(*tasks)
    wall_elapsed = time.perf_counter() - wall_start
    return list(results), wall_elapsed


def percentile(sorted_values, pct):
    if not sorted_values:
        return float("nan")
    k = (len(sorted_values) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def print_perf_summary(results: List[RequestResult], wall_elapsed: float, max_inflight: int) -> None:
    latencies = sorted(r.latency_ms for r in results)
    errors = [r for r in results if r.error]
    print("=== Load Summary ===")
    print(f"Requests fired:        {len(results)}")
    print(f"Max in-flight:         {max_inflight}")
    print(f"Wall-clock duration:   {wall_elapsed:.3f}s")
    print(f"Throughput:            {len(results) / wall_elapsed:.1f} req/s")
    print(f"Transport errors:      {len(errors)}")
    if latencies:
        print(
            f"Latency ms:  min={latencies[0]:.2f}  "
            f"median={statistics.median(latencies):.2f}  "
            f"p95={percentile(latencies, 95):.2f}  "
            f"p99={percentile(latencies, 99):.2f}  "
            f"max={latencies[-1]:.2f}"
        )
    print()
    print("=== Response Breakdown ===")
    counts = Counter(r.body_status or f"transport_error:{r.error}" for r in results)
    for status, count in counts.most_common():
        print(f"  {status:35s} {count}")
    print()


def check_invariants(base_url: str, results: List[RequestResult], timeout: float) -> bool:
    r = httpx.get(f"{base_url}/status", timeout=timeout)
    r.raise_for_status()
    status = r.json()

    total_tickets = status["total_tickets"]
    sold_count = status["sold_count"]
    ticket_numbers_issued = status["ticket_numbers_issued"]
    processed_requests: Dict[str, int] = status["processed_requests"]
    user_tickets: Dict[str, list] = status["user_tickets"]

    findings = []

    inv1_pass = sold_count <= total_tickets and len(ticket_numbers_issued) <= total_tickets
    findings.append((
        "INV1 No overselling (sold_count <= total_tickets)",
        inv1_pass,
        f"sold_count={sold_count} total_tickets={total_tickets} tickets_issued={len(ticket_numbers_issued)}",
    ))

    inv2_pass = len(ticket_numbers_issued) == len(set(ticket_numbers_issued))
    dupes = len(ticket_numbers_issued) - len(set(ticket_numbers_issued))
    findings.append((
        "INV2 No duplicate ticket numbers issued",
        inv2_pass,
        f"issued={len(ticket_numbers_issued)} distinct={len(set(ticket_numbers_issued))} dupes={dupes}",
    ))

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
        f"req_ids_with_conflicting_tickets={len(conflicting)} mismatched_vs_server={len(mismatched_vs_server)}",
    ))

    user_ticket_total = sum(len(v) for v in user_tickets.values())
    inv4_pass = sold_count == len(processed_requests) == user_ticket_total
    findings.append((
        "INV4 Bookkeeping consistent (sold_count == processed_requests == user_tickets)",
        inv4_pass,
        f"sold_count={sold_count} processed_requests={len(processed_requests)} user_ticket_total={user_ticket_total}",
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


async def main_async() -> bool:
    parser = argparse.ArgumentParser(description="Async load client for the distributed ticket seller")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--num-unique", type=int, default=40000)
    parser.add_argument("--max-inflight", type=int, default=500)
    parser.add_argument("--duplicate-fraction", type=float, default=0.1)
    parser.add_argument("--replay-count", type=int, default=3)
    parser.add_argument("--num-users", type=int, default=500)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-reset", action="store_true")
    args = parser.parse_args()

    if not args.no_reset:
        r = httpx.post(f"{args.base_url}/reset", timeout=args.timeout)
        r.raise_for_status()
        print(f"Reset: {r.json()}\n")

    plan = build_request_plan(
        args.num_unique, args.duplicate_fraction, args.replay_count, args.num_users, args.seed
    )
    print(
        f"Plan: {args.num_unique} unique req_ids, ~{args.duplicate_fraction * 100:.0f}% "
        f"replayed {args.replay_count}x each -> {len(plan)} total HTTP requests, "
        f"max_inflight={args.max_inflight}\n"
    )

    results, wall_elapsed = await run_load(args.base_url, plan, args.max_inflight, args.timeout)
    print_perf_summary(results, wall_elapsed, args.max_inflight)
    return check_invariants(args.base_url, results, args.timeout)


def main() -> None:
    passed = asyncio.run(main_async())
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
