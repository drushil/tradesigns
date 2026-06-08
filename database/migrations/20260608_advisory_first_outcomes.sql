-- Advisory-first outcomes schema.
--
-- Context: project pivots to advisory-first. The autonomous signal cycle is
-- being retired. Learning must move from "closed broker trades + EWA weights"
-- to two ground-truth substrates:
--   1. Pick-level outcomes  (was the *signal* good?)            → advisory_signals
--   2. Execution-level outcomes (was the *entry policy* good?)  → advisory_auto_simulations
--                                                                + future advisory paper/live rows in `trades`
--
-- This migration:
--   • adds longer-horizon pick-level outcome columns to advisory_signals (T+5d)
--   • adds entry-policy + r_multiple + closure_reason to advisory_auto_simulations
--   • adds an EOD-close status pair + eod_marked_at for the nightly sim closer
--     (fixes Codex's "stale filled rows past session end" gap, e.g. GOOGL/NFLX)
--   • creates advisory_policy_recommendations for the nightly learner
--   • creates two outcome scoreboards (pick + execution)
--
-- No new table is required for advisory paper/live trades. `trades` already
-- has `trade_source` and `advisory_signal_id` (see 20260602_trade_source_advisory.sql),
-- so paper/live flows slot in by tagging trade_source='advisory_paper'|'advisory_live'.
--
-- Safe to re-run. All adds are guarded with IF NOT EXISTS.

-- ---------------------------------------------------------------------------
-- 1. Pick-level outcomes (advisory_signals)
-- ---------------------------------------------------------------------------

alter table if exists advisory_signals
    add column if not exists forward_return_5d numeric(8,4),
    add column if not exists forward_scored_horizons text[] default '{}'::text[],
    add column if not exists regime_at_pick text,
    add column if not exists session_window text,
    add column if not exists direction_correct_60m boolean,
    add column if not exists direction_correct_5d boolean,
    -- one of: pending | tp1_hit | stop_hit | expired_positive | expired_negative
    add column if not exists pick_outcome_bucket text;

-- Index for the learner: "all picks with at least one horizon scored, grouped
-- by grade/stage/window/regime". The existing forward-scored index covers
-- pending picks; this one covers learning queries.
create index if not exists idx_advisory_signals_outcome_scored
    on advisory_signals (grade, session_window, regime_at_pick, created_at desc)
    where forward_scored_at is not null;

create index if not exists idx_advisory_signals_outcome_bucket
    on advisory_signals (pick_outcome_bucket, grade, created_at desc)
    where pick_outcome_bucket is not null;

-- ---------------------------------------------------------------------------
-- 2. Execution-level outcomes (advisory_auto_simulations)
-- ---------------------------------------------------------------------------

alter table if exists advisory_auto_simulations
    -- Entry-policy classification — today inferable only from `mode` + `alert_stage`
    -- + notes. Materialize it so learning queries don't reparse jsonb.
    -- One of: trade_now | watch_pullback | ignition | chase_tracker
    add column if not exists entry_policy text,
    -- Fill→exit in units of risk: (exit_price - fill_price) / (fill_price - stop_price)
    -- Signed for both sides. The single most useful learning column.
    add column if not exists r_multiple numeric(8,4),
    -- How the row terminated. Decoupled from `status` so we can group by
    -- "why did this end" independently of "what state is it in".
    -- One of: target_1 | target_2 | stop | near_t1_protection | expired_pending |
    --         expired_filled | eod_close | cancelled_weak | manual_dismiss
    add column if not exists closure_reason text,
    -- EOD close support
    add column if not exists eod_marked_at timestamptz,
    add column if not exists eod_close_price numeric(12,4),
    -- Entry quality: where in the band did we get filled? 0 = at entry_min,
    -- 1 = at entry_max, >1 = chased above the band. Null for unfilled rows.
    add column if not exists entry_policy_quality numeric(6,3),
    -- Simulator version tag so old rows don't pollute new learner logic after
    -- simulator behaviour changes. Populated by simulator.py at row-creation time.
    add column if not exists sim_version smallint default 1;

-- New status values used by the EOD closer:
--   closed_eod_win  — fill price below last_price (for BUY); above (for SELL)
--   closed_eod_loss — opposite
-- We don't constrain `status` with a CHECK because the column was already
-- free-text; documenting here instead.

create index if not exists idx_aas_entry_policy
    on advisory_auto_simulations (entry_policy, grade, simulated_at desc)
    where entry_policy is not null;

create index if not exists idx_aas_closure_reason
    on advisory_auto_simulations (closure_reason, grade, closed_at desc)
    where closure_reason is not null;

-- For the nightly EOD closer: find rows that are still `filled` after their
-- session should be over. The closer reads this and marks them closed_eod_*.
create index if not exists idx_aas_eod_pending
    on advisory_auto_simulations (market, fill_at)
    where status = 'filled' and closed_at is null;

-- ---------------------------------------------------------------------------
-- 3. Policy recommendations (new table)
-- ---------------------------------------------------------------------------
-- Output of the nightly advisory learner. NOT auto-applied at first — the
-- config dashboard reads this and the human applies. Once we trust it, an
-- auto-apply path can flip `applied` to true and write to a config table.

create table if not exists advisory_policy_recommendations (
    id bigint generated always as identity primary key,
    computed_at timestamptz not null default now(),
    -- Slice this recommendation applies to.
    -- scope examples: 'grade', 'stage', 'session_window', 'signal', 'global'
    scope text not null,
    scope_value text,           -- e.g. 'B', 'watch_pullback', 'US_OPEN'
    -- recommendation_type: 'threshold' | 'gate' | 'filter' | 'weight'
    recommendation_type text not null,
    field_name text not null,   -- e.g. 'min_composite_score', 'breakout_quality_floor'
    current_value numeric(10,4),
    suggested_value numeric(10,4),
    -- Supporting stats
    sample_size integer not null,
    hit_rate numeric(6,4),              -- 0..1, fraction of picks that hit T1 or +x%
    expected_lift_pct numeric(6,3),     -- modeled lift over current threshold
    confidence numeric(4,3),            -- 0..1, learner's self-reported confidence
    evidence_json jsonb default '{}'::jsonb,
    -- Lifecycle: proposed → accepted | rejected | expired
    -- 'proposed'  = learner output, human has not acted
    -- 'accepted'  = human accepted; if auto-applied, code wrote the config change
    -- 'rejected'  = human dismissed
    -- 'expired'   = superseded by a newer recommendation for the same scope/field
    status text not null default 'proposed',
    status_changed_at timestamptz,
    notes text
);

create index if not exists idx_apr_scope_field
    on advisory_policy_recommendations (scope, scope_value, field_name, computed_at desc);

create index if not exists idx_apr_proposed
    on advisory_policy_recommendations (computed_at desc)
    where status = 'proposed';

alter table advisory_policy_recommendations enable row level security;

-- Read for anon (dashboard), write for service_role.
do $$ begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'advisory_policy_recommendations'
          and policyname = 'apr_read_anon'
    ) then
        create policy apr_read_anon on advisory_policy_recommendations
            for select using (true);
    end if;
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'advisory_policy_recommendations'
          and policyname = 'apr_write_service'
    ) then
        create policy apr_write_service on advisory_policy_recommendations
            for all using (auth.role() = 'service_role')
            with check (auth.role() = 'service_role');
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- 4. Scoreboards (views)
-- ---------------------------------------------------------------------------

