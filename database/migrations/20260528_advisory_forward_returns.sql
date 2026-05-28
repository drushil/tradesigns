-- Forward-return scoreboard for advisory alerts.
-- Filled incrementally by backend.analytics.replay during normal agent cycles.

alter table if exists advisory_signals
    add column if not exists forward_return_5m numeric(8,4),
    add column if not exists forward_return_15m numeric(8,4),
    add column if not exists forward_return_30m numeric(8,4),
    add column if not exists forward_return_60m numeric(8,4),
    add column if not exists forward_scored_at timestamptz,
    add column if not exists advisory_replay_json jsonb default '{}'::jsonb;

create index if not exists idx_advisory_signals_forward_pending
    on advisory_signals (created_at)
    where forward_scored_at is null;

create index if not exists idx_advisory_signals_forward_scorecard
    on advisory_signals (market, mode, status, grade, created_at desc);
