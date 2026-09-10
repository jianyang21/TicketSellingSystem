-- Postgres schema: the ground truth for ticket state and idempotency.
--
-- tickets_state is a singleton row (id is always 1) holding the counter.
-- sold_tickets is the durable record of every sale; req_id is its PRIMARY
-- KEY, which is the actual UNIQUE constraint enforcing idempotency at the
-- database level -- not just an application-level check.

CREATE TABLE IF NOT EXISTS tickets_state (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    total_tickets INT NOT NULL,
    sold_count INT NOT NULL DEFAULT 0,
    CONSTRAINT singleton_row CHECK (id = 1)
);

INSERT INTO tickets_state (id, total_tickets, sold_count)
VALUES (1, 100, 0)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS sold_tickets (
    req_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    ticket_number INT NOT NULL UNIQUE,
    sold_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- req_id (PK) and ticket_number (UNIQUE) already have implicit indexes,
-- which is also why get_status()'s "ORDER BY ticket_number" is already an
-- indexed scan, not a sort. user_id has no index yet even though it's the
-- natural lookup key for "which tickets does this user hold" -- add it so
-- that query (and the cache-reconciliation path in cache.py, which falls
-- back to a full read of this table) doesn't degrade to a sequential scan
-- as sold_tickets grows. INCLUDE carries ticket_number/sold_at in the
-- index itself so a per-user lookup can be answered without a heap fetch.
CREATE INDEX IF NOT EXISTS idx_sold_tickets_user_id
    ON sold_tickets (user_id) INCLUDE (ticket_number, sold_at);
