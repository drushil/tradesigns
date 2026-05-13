-- Prevent historical closed trades outside the 1-minute data window from
-- occupying the post-exit replay queue after the initial migration.

update trades
set post_exit_checked_at = now(),
    post_exit_result_json = jsonb_build_object(
        'status', 'skipped_historical_backfill',
        'reason', 'older_than_1m_replay_window'
    )
where post_exit_checked_at is null
  and created_at < now() - interval '5 days';
