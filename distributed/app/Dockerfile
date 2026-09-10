FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# RCA follow-up: a single uvicorn worker is one OS process = one event
# loop, pinned to ~1 CPU core regardless of how many the host has. Under
# concurrent load, all 3 replicas together were still only 3 event loops
# servicing hundreds of simultaneous connections, which is what produced
# the multi-second p99 (confirmed by benchmarking a logic-free endpoint
# and seeing the same tail with zero Redis/Postgres involved). --workers
# spawns that many independent worker processes in this container; UVICORN_WORKERS
# is overridable per-deployment via docker-compose without a rebuild.
# Shell form (not exec form) is required here so $UVICORN_WORKERS expands.
CMD uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-4}
