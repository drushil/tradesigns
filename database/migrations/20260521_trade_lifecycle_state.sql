-- Persist intraday lifecycle state across stateless GitHub Actions runs.

alter table if exists open_trades
    add column if not exists breakeven_stop_set boolean default false,
    add column if not exists runner_trail_update_count integer default 0,
    add column if not exists runner_trail_last_update_at timestamptz,
    add column if not exists hold_score_latest numeric(6,4),
    add column if not exists hold_score_min numeric(6,4),
    add column if not exists hold_score_max numeric(6,4),
    add column if not exists trim_done boolean default false;

alter table if exists trades
    add column if not exists breakeven_stop_set boolean default false,
    add column if not exists runner_trail_update_count integer default 0,
    add column if not exists runner_trail_last_update_at timestamptz,
    add column if not exists hold_score_latest numeric(6,4),
    add column if not exists hold_score_min numeric(6,4),
    add column if not exists hold_score_max numeric(6,4),
    add column if not exists trim_done boolean default false;
