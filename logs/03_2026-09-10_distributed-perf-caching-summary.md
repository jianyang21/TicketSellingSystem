# Session log — distributed ticket-seller: perf investigation, Redis/Postgres caching, RCA fixes

**Date:** 2026-09-10
**Scope:** `distributed/` (Stage 3 — nginx + 3 app replicas + Redis Streams + Postgres)
**Done with:** Claude Code (Sonnet 5)

---

## 1. What was asked, in order

1. Unpause all containers and compare performance (throughput, latency p95/p99, response time, status).
2. Compare against "System A" (Stage 1, the naive single-process baseline) before vs. after, to show improvement.
3. Root-cause the high p99 latency and find ways to raise throughput in the distributed stack.
4. Build a Redis + Postgres architecture for the frequently-read data, index the backing table, make Redis reconcile from Postgres if it isn't persistent, and fix the RCA issues.
5. This log.

---

## 2. What we found and did, by phase

### Phase 1 — Bring the stack up and baseline it
Docker Desktop's engine was actually stopped (not just paused). Started it, then `docker compose up -d` on `distributed/`. All 6 containers (postgres, redis, app1-3, nginx) came up healthy.

Ran `scripts/load_client_async.py` (48,172 requests, 500 max in-flight, 10% of req_ids replayed 3x) against `http://localhost:8080`:

| Metric | Value |
|---|---|
| Throughput | 270.3 req/s |
| Latency p50 / p95 / p99 / max | 1,101 / 6,163 / 10,313 / 29,133 ms |
| Response mix | 42,853 `rate_limited`, 5,217 `sold_out`, 100 `success`, 2 `already_processed` |
| Correctness | PASS (no overselling, no dupes, idempotency held) |

### Phase 2 — "System A" (naive Stage 1) before/after comparison
Ran the naive, single-process, unlocked `main.py` (Stage 1) at the same request scale/concurrency, no rate limiter, no reverse proxy:

| Metric | Stage 1 (naive) | Distributed stack |
|---|---|---|
| Transport errors | **32,208 / 48,172 (67%)** | 0 |
| Overselling | **FAILED — sold 103/100 tickets** | PASSED — exactly 100/100 |
| Throughput | 373.4 req/s (misleading — see below) | 270.3 req/s |

Stage 1's higher "throughput" was mostly fast connection failures, not real work, and it corrupted its own ticket count under the same load. The real finding: correctness under load, not raw req/s, is what actually changed.

### Phase 3 — RCA on p99 latency and throughput ceiling
Initial hypothesis (Postgres row-lock contention) was tested and **disproved**:

| Test | Finding |
|---|---|
| Hammered `atomic_buy()` directly against Postgres, bypassing the whole app | ~160–195 tx/s achievable — 6x+ more than the ~30/s actually used |
| Broke down latency by response status on a live run | Even `rate_limited` (a single cheap Redis check) showed p50 1.2s / p99 13s |
| Hammered logic-free `GET /health` (no Redis, no Postgres) at the same concurrency | Same multi-second tail with zero business logic involved |
| A/B'd uvicorn's default access logging on/off | No measurable difference |
| Checked `docker stats` during a run | `app1` pinned at ~100–110% CPU (one core) while 12 were idle |

**Root cause:** each app replica ran uvicorn with the default single worker process — one OS process, one event loop, effectively bound to one CPU core no matter how many the host has. Under 500 concurrent connections, only 3 single-threaded event loops (one per replica) were servicing them — classic queueing saturation, not a database or business-logic problem. The rate limiter's 30/s token-bucket refill was separately confirmed to be exactly what set the *admission* rate (predicted ~5,340 admissions over the test window vs. 5,319 observed) — working as designed, but far below what Postgres could actually sustain.

### Phase 4 — Built the Redis + Postgres caching architecture
New module `distributed/app/cache.py`: cache-aside read-through for `GET /status`.

