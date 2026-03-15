-- Gemini context cache handles
-- Stores references to Gemini cached content objects created during daily cron.
-- Each row maps a (use_case, dining_hall, date) to a Gemini cache name.

CREATE TABLE IF NOT EXISTS gemini_caches (
    id TEXT PRIMARY KEY,
    cache_name TEXT NOT NULL,
    model TEXT NOT NULL,
    university TEXT NOT NULL,
    dining_hall TEXT NOT NULL,
    date TEXT NOT NULL,
    use_case TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gemini_caches_lookup
    ON gemini_caches (dining_hall, university, date, use_case);
