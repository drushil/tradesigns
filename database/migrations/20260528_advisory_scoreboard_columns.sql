-- Additional columns for the advisory forward-return replay engine.
-- These are written by update_advisory_signal_replay() but were missing from
-- the initial 20260528_advisory_forward_returns.sql migration.

alter table if exists advisory_signals
    add column if not exists max_favorable_pct  numeric(8,4),
    add column if not exists max_adverse_pct    numeric(8,4),
    add column if not exists close_after_pct    numeric(8,4),
    add column if not exists replay_checked_at  timestamptz;

-- Index used by the scoreboard query (scored rows ordered by date)
create index if not exists idx_advisory_signals_scoreboard
    on advisory_signals (created_at desc)
    where forward_scored_at is not null;
