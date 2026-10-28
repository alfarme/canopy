-- Scheduled digests replace per-condition Telegram alerts for plant state.
--
-- One row per (kind, local date) records that a digest has been sent. The
-- scheduler claims a run with INSERT ... ON CONFLICT DO NOTHING RETURNING:
-- exactly one caller gets a row back, everyone else gets nothing and skips.
-- That is what makes the 19:00 fire idempotent across a container restart,
-- a redeploy, or two bot instances briefly overlapping during a recreate --
-- all of which are ordinary events here and all of which would otherwise
-- double-post.
--
-- `run_on` is the LOCAL date (Europe/Zurich), not UTC: a 19:00 Zurich digest
-- lands at 17:00 UTC in summer and 18:00 in winter, so keying on the UTC date
-- would be correct by luck rather than by construction.
--
-- `items` is kept even when zero. A claimed run with items = 0 means "checked,
-- nothing to report" and is the reason the scheduler does not retry every
-- minute for the rest of the evening.
CREATE TABLE IF NOT EXISTS digest_runs (
    kind     text        NOT NULL,
    run_on   date        NOT NULL,
    sent_at  timestamptz NOT NULL DEFAULT now(),
    items    integer     NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, run_on)
);

-- "What did the digests say lately" is the only query this table serves.
CREATE INDEX IF NOT EXISTS digest_runs_recent ON digest_runs (sent_at DESC);
