-- Backfill historical rows that predate the replay feature so the worker
-- never wastes cycles fetching yfinance data that no longer exists (>4 days old).
-- Rows are marked checked with a sentinel rather than left as NULL.

update trades
set
    post_exit_checked_at = now(),
    post_exit_result_json = '{"skipped": "pre_migration_backfill"}'::jsonb
where
    post_exit_checked_at is null
    and created_at < now() - interval '4 days';

update blocked_opportunities
set
    replay_checked_at = now(),
    replay_result_json = '{"skipped": "pre_migration_backfill"}'::jsonb
where
    replay_checked_at is null
    and created_at < now() - interval '4 days';