- **Read path:** try Redis key `status_cache:v1` first; on any miss (never cached, TTL lapsed, or Redis lost its data because it isn't persistent), recompute the full status from Postgres and repopulate the cache. This *is* the reconciliation mechanism — there's no separate repair job, any kind of cache loss self-heals on the next read.
- **Write path:** every successful ticket sale or `/reset` **invalidates** the cache (doesn't patch it in place), inside the one place ticket state is ever mutated (`db.py`'s `atomic_buy()` / `reset_state()`), so both callers (the queue consumer and the direct-fallback path) get it automatically and can't drift out of sync.
- All Redis calls here are best-effort try/except; a Redis outage just means every read goes straight to Postgres, which is correct anyway since Postgres is the ground truth.

**Verified live**, not just by reading the code:
- `redis-cli FLUSHALL` (simulated non-persistent data loss) → cache key gone → next `/status` returned the exact correct data and repopulated the cache.
- `docker compose stop redis` (Redis fully down) → `/status` still returned correct data straight from Postgres; `/buy` correctly fell back (`path: db_fallback_redis_down`) with the right ticket number, no errors.
- Redis restarted → cache resumed normally.

### Phase 5 — Indexing
Added `idx_sold_tickets_user_id` (covering `ticket_number`, `sold_at`) to `sold_tickets` in `init_db.sql`, and applied it live to the running database. `req_id` (PK) and `ticket_number` (UNIQUE) already had implicit indexes; `user_id` — the natural per-user lookup key, and what the cache-reconciliation path scans on a miss — had none.

### Phase 6 — RCA fixes
| Fix | File(s) | What changed |
|---|---|---|
| Multi-worker ASGI | `app/Dockerfile` | `uvicorn --workers ${UVICORN_WORKERS:-4}` instead of the default single worker; shell-form CMD so the env var expands |
| Unique consumer identity | `main.py` | Redis Streams consumer name is now `INSTANCE_ID-PID`, since multiple uvicorn workers per container share the same `INSTANCE_ID` env var and would otherwise collide in the consumer group |
| Parallel batch consumption | `queue_stream.py` | The consumer's per-batch loop now dispatches all entries concurrently via `asyncio.gather` instead of one-at-a-time |
| Rate limiter headroom | `main.py`, `docker-compose.yml` | Token bucket refill raised 30→80/s, leaky bucket 50→80/s (env-tunable) — Postgres has 6x the headroom the old defaults ever used |
| Connection budget | `db.py`, `docker-compose.yml` | Per-process pool size reduced (20→10 max) since worker count now multiplies it; Postgres `max_connections` raised 100→200 to match |

### Phase 7 — Re-measured, and found a second-order result
Re-ran the identical 48,172-request/500-concurrency test:

| Metric | Before fixes | After fixes |
|---|---|---|
| Throughput | 270.3 req/s | 278.6 req/s |
| Admitted (non-rejected) requests | 5,319 | **13,412** |
| Latency p95 / p99 / max | 6,163 / 10,313 / 29,133 ms | 5,761 / 9,429 / 21,787 ms |
| Correctness | PASS | PASS |

Admitted volume rose ~2.5x, tracking the new rate-limiter math almost exactly. But p95/p99 barely moved despite 4x more server-side capacity — which didn't fit. Checked `docker stats` across every container *during* the run:

| Container | CPU |
|---|---|
| app1 / app2 / app3 | 5–13% |
| nginx | ~5% |
| redis / postgres | 4–7% |
| **load-generator process** | **97–99%** |

**The bottleneck moved from the server to the test client.** The distributed stack now has substantial idle headroom on every tier; the single-process Python/asyncio load generator used throughout this investigation is now the thing pinned at one core, and its own internal queueing is what the "after" p95/p99 numbers were actually measuring. A trustworthy post-fix ceiling would need a multi-process load generator (locust/k6/wrk2) — flagged as the next step, not done here.

One clean, uncontested number that *is* directly attributable to the cache: a `/status` cache hit measured **16ms median**, vs. **32ms** for the forced-reconciliation path right after a Redis flush — at only ~100 rows. That gap is expected to widen as `sold_tickets` grows, since the cache is O(1) per read regardless of table size while the reconciliation query is a full scan.

---

## 3. Files changed

- `distributed/app/cache.py` — new: Redis cache-aside + reconciliation for `GET /status`
- `distributed/app/db.py` — cache invalidation wired into the single mutation point; pool sizing made configurable
- `distributed/app/main.py` — cache wiring for `/status`/`/reset`/`/buy`; unique per-worker consumer names; env-tunable rate limiter and pool sizes
- `distributed/app/queue_stream.py` — parallelized per-batch consumer processing
- `distributed/app/Dockerfile` — multi-worker uvicorn
- `distributed/init_db.sql` — new index on `sold_tickets(user_id)`
- `distributed/docker-compose.yml` — new env vars (`UVICORN_WORKERS`, `DB_POOL_*`, `RATE_LIMIT_*`), Postgres `max_connections=200`
- `scripts/load_client_async.py` — added p95 alongside the existing p99 in the summary output

---

## 4. Net improvement — honest summary

**Unambiguous wins:**
- **Correctness under load**: the naive baseline oversold tickets (103/100) and dropped 67% of requests under the same burst; the distributed stack has never oversold or lost a request across every test run in this session, including with Redis fully down.
- **Legitimate throughput admitted**: ~2.5x more real ticket-selling traffic let through (5,319 → 13,412 admitted requests over the same test window), by design, with Postgres's ~190 tx/s ceiling confirmed to have another ~6x of headroom beyond that if the rate limiter is opened further.
- **Resilience**: Redis losing its data (flushed or fully down) is now a correctly-handled, tested case — `/status` and `/buy` both keep serving correct answers straight from Postgres, and the cache self-heals on the next read with no manual repair step.
- **Read cost**: a `/status` cache hit is ~2x faster than the reconciliation path at current (small) data volume, and that gap only grows as the ticket ledger grows, since the cache no longer pays for a full table scan on every read.
- **Server headroom**: every server-side container now runs at 5–13% CPU under the load that used to peg the single-worker app processes at 100% — there is substantial unused capacity for a bigger burst than has been tested.

**Not yet proven, and why:**
- p95/p99 tail latency under the standard 500-concurrency benchmark barely moved (10.3s → 9.4s p99), *not* because the fixes didn't work, but because the single-process load generator became the bottleneck instead of the server (confirmed via `docker stats`: server tiers idle, load-gen process pinned at ~98% CPU). The true post-fix latency ceiling is currently unmeasured — a multi-process load generator is needed to get a trustworthy number, and is the natural next step.
