-- Initial schema for AnyDown download events.
--
-- There are no users or sessions: the app has no account system.

CREATE TABLE IF NOT EXISTS download_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT,
    requested_height INTEGER,
    status TEXT NOT NULL DEFAULT 'started',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