-- 4a. Pick-level scoreboard — was the signal itself good?
-- Aggregates over advisory_signals only. Ignores whether anything was traded
-- or simulated; this is the "did our scan logic identify edge" view.
create or replace view advisory_pick_scoreboard as
select
    date_trunc('day', created_at)::date as utc_date,
    market,
    coalesce(session_window, '-')        as session_window,
    coalesce(grade, '-')                  as grade,
    coalesce(regime_at_pick, '-')         as regime,
    side,
    count(*)                              as picks_total,
    count(*) filter (where forward_scored_at is not null) as picks_scored,
    count(*) filter (where target_hit_first = true)       as picks_tp1_hit,
    count(*) filter (where stop_hit_first = true)         as picks_stop_hit,
    count(*) filter (where direction_correct_60m = true)  as picks_dir_correct_60m,
    count(*) filter (where direction_correct_5d = true)   as picks_dir_correct_5d,
    round(avg(forward_return_60m)::numeric, 4)            as avg_fwd_60m,
    round(avg(forward_return_5d)::numeric, 4)             as avg_fwd_5d,
    round(avg(max_favorable_pct)::numeric, 4)             as avg_mfe_pct,
    round(avg(max_adverse_pct)::numeric, 4)               as avg_mae_pct
from advisory_signals
where created_at >= now() - interval '90 days'
group by 1, 2, 3, 4, 5, 6;

