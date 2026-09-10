# DECISIONS.md

Problem 1 (ticket-selling stampede): sell exactly N tickets under concurrent demand, no overselling, idempotent retries.

## 1. Architecture chosen — and what was rejected

**Stage 1 — `naive/`**: FastAPI, single uvicorn worker, plain in-memory dict. Deliberately racy: the
idempotency check-then-act and the oversell check-then-act are each two separate steps with a
simulated datastore round-trip between them, so concurrent requests can interleave and both sell.
Purpose: produce a genuine, reproducible failure to fix against, not a strawman.

**Stage 2 — `seller/` (the required fix)**: identical domain logic, wrapped in one process-wide
`threading.Lock` held across the whole check-then-act sequence in `/buy`.
- *Chosen*: a single coarse lock, because Part A's one shared resource is `sold_count` — there's
  nothing to gain from per-resource locking when there's only one resource.
- *Rejected*: per-request/fine-grained locking — same serialization outcome as one lock, more code,
  more ways to get wrong (lock ordering, missed unlocks).
- *Rejected*: optimistic compare-and-swap on the dict — equivalent correctness to a mutex here, but
  harder to prove correct under a time-boxed review; a mutex is verifiable by inspection.
- *Rejected for Part A specifically*: multi-worker/multi-process uvicorn. That reintroduces the exact
  race Part A exists to demonstrate and fix (separate processes don't share `threading.Lock` or
  process memory) — multi-instance correctness is deliberately out of scope for the core ask and
  deferred to the stretch stage below instead of faked with an in-process lock that wouldn't actually
  coordinate across processes.

**Stage 3 (bonus, beyond the ask) — `distributed/`**: nginx (least-conn) → 3 uvicorn replicas (4
workers each) → Postgres as source of truth (`SELECT ... FOR UPDATE` row lock for atomic
decrement-if-positive) + Redis (admission queue via Streams, token-bucket + leaky-bucket rate
limiting, cache-aside for `/status` reads) + a circuit breaker around Redis.
- *Chosen*: Postgres row lock over a distributed lock (e.g. Redlock on Redis) — Postgres is already
  the durable, crash-safe source of truth for ticket counts via MVCC; adding a second locking layer
  on top would be redundant complexity without more correctness.
- *Rejected*: making Redis the source of truth — it isn't persistent by default and a cache/queue that
  can also be "the truth" invites exactly the kind of split-brain this whole problem is about avoiding.
  Redis here is disposable: on flush or outage, `/status` and `/buy` fall back to Postgres directly.

## 2. Trade-offs under the time budget

- `naive/`/`seller/` use an in-memory dict, not a real datastore — deliberate: the ask is to
  demonstrate and fix a concurrency bug, not build a persistence layer.
- `seller/`'s single lock serializes *all* `/buy` calls process-wide, even though only `sold_count`
  and `processed_requests` need protecting — correct, not throughput-optimal. Accepted: the bar is
  "no overselling," not "maximum single-process req/s."
- No pytest suite — correctness is asserted black-box over HTTP by `buyer/`'s load client instead,
  judged higher-value per hour for a bug class that's about concurrent *behavior*, not isolated
  function correctness.
- `distributed/` was a stretch beyond the required minimum; it cost polish time elsewhere (no
  waitlist UX, no automated multi-process load tool) — called out rather than left implicit.
- No waitlist for sold-out buyers in any stage — out of scope for Part A's ask (see §5).

## 3. How it was tested

- `buyer/load_client.py`: black-box HTTP load generator + correctness prober (thread pool). Fires a
  plan of unique `req_id`s plus a deliberately-replayed fraction (simulating client retries), then
  reads `GET /status` and checks four invariants: (1) **no overselling**, `sold_count <=
  total_tickets`; (2) **no duplicate ticket numbers**; (3) **idempotency** — every `req_id` maps to
  exactly one ticket, matching the server's own record, across every response it ever returned; (4)
  **bookkeeping consistency** — `sold_count == len(processed_requests) == total user tickets` (catches
  a double-sell that invariant 1 or 3 alone could miss). Exit code reflects pass/fail — CI-able, not
  just a manual demo.
