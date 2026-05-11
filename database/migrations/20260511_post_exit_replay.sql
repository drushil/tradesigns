-- Post-exit replay metrics for closed trades.
-- Lets the agent measure whether exits left favorable move on the table.

alter table if exists trades
    add column if not exists post_exit_checked_at timestamptz,
    add column if not exists post_exit_horizon_minutes smallint,
    add column if not exists post_exit_max_favorable_pct numeric(8,4),
    add column if not exists post_exit_max_adverse_pct numeric(8,4),
    add column if not exists post_exit_close_after_pct numeric(8,4),
    add column if not exists post_exit_result_json jsonb;

create index if not exists idx_trades_post_exit_replay_pending
    on trades (created_at)
    where post_exit_checked_at is null;

create index if not exists idx_trades_post_exit_reason
    on trades (exit_reason, created_at desc)
    where post_exit_checked_at is not null;
