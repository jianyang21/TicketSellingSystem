# AI session transcripts

Full, unedited exports of every Claude Code session used to build this project. These are raw
transcripts (converted from Claude Code's native `.jsonl` session format to readable Markdown —
every user/assistant message kept verbatim; only very large individual tool outputs, e.g. full file
dumps, are truncated with a note, to keep file sizes sane), not summaries or cherry-picked excerpts.
That includes the messy parts: wrong turns, corrections, re-litigated approaches.

They don't map one-to-one onto "stage1 / stage2 / stage3" the way a from-scratch project might,
because Stage 1, Stage 2, and the initial Stage 3 build happened in one continuous working session.
Here's what's actually in each file:

| File | Date | Covers |
|---|---|---|
| `01_2026-09-09_build-naive-fixed-distributed-transcript.md` | 2026-09-09, 16:18–17:51 UTC | The main build session: designing and building `naive/` (Stage 1), demonstrating its races, building `seller/` (Stage 2, the required fix), then starting the `distributed/` bonus stretch (Postgres row-lock, Redis Streams queue, rate limiter, circuit breaker) through an initial correctness pass (AOF durability, Redis-outage behavior, a 48k-request load test). |
| `02_2026-09-09_docker-pause-transcript.md` | 2026-09-09, 17:56 UTC | Trivial housekeeping (pausing Docker containers overnight). Included for completeness — every session is logged, not just the substantive ones. |
| `03_2026-09-10_distributed-perf-caching-transcript.md` | 2026-09-10, 05:28–06:19 UTC | Raw transcript of the p99-latency RCA and Redis+Postgres caching work on `distributed/`. |
| `03_2026-09-10_distributed-perf-caching-summary.md` | 2026-09-10 | A hand-written summary of the same session above (phases, root-cause findings, before/after numbers) — kept alongside the raw transcript as a faster way to read what was found, not a replacement for it. |
| `04_2026-09-10_repo-packaging-transcript.md` | 2026-09-10 | Packaging this repo for submission: fixing an unsafe git-repo location, reorganizing into the required `naive/`/`seller/`/`buyer/`/`results/` layout, capturing the oversold/passing evidence, writing `README.md` and `DECISIONS.md`, exporting these transcripts. |