- `buyer/load_client_async.py`: same four invariants via `httpx`+`asyncio`, for tens of thousands of
  requests against the distributed stack (the thread-pool client tops out in the low thousands before
  OS thread overhead dominates).
- `buyer/race_test.py`: minimal script isolating each race individually, for a quick readable demo.
- The client is pure HTTP in, HTTP out, never inspects internals — the identical script validates
  `naive/`, `seller/`, and `distributed/` unmodified, just pointed at a different port.
- Raw evidence: `results/naive-run-oversold.txt` (101/100 sold, `INV1 FAIL`, exit 1) vs.
  `results/fixed-run-passing.txt` (100/100 sold, all four `PASS`, exit 0) — same load plan, same seed,
  only the server under test differs.

## 4. Where it breaks

- `naive/` breaks by design (that's the point) — see `results/naive-run-oversold.txt`.
- `seller/` is correct for one process, but the global lock is a hard throughput ceiling
  (~1/(2×`DATASTORE_LATENCY_SECONDS`) req/s, ≈166 req/s at the simulated 3ms latency) and gives
  **zero** coordination across more than one worker process or machine — known and intentional, not
  hidden.
- `distributed/`: a p99-latency RCA (`logs/`) found that under sustained concurrency the bottleneck
  was our own single-process load generator pinned at ~100% CPU, not the server stack — so throughput
  above what's recorded in `logs/` is unverified past what our own tooling can generate; a real
  multi-process load tool (k6/locust/wrk2) is needed to find the true ceiling.
- Redis in `distributed/` is a cache/queue, not the source of truth (Postgres wins on disagreement),
  but there's no *automated* reconciliation test — only the manual AOF-durability and Redis-outage
  runs in `logs/03_2026-09-10_distributed-perf-caching-summary.md`.

## 5. What's next with two more weeks

- Swap the hand-rolled load clients for k6/locust/wrk2 to find `distributed/`'s real ceiling.
- Add a waitlist (queue position + notify-on-availability) instead of a hard `sold_out`.
- Add automated pytest coverage around the invariants (currently black-box-only via `buyer/`).
- Structured metrics (Prometheus/Grafana) instead of reading `docker stats` by hand during RCA.
- Chaos test: kill an app replica mid-traffic, confirm nginx failover doesn't lose or double-sell.
- Multi-region / multi-instance stretch beyond the current single-Postgres-primary design.

## 6. Division of work: what I directed vs. what Claude Code wrote

Full unedited transcripts are in `logs/` — this is a summary of who drove what, not a substitute.

- **Spec and API contract**: mine. Exact `/reset`/`/buy` semantics, and specifically which two races
  to bake into Stage 1 on purpose (idempotency check-then-act, oversell check-then-act) and why —
  set before any code existed.
- **Priority order**: mine, stated explicitly — "correctness under concurrency comes first, speed only
  counts once nothing is oversold" — and I deferred load-testing follow-ups until Part B was built
  first rather than letting scope wander.
- **Part B's requirements**: mine — concurrency + duplicate-`req_id` replay, throughput/p50/p99
  reporting, the four invariants, and requiring both the failing and passing run be kept as evidence
  (why `results/` exists).
- **Evidence-over-assumption bar**: mine — I asked "where's the bottleneck, and how do you know rather
  than guess" before accepting any explanation. That standard is what later disproved the RCA's
  *first* hypothesis (Postgres row-lock contention) and forced isolating the real cause.
- **`distributed/`'s shape**: mine — nginx load-balancing across replicas, a hybrid leaky-bucket +
  token-bucket rate limiter, Redis Streams with AOF persistence — specified before any of
  `distributed/app/` was written.
- **The four-phase performance investigation**: mine — baseline, before/after vs. the naive system,
  root-cause the p99/throughput ceiling, then design and build the Redis+Postgres caching fix — in
  that order (`logs/03_2026-09-10_distributed-perf-caching-transcript.md`).
- **Claude Code wrote the majority of the implementation** — handlers, lock/queue/cache/rate-limit
  plumbing, the load-test client, RCA/debugging scripts — under that direction, including its own
  in-task debugging (e.g. tracing a DNS-timeout failure mode during a Redis outage, in
  `logs/01_...-transcript.md`). That work is visible and unedited in `logs/`; credit above is for the
  design and standards, not for lines I didn't personally write.
