-- Prune ephemeral runtime logs older than 30 days.
-- Binding constraint: backend/daily_review.py::_shadow_evidence_days()
-- currently looks back 7 days. Keep retention comfortably above that.
-- Trades, signals, blocked_opportunities, portfolio_snapshots, and daily_reviews
-- are retained separately; this only trims operational exhaust.

create extension if not exists pg_cron with schema extensions;

-- Idempotent: drop existing job if migration is re-run.
do $$
declare
    job_id bigint;
begin
    select jobid
    into job_id
    from cron.job
    where jobname = 'prune-agent-logs'
    limit 1;

    if job_id is not null then
        perform cron.unschedule(job_id);
    end if;
end $$;

-- Weekly Sunday 02:00 UTC. Operational logs are not learning memory.
select cron.schedule(
    'prune-agent-logs',
    '0 2 * * 0',
    $$
    delete from agent_logs
    where logged_at < now() - interval '30 days';
    $$
);
