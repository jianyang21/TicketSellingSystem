# Ticket Stampede — Problem 1

Sell exactly 100 tickets under concurrent demand: no overselling, idempotent retries.
This submission is **Problem 1 only** (not Problem 2 or 3).

- `naive/` — Stage 1: deliberately racy baseline (overselling + idempotency-check races, by design).
- `seller/` — Stage 2: the required fix (single-lock serialized, no in-process races).
- `buyer/` — Part B: the load-testing / correctness-checking client.
- `distributed/` — bonus stretch, not required: nginx + 3 replicas + Postgres + Redis. See
  [`distributed/README` section below](#optional-bonus-distributed-stack) — skip it entirely for the
  core naive-vs-fixed demonstration.
- `logs/` — full AI session transcripts (see `logs/README.md` for what each one covers).
- `results/` — raw output of the failing (naive) and passing (fixed) runs.
- `DECISIONS.md` — architecture, trade-offs, testing methodology, known weak points, next steps.

## Prerequisites

- Python 3.11+ (developed and tested on 3.13.3)
- No database, Docker, or external services needed for `naive/`, `seller/`, or `buyer/`.

## Setup (under a minute)

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

## Run the naive (broken) seller

```bash
uvicorn naive.main:app --host 127.0.0.1 --port 8000
```
Runs on `http://127.0.0.1:8000`. `POST /reset`, `POST /buy` (body: `{"user_id": "...", "req_id": "<uuid>"}`),
`GET /status`.

## Run the fixed seller

```bash
uvicorn seller.main:app --host 127.0.0.1 --port 8000
```
Same API, same port convention — point the load client at whichever one is running.

## Run the load tester / correctness checker

In a second terminal, with the venv active and a seller already running on port 8000:

```bash
python buyer/load_client.py --base-url http://127.0.0.1:8000 \
  --num-unique 300 --concurrency 150 --duplicate-fraction 0.2 --replay-count 3 --num-users 30 --seed 42
```

Prints throughput/latency, then checks four invariants (no overselling, no duplicate tickets,
idempotency holds, bookkeeping consistent) against `GET /status` and exits `0` on pass, `1` on fail.

There's also a minimal single-purpose demo, useful for a quick manual look:

```bash
python buyer/race_test.py   # assumes a server (naive/main.py) already running on :8000
```

## Reproduce the naive-fails / fixed-passes demonstration

```bash
# Terminal 1 — start the naive server
uvicorn naive.main:app --host 127.0.0.1 --port 8000

# Terminal 2 — same load plan against it
python buyer/load_client.py --base-url http://127.0.0.1:8000 \
  --num-unique 300 --concurrency 150 --duplicate-fraction 0.2 --replay-count 3 --num-users 30 --seed 42
# -> INV1 FAIL, sold_count > 100, exit code 1  (see results/naive-run-oversold.txt for a captured run)

# Ctrl+C the naive server, then Terminal 1 — start the fixed server instead
uvicorn seller.main:app --host 127.0.0.1 --port 8000

# Terminal 2 — identical command, identical seed
python buyer/load_client.py --base-url http://127.0.0.1:8000 \
  --num-unique 300 --concurrency 150 --duplicate-fraction 0.2 --replay-count 3 --num-users 30 --seed 42
# -> all four invariants PASS, sold_count == 100, exit code 0  (see results/fixed-run-passing.txt)
```

The only variable that changes between the two runs is which server is under test — same client,
same seed, same load plan.

## Optional: bonus distributed stack

Not required for Problem 1's core ask; included because it was built as a stretch goal. Needs Docker
Desktop.

```bash
cd distributed
docker compose up -d --build
# wait for all 6 containers healthy, then from the repo root:
python buyer/load_client_async.py --base-url http://127.0.0.1:8080 \
  --num-unique 40000 --duplicate-fraction 0.1 --replay-count 3 --max-inflight 500
```

See `DECISIONS.md` and `logs/2026-09-10_distributed_perf_and_caching.md` for what this stage does,
what was measured, and its known limitations.

## Troubleshooting

- **Port 8000 already in use**: stop the other server first (only one seller should be bound to a
  given port at a time), or pass `--port 8001` to uvicorn and `--base-url http://127.0.0.1:8001` to
  the load client.
- **`ModuleNotFoundError`**: confirm the venv is active and `pip install -r requirements.txt`
  completed without errors.