-- 4b. Execution-level scoreboard — was the entry policy good?
-- Aggregates over advisory_auto_simulations plus future advisory trades.
-- Today only the sim side is populated; advisory paper/live rows in `trades`
-- will join in once advisory_paper trade_source is live.
create or replace view advisory_execution_scoreboard as
with sim as (
    select
        date_trunc('day', simulated_at)::date as utc_date,
        market,
        coalesce(entry_policy, mode, 'unknown') as entry_policy,
        coalesce(grade, '-')                    as grade,
        side,
        'simulation'::text                      as source,
        count(*)                                as total,
        count(*) filter (where status in ('filled','hit_stop','hit_target_1','hit_target_2','closed_eod_win','closed_eod_loss')) as filled_or_closed,
        count(*) filter (where closure_reason = 'target_1')  as tp1_hit,
        count(*) filter (where closure_reason = 'target_2')  as tp2_hit,
        count(*) filter (where closure_reason = 'stop')      as stopped,
        count(*) filter (where closure_reason = 'eod_close') as eod_closed,
        count(*) filter (where status = 'expired')           as expired,
        count(*) filter (where status = 'cancelled_signal_weak') as cancelled_weak,
        round(avg(r_multiple)::numeric, 3)                   as avg_r,
        round(avg(mfe_pct)::numeric, 3)                      as avg_mfe_pct,
        round(avg(mae_pct)::numeric, 3)                      as avg_mae_pct,
        round(avg(entry_policy_quality)::numeric, 3)         as avg_entry_quality
    from advisory_auto_simulations
    where simulated_at >= now() - interval '90 days'
    group by 1, 2, 3, 4, 5
),
real_trades as (
    select
        date_trunc('day', coalesce(entry_time, created_at))::date as utc_date,
        'US'::text                              as market,
        'real'::text                            as entry_policy,
        coalesce(setup_grade, '-')              as grade,
        side,
        trade_source                            as source,
        count(*)                                as total,
        count(*) filter (where exit_price is not null) as filled_or_closed,
        count(*) filter (where exit_reason ilike '%target%' or exit_reason ilike '%t1%') as tp1_hit,
        0                                       as tp2_hit,
        count(*) filter (where exit_reason ilike '%stop%') as stopped,
        count(*) filter (where exit_reason ilike '%eod%')  as eod_closed,
        0                                       as expired,
        0                                       as cancelled_weak,
        round(avg(r_multiple)::numeric, 3)      as avg_r,
        null::numeric                           as avg_mfe_pct,
        null::numeric                           as avg_mae_pct,
        null::numeric                           as avg_entry_quality
    from trades
    where trade_source like 'advisory%'
      and coalesce(entry_time, created_at) >= now() - interval '90 days'
    group by 1, 2, 3, 4, 5, 6
)
select * from sim
union all
select * from real_trades;

-- ---------------------------------------------------------------------------
-- 5. Backfill helpers (informational, not executed here)
-- ---------------------------------------------------------------------------
-- After this migration applies, a one-shot backfill should run:
--
--   UPDATE advisory_auto_simulations
--   SET entry_policy = mode
--   WHERE entry_policy IS NULL;
--
--   UPDATE advisory_auto_simulations
--   SET closure_reason = CASE status
--       WHEN 'hit_target_1' THEN 'target_1'
--       WHEN 'hit_target_2' THEN 'target_2'
--       WHEN 'hit_stop'     THEN 'stop'
--       WHEN 'hit_near_t1_protection' THEN 'near_t1_protection'
--       WHEN 'expired'      THEN 'expired_pending'
--       WHEN 'cancelled_signal_weak' THEN 'cancelled_weak'
--       ELSE NULL END
--   WHERE closure_reason IS NULL AND status <> 'pending' AND status <> 'filled';
--
-- These are kept out of the migration so re-running is idempotent and so the
-- backfill is reviewable separately.
