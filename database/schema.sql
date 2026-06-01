-- ============================================================
-- TRADING AGENT — SUPABASE SCHEMA (v2)
-- Applies Supabase agent-skills best practices:
--   ✓ bigint identity PKs (not random UUID — avoids index fragmentation)
--   ✓ RLS enabled + anon read policies (Streamlit dashboard)
--   ✓ Composite indexes on common query patterns
--   ✓ numeric for money, real for scores, smallint for counters
--   ✓ Check constraints on bounded columns
--   ✓ Generated columns, useful views
-- Run: Supabase → SQL Editor → New Query → Run
-- ============================================================

create extension if not exists pg_stat_statements;

-- 1. TRADES
create table if not exists trades (
    id              bigint generated always as identity primary key,
    created_at      timestamptz not null default now(),
    ticker          text        not null,
    side            text        not null check (side in ('BUY','SELL')),
    entry_price     numeric(12,4),
    exit_price      numeric(12,4),
    quantity        numeric(14,6),
    submitted_qty   numeric(14,6),
    implied_qty     numeric(14,6),
    stop_price      numeric(12,4),
    take_profit_price numeric(12,4),
    intended_size_eur numeric(10,2),
    executed_size_eur numeric(10,2),
    executed_size_usd numeric(10,2),
    bracket_floor_qty_loss_pct numeric(8,4),
    size_eur        numeric(10,2),
    size_usd        numeric(10,2),
    pnl_pct         numeric(8,4),
    net_pnl_pct     numeric(8,4),
    pnl_eur         numeric(10,2),
    entry_time      timestamptz,
    exit_time       timestamptz,
    hold_minutes    smallint,
    exit_reason     text check (exit_reason in (
                        'stop_loss','take_profit','time_exit',
                        'signal_reversal','manual','circuit_breaker')),
    regime          text check (regime in (
                        'trending','ranging','high_vol','news_driven')),
    macro_regime    text check (macro_regime in (
                        'geopolitical_shock','rate_shift','normal','risk_off','risk_on')),
    macro_multiplier numeric(5,2),
    dip_type        text,
    sizing_json     jsonb,
    mean_reversion_trade boolean default false,
    swing_trade     boolean default false,
    exposure_direction text,
    strategy_family text,
    regime_debug_json jsonb,
    composite_score numeric(6,4) check (composite_score between -1 and 1),
    llm_conviction  numeric(6,4) check (llm_conviction between 0 and 1),
    llm_rationale   text,
    signals_json    jsonb,
    order_id        text,
    client_order_id text,
    close_order_id  text,
    close_error     text,
    commission_eur  numeric(8,4) default 0,
    slippage_eur    numeric(8,4) default 0,
    llm_cost_eur    numeric(8,5) default 0,
    risk_profile    text,
    horizon         text check (horizon in ('short','mid','both','swing','intraday'))
);

create index if not exists idx_trades_created_at  on trades (created_at desc);
create index if not exists idx_trades_ticker_time on trades (ticker, created_at desc);
create index if not exists idx_trades_regime      on trades (regime);
create index if not exists idx_trades_pnl         on trades (net_pnl_pct)
    where net_pnl_pct is not null;

alter table if exists trades
    add column if not exists size_usd numeric(10,2),
    add column if not exists submitted_qty numeric(14,6),
    add column if not exists implied_qty numeric(14,6),
    add column if not exists intended_size_eur numeric(10,2),
    add column if not exists executed_size_eur numeric(10,2),
    add column if not exists executed_size_usd numeric(10,2),
    add column if not exists bracket_floor_qty_loss_pct numeric(8,4),
    add column if not exists exposure_direction text,
    add column if not exists strategy_family text,
    add column if not exists regime_debug_json jsonb,
    add column if not exists breakeven_stop_set boolean default false,
    add column if not exists runner_trail_update_count integer default 0,
    add column if not exists runner_trail_last_update_at timestamptz,
    add column if not exists hold_score_latest numeric(6,4),
    add column if not exists hold_score_min numeric(6,4),
    add column if not exists hold_score_max numeric(6,4),
    add column if not exists trim_done boolean default false;

alter table if exists trades
    add column if not exists client_order_id text;

-- 2. OPEN_TRADES
create table if not exists open_trades (
    id                  bigint generated always as identity primary key,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    entry_time          timestamptz,
    closed_at           timestamptz,
    ticker              text not null unique,
    side                text not null check (side in ('BUY','SELL')),
    entry_price         numeric(12,4),
    quantity            numeric(14,6),
    submitted_qty       numeric(14,6),
    implied_qty         numeric(14,6),
    stop_price          numeric(12,4),
    take_profit_price   numeric(12,4),
    hold_minutes        smallint,
    hold_days           smallint,
    horizon             text check (horizon in ('short','mid','both','swing','intraday',null)),
    size_eur            numeric(10,2),
    size_usd            numeric(10,2),
    intended_size_eur   numeric(10,2),
    executed_size_eur   numeric(10,2),
    executed_size_usd   numeric(10,2),
    bracket_floor_qty_loss_pct numeric(8,4),
    order_id            text,
    client_order_id     text,
    status              text not null default 'open' check (status in ('open','closed')),
    close_reason        text,
    regime              text,
    macro_regime        text,
    macro_multiplier    numeric(5,2),
    dip_type            text,
    sizing_json         jsonb,
    mean_reversion_trade boolean default false,
    swing_trade         boolean default false,
    exposure_direction  text,
    strategy_family     text,
    regime_debug_json   jsonb,
    composite_score     numeric(6,4),
    llm_conviction      numeric(6,4),
    llm_rationale       text,
    signals_json        jsonb default '{}'::jsonb
);

create index if not exists idx_open_trades_status on open_trades (status, created_at desc);

alter table if exists open_trades
    add column if not exists entry_time timestamptz,
    add column if not exists size_usd numeric(10,2),
    add column if not exists quantity numeric(14,6),
    add column if not exists submitted_qty numeric(14,6),
    add column if not exists implied_qty numeric(14,6),
    add column if not exists intended_size_eur numeric(10,2),
    add column if not exists executed_size_eur numeric(10,2),
    add column if not exists executed_size_usd numeric(10,2),
    add column if not exists bracket_floor_qty_loss_pct numeric(8,4),
    add column if not exists hold_minutes smallint,
    add column if not exists hold_days smallint,
    add column if not exists horizon text,
    add column if not exists macro_regime text,
    add column if not exists macro_multiplier numeric(5,2),
    add column if not exists dip_type text,
    add column if not exists sizing_json jsonb,
    add column if not exists mean_reversion_trade boolean default false,
    add column if not exists swing_trade boolean default false,
    add column if not exists exposure_direction text,
    add column if not exists strategy_family text,
    add column if not exists regime_debug_json jsonb,
    add column if not exists breakeven_stop_set boolean default false,
    add column if not exists runner_trail_update_count integer default 0,
    add column if not exists runner_trail_last_update_at timestamptz,
    add column if not exists hold_score_latest numeric(6,4),
    add column if not exists hold_score_min numeric(6,4),
    add column if not exists hold_score_max numeric(6,4),
    add column if not exists trim_done boolean default false;

alter table if exists open_trades
    add column if not exists client_order_id text;

-- 3. SIGNALS
create table if not exists signals (
    id                      bigint generated always as identity primary key,
    created_at              timestamptz not null default now(),
    ticker                  text not null,
    composite_score         numeric(6,4) check (composite_score between -1 and 1),
    order_book_score        real,
    tape_aggression_score   real,
    rsi_divergence_score    real,
    news_sentiment_score    real,
    vwap_deviation_score    real,
    macd_score              real,
    rel_strength_score      real,
    bollinger_score         real,
    put_call_score          real,
    atr_pct                 numeric(6,4),
    earnings_days           smallint,
    earnings_mult           numeric(4,2) default 1.0,
    regime                  text,
    macro_regime            text check (macro_regime in (
                                'geopolitical_shock','rate_shift','normal','risk_off','risk_on')),
    macro_multiplier        numeric(5,2),
    regime_bull_bear        text check (regime_bull_bear in ('bull','bear','transitioning')),
    shock_detected          boolean default false,
    shock_classification    text,
    action_hint             text check (action_hint in ('BUY','SELL','HOLD',null)),
    exposure_direction      text,
    strategy_family         text,
    regime_debug_json       jsonb,
    vix                     numeric(6,2),
    volume_vs_avg           numeric(6,2),
    gated                   boolean not null default false,
    gate_reason             text,
    llm_called              boolean not null default false,
    llm_action              text check (llm_action in ('BUY','SELL','HOLD',null)),
    llm_conviction          real
);

create index if not exists idx_signals_created_at  on signals (created_at desc);
create index if not exists idx_signals_ticker_time on signals (ticker, created_at desc);
create index if not exists idx_signals_active      on signals (created_at desc)
    where gated = false;

alter table if exists signals
    add column if not exists action_hint text,
    add column if not exists exposure_direction text,
    add column if not exists strategy_family text,
    add column if not exists regime_debug_json jsonb;

-- 4. NEWS_CACHE
create table if not exists news_cache (
    id                bigint generated always as identity primary key,
    ticker            text not null unique,
    fetched_at        timestamptz not null default now(),
    sentiment_score   numeric(6,4) not null check (sentiment_score between -1 and 1),
    meta_json         jsonb not null default '{}'::jsonb,
    headlines_json    jsonb not null default '[]'::jsonb
);

create index if not exists idx_news_cache_ticker_time
    on news_cache (ticker, fetched_at desc);

create table if not exists newsapi_usage (
    id            bigint generated always as identity primary key,
    usage_date    date not null,
    ticker        text not null,
    calls         integer not null default 0 check (calls >= 0),
    updated_at    timestamptz not null default now(),
    unique (usage_date, ticker)
);

create index if not exists idx_newsapi_usage_date
    on newsapi_usage (usage_date desc);

create table if not exists ticker_profiles (
    id              bigint generated always as identity primary key,
    ticker          text not null unique,
    updated_at      timestamptz not null default now(),
    profile_json    jsonb not null default '{}'::jsonb
);

create index if not exists idx_ticker_profiles_ticker
    on ticker_profiles (ticker);

-- 5. SIGNAL_WEIGHTS
create table if not exists signal_weights (
    id              bigint generated always as identity primary key,
    updated_at      timestamptz not null default now(),
    regime          text not null default 'global',
    order_book      numeric(6,4) not null check (order_book between 0 and 1),
    tape_aggression numeric(6,4) not null check (tape_aggression between 0 and 1),
    rsi_divergence  numeric(6,4) not null check (rsi_divergence between 0 and 1),
    news_sentiment  numeric(6,4) not null check (news_sentiment between 0 and 1),
    vwap_deviation  numeric(6,4) not null check (vwap_deviation between 0 and 1),
    macd_crossover  numeric(6,4) not null default 0.10 check (macd_crossover between 0 and 1),
    relative_strength numeric(6,4) not null default 0.08 check (relative_strength between 0 and 1),
    trade_count     integer,
    trigger         text check (trigger in (
                        'trade_update','weekly_review','manual','init'))
);

create index if not exists idx_weights_regime_time
    on signal_weights (regime, updated_at desc);

-- Idempotent upgrades for existing databases created before the 8-signal engine.
alter table if exists signals
    add column if not exists macd_score real,
    add column if not exists rel_strength_score real,
    add column if not exists earnings_days smallint,
    add column if not exists earnings_mult numeric(4,2) default 1.0;

alter table if exists signal_weights
    add column if not exists macd_crossover numeric(6,4) not null default 0.10 check (macd_crossover between 0 and 1),
    add column if not exists relative_strength numeric(6,4) not null default 0.08 check (relative_strength between 0 and 1);

-- Phase 1 upgrade: Bollinger squeeze, Put/Call ratio, ATR columns.
alter table if exists signals
    add column if not exists bollinger_score  real,
    add column if not exists put_call_score   real,
    add column if not exists atr_pct          numeric(6,4),
    add column if not exists macro_regime     text,
    add column if not exists macro_multiplier numeric(5,2);

alter table if exists trades
    add column if not exists stop_price numeric(12,4),
    add column if not exists take_profit_price numeric(12,4),
    add column if not exists order_id text,
    add column if not exists close_order_id text,
    add column if not exists close_error text,
    add column if not exists macro_regime text,
    add column if not exists macro_multiplier numeric(5,2),
    add column if not exists dip_type text,
    add column if not exists sizing_json jsonb,
    add column if not exists mean_reversion_trade boolean default false,
    add column if not exists swing_trade boolean default false;

alter table if exists signals
    add column if not exists regime_bull_bear text,
    add column if not exists shock_detected boolean default false,
    add column if not exists shock_classification text;

alter table if exists signal_weights
    add column if not exists bollinger_squeeze numeric(6,4) not null default 0.09 check (bollinger_squeeze between 0 and 1),
    add column if not exists put_call_ratio    numeric(6,4) not null default 0.05 check (put_call_ratio    between 0 and 1);

-- 6. LEARNINGS
create table if not exists learnings (
    id              bigint generated always as identity primary key,
    created_at      timestamptz not null default now(),
    week_start      date not null,
    insights_json   jsonb not null default '[]'::jsonb,
    trades_analysed integer not null default 0,
    applied         boolean not null default false
);

create index if not exists idx_learnings_week on learnings (week_start desc);

-- 7. PORTFOLIO_SNAPSHOTS
create table if not exists portfolio_snapshots (
    id                  bigint generated always as identity primary key,
    snapshot_at         timestamptz not null default now(),
    total_value_eur     numeric(12,2) not null,
    cash_eur            numeric(12,2) not null,
    invested_eur        numeric(12,2) generated always as
                            (total_value_eur - cash_eur) stored,
    daily_pnl_pct       numeric(8,4),
    cumulative_pnl_pct  numeric(8,4),
    drawdown_pct        numeric(8,4) default 0,
    open_positions      jsonb        default '[]'::jsonb,
    trades_today        smallint     default 0,
    llm_calls_today     smallint     default 0,
    llm_cost_today      numeric(8,5) default 0
);

create index if not exists idx_snapshots_at on portfolio_snapshots (snapshot_at desc);

-- 8. AGENT_LOGS
create table if not exists agent_logs (
    id          bigint generated always as identity primary key,
    logged_at   timestamptz not null default now(),
    level       text not null check (level in (
                    'INFO','WARN','ERROR','TRADE','SIGNAL','LEARNING')),
    event       text not null,
    detail      jsonb default '{}'::jsonb
);

create index if not exists idx_logs_time       on agent_logs (logged_at desc);
create index if not exists idx_logs_level_time on agent_logs (level, logged_at desc);
create index if not exists idx_logs_errors     on agent_logs (logged_at desc)
    where level in ('ERROR','WARN');

-- 9. BLOCKED_OPPORTUNITIES
create table if not exists blocked_opportunities (
    id                      bigint generated always as identity primary key,
    created_at              timestamptz not null default now(),
    ticker                  text not null,
    action_hint             text check (action_hint in ('BUY','SELL','HOLD',null)),
    composite_score         numeric(6,4),
    block_stage             text not null check (block_stage in (
                                'gate','ev','ranking','llm','conviction','price',
                                'signal_consensus','reward_risk','exposure',
                                'signal_alignment','regime','time','sizing','position',
                                'entry_quality')),
    block_reason            text,
    block_detail            jsonb default '{}'::jsonb,
    candidate_rank_score    numeric(7,4),
    breakout_quality        numeric(6,4),
    ev_decision             text,
    ev_net_pct              numeric(8,4),
    ev_result_json          jsonb default '{}'::jsonb,
    signals_json            jsonb default '{}'::jsonb,
    setup_context_json      jsonb default '{}'::jsonb,
    regime                  text,
    market_regime           text,
    strategy_family         text,
    event_risk_active       boolean default false,
    reference_price         numeric(12,4),
    replay_checked_at       timestamptz,
    max_favorable_pct       numeric(8,4),
    max_adverse_pct         numeric(8,4),
    close_after_pct         numeric(8,4),
    replay_result_json      jsonb default '{}'::jsonb
);

create index if not exists idx_blocked_opportunities_time
    on blocked_opportunities (created_at desc);
create index if not exists idx_blocked_opportunities_ticker_time
    on blocked_opportunities (ticker, created_at desc);
create index if not exists idx_blocked_opportunities_stage
    on blocked_opportunities (block_stage, created_at desc);

-- Observability-only playbook evidence tags.
alter table if exists signals
    add column if not exists playbook text,
    add column if not exists playbook_lifecycle text,
    add column if not exists session_window text,
    add column if not exists primary_factor text,
    add column if not exists factor_bucket text,
    add column if not exists regime_key text,
    add column if not exists data_quality_state text,
    add column if not exists data_quality_json jsonb default '{}'::jsonb,
    add column if not exists cost_estimate_json jsonb default '{}'::jsonb,
    add column if not exists estimated_spread_pct numeric(8,4),
    add column if not exists estimated_total_cost_pct numeric(8,4);

alter table if exists blocked_opportunities
    add column if not exists playbook text,
    add column if not exists playbook_lifecycle text,
    add column if not exists session_window text,
    add column if not exists primary_factor text,
    add column if not exists factor_bucket text,
    add column if not exists regime_key text,
    add column if not exists data_quality_state text,
    add column if not exists data_quality_json jsonb default '{}'::jsonb,
    add column if not exists cost_estimate_json jsonb default '{}'::jsonb,
    add column if not exists estimated_spread_pct numeric(8,4),
    add column if not exists estimated_total_cost_pct numeric(8,4);

alter table if exists open_trades
    add column if not exists playbook text,
    add column if not exists playbook_lifecycle text,
    add column if not exists session_window text,
    add column if not exists primary_factor text,
    add column if not exists factor_bucket text,
    add column if not exists regime_key text,
    add column if not exists data_quality_state text,
    add column if not exists data_quality_json jsonb default '{}'::jsonb,
    add column if not exists cost_estimate_json jsonb default '{}'::jsonb,
    add column if not exists estimated_spread_pct numeric(8,4),
    add column if not exists estimated_total_cost_pct numeric(8,4);

alter table if exists trades
    add column if not exists playbook text,
    add column if not exists playbook_lifecycle text,
    add column if not exists session_window text,
    add column if not exists primary_factor text,
    add column if not exists factor_bucket text,
    add column if not exists regime_key text,
    add column if not exists data_quality_state text,
    add column if not exists data_quality_json jsonb default '{}'::jsonb,
    add column if not exists cost_estimate_json jsonb default '{}'::jsonb,
    add column if not exists estimated_spread_pct numeric(8,4),
    add column if not exists estimated_total_cost_pct numeric(8,4);

create index if not exists idx_signals_playbook_time
    on signals (playbook, created_at desc)
    where playbook is not null;

create index if not exists idx_blocked_playbook_time
    on blocked_opportunities (playbook, created_at desc)
    where playbook is not null;

create index if not exists idx_trades_playbook_time
    on trades (playbook, created_at desc)
    where playbook is not null;

create index if not exists idx_open_trades_factor
    on open_trades (primary_factor, status)
    where primary_factor is not null;

-- ── RLS ──────────────────────────────────────────────────────────────────────
-- anon role = Streamlit dashboard (read-only, anon key)
-- service_role = agent backend (bypasses RLS, write access)

alter table trades              enable row level security;
alter table open_trades         enable row level security;
alter table signals             enable row level security;
alter table signal_weights      enable row level security;
alter table news_cache          enable row level security;
alter table newsapi_usage       enable row level security;
alter table ticker_profiles     enable row level security;
alter table learnings           enable row level security;
alter table portfolio_snapshots enable row level security;
alter table agent_logs          enable row level security;
alter table blocked_opportunities enable row level security;

create policy "anon read trades"             on trades             for select to anon using (true);
create policy "anon read open_trades"        on open_trades        for select to anon using (true);
create policy "anon read signals"            on signals            for select to anon using (true);
create policy "anon read signal_weights"     on signal_weights     for select to anon using (true);
create policy "anon read news_cache"         on news_cache         for select to anon using (true);
create policy "anon read newsapi_usage"      on newsapi_usage      for select to anon using (true);
create policy "anon read ticker_profiles"    on ticker_profiles    for select to anon using (true);
create policy "anon read learnings"          on learnings          for select to anon using (true);
create policy "anon read portfolio_snapshots" on portfolio_snapshots for select to anon using (true);
create policy "anon read agent_logs"         on agent_logs         for select to anon using (true);
create policy "anon read blocked_opportunities" on blocked_opportunities for select to anon using (true);

-- ── Views ─────────────────────────────────────────────────────────────────────

create or replace view trade_stats_30d as
select
    count(*)                                                    as total_trades,
    count(*) filter (where net_pnl_pct > 0)                    as wins,
    count(*) filter (where net_pnl_pct <= 0)                   as losses,
    round(count(*) filter (where net_pnl_pct > 0)::numeric
          / nullif(count(*), 0) * 100, 1)                      as win_rate_pct,
    round(avg(net_pnl_pct), 4)                                 as avg_net_pnl_pct,
    round(sum(pnl_eur), 2)                                     as total_pnl_eur,
    round(avg(hold_minutes), 1)                                as avg_hold_minutes,
    round(sum(commission_eur + slippage_eur + llm_cost_eur), 4) as total_costs_eur
from trades
where created_at >= now() - interval '30 days';

grant select on trade_stats_30d to anon;

create or replace view regime_performance as
select
    regime,
    count(*)                                                    as trade_count,
    round(avg(net_pnl_pct), 4)                                 as avg_net_pnl_pct,
    round(count(*) filter (where net_pnl_pct > 0)::numeric
          / nullif(count(*), 0) * 100, 1)                      as win_rate_pct,
    round(sum(pnl_eur), 2)                                     as total_pnl_eur
from trades
where regime is not null and created_at >= now() - interval '30 days'
group by regime
order by avg_net_pnl_pct desc;

grant select on regime_performance to anon;

drop view if exists latest_signal_weights;

create view latest_signal_weights as
select distinct on (regime)
    regime,
    order_book,
    tape_aggression,
    rsi_divergence,
    news_sentiment,
    vwap_deviation,
    coalesce(macd_crossover,    0.10) as macd_crossover,
    coalesce(relative_strength,  0.08) as relative_strength,
    coalesce(bollinger_squeeze,  0.09) as bollinger_squeeze,
    coalesce(put_call_ratio,     0.05) as put_call_ratio,
    trade_count,
    updated_at
from signal_weights
order by regime, updated_at desc;

grant select on latest_signal_weights to anon;

-- ── Cash sweeps (BROKER_ENV-gated, simulation on alpaca_paper) ────────────────

create table if not exists cash_sweeps (
    id               bigint generated always as identity primary key,
    broker_env       text,
    sweep_ticker     text,
    sweepable_eur    numeric(10,2),
    reserve_eur      numeric(10,2),
    est_daily_yield  numeric(10,4),
    est_annual_yield numeric(10,2),
    executed         boolean default false,
    mode             text,
    skip_reason      text,
    sim_note         text,
    error            text,
    should_sweep     boolean,
    executed_at      timestamptz default now()
);

alter table cash_sweeps enable row level security;
create policy "anon read cash_sweeps" on cash_sweeps for select to anon using (true);
grant select on cash_sweeps to anon;
create index if not exists idx_cash_sweeps_executed_at on cash_sweeps (executed_at desc);

-- ── Dividend opportunities (advisory overlay, logged when score > 0.5) ────────

create table if not exists dividend_opportunities (
    id               bigint generated always as identity primary key,
    broker_env       text,
    ticker           text,
    next_ex_date     date,
    days_to_ex       smallint,
    dividend_amount  numeric(8,4),
    dividend_yield   numeric(6,2),
    opportunity_score numeric(4,3),
    action_taken     text default 'logged_only',
    scanned_at       timestamptz default now()
);

alter table dividend_opportunities enable row level security;
create policy "anon read dividend_opportunities" on dividend_opportunities for select to anon using (true);
grant select on dividend_opportunities to anon;
create index if not exists idx_dividend_opportunities_scanned_at on dividend_opportunities (scanned_at desc, ticker);

-- ── portfolio_snapshots: sweep tracking columns ───────────────────────────────

alter table if exists portfolio_snapshots
    add column if not exists sweep_active      boolean default false,
    add column if not exists sweep_ticker      text,
    add column if not exists sweep_value_eur   numeric(10,2),
    add column if not exists sim_yield_ytd_eur numeric(10,4);

-- ── Momentum swing trading columns (Task 6 + Modification 4) ─────────────────

-- trades: swing metadata and promotion tracking
alter table if exists trades
    add column if not exists swing_conviction       numeric(4,3),
    add column if not exists swing_reasons          text[],
    add column if not exists hold_days_actual       smallint,
    add column if not exists stop_multiplier        numeric(4,2),
    add column if not exists daily_reeval_count     smallint default 0,
    add column if not exists exit_trigger           text,
    add column if not exists promoted_to_swing      boolean,
    add column if not exists promoted_at            timestamptz,
    add column if not exists initial_horizon        text,
    add column if not exists trailing_stop_price    numeric(12,4),
    add column if not exists highest_price_since_entry numeric(12,4),
    add column if not exists overnight_gap_pct      numeric(8,4),
    add column if not exists protective_stop_order_id text,
    add column if not exists hold_extension_count  smallint default 0,
    add column if not exists hold_decision_json     jsonb;

-- signals: swing detection results at signal time
alter table if exists signals
    add column if not exists swing_detected    boolean,
    add column if not exists swing_conviction  numeric(4,3),
    add column if not exists swing_hold_days   smallint;

-- open_trades: persist swing state across agent restarts
alter table if exists open_trades
    add column if not exists promoted_to_swing          boolean default false,
    add column if not exists promoted_at                timestamptz,
    add column if not exists swing_conviction           numeric(4,3),
    add column if not exists swing_reasons              text[],
    add column if not exists highest_price_since_entry  numeric(12,4),
    add column if not exists trailing_stop_price        numeric(12,4),
    add column if not exists stop_multiplier            numeric(4,2),
    add column if not exists stop_pct                   numeric(6,4),
    add column if not exists max_hold_minutes           smallint,
    add column if not exists daily_reeval_count         smallint default 0,
    add column if not exists hold_extension_count       smallint default 0,
    add column if not exists hold_decision_json         jsonb,
    add column if not exists peak_directional_score     numeric(6,4) default 0,
    add column if not exists initial_horizon            text,
    add column if not exists protective_stop_order_id   text;

-- Update exit_reason constraint to include momentum-swing exit types
alter table if exists trades drop constraint if exists trades_exit_reason_check;
alter table if exists trades
    add constraint trades_exit_reason_check check (exit_reason in (
        'stop_loss','take_profit','time_exit',
        'signal_reversal','manual','circuit_breaker',
        'chandelier_stop','swing_exit','earnings_tomorrow',
        'regime_turned_bear','momentum_reversed',
        'macro_shock','take_profit_8pct','swing_promoted',
        'momentum_peak_decay','eod_cleanup',
        'thesis_invalidated','partial_runner_stop','a_plus_override'
    ));

-- ============================================================
-- MIGRATION v3 — Grading engine + ORB + adaptive percentiles
-- Run in Supabase SQL Editor after v2 schema is applied.
-- ============================================================

-- 1. signal_percentiles — rolling composite window per ticker
create table if not exists signal_percentiles (
    id                  bigint generated always as identity primary key,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    ticker              text not null unique,
    sample_count        integer default 0,
    p50                 numeric(8,4),
    p70                 numeric(8,4),
    p85                 numeric(8,4),
    p90                 numeric(8,4),
    p95                 numeric(8,4),
    window_composites   jsonb default '[]'::jsonb
);

create index if not exists idx_signal_percentiles_ticker on signal_percentiles (ticker);

alter table if exists signal_percentiles enable row level security;
do $$ begin
  if not exists (
    select 1 from pg_policies
    where tablename = 'signal_percentiles'
      and policyname = 'anon_read_signal_percentiles'
  ) then
    execute 'create policy "anon_read_signal_percentiles"
      on signal_percentiles for select using (true)';
  end if;
end $$;

-- 2. open_trades — grade metadata + partial exit tracking
alter table if exists open_trades
    add column if not exists setup_grade            text check (setup_grade in ('A+','A','B','C',null)),
    add column if not exists sector_confirmation    numeric(4,3),
    add column if not exists percentile_rank        numeric(5,1),
    add column if not exists grade_reasons          jsonb default '[]'::jsonb,
    add column if not exists partial_target_price   numeric(12,4),
    add column if not exists partial_exit_pct       numeric(4,2),
    add column if not exists partial_exit_done      boolean default false,
    add column if not exists partial_exit_qty       numeric(14,6) default 0,
    add column if not exists runner_atr_mult        numeric(4,2),
    add column if not exists runner_stop_price      numeric(12,4),
    add column if not exists vwap_thesis_strike_count smallint default 0;

-- 3. signals — grade + ORB signal column
alter table if exists signals
    add column if not exists setup_grade            text,
    add column if not exists sector_confirmation    numeric(4,3),
    add column if not exists orb_score              real,
    add column if not exists percentile_rank        numeric(5,1),
    add column if not exists llm_shadow_json        jsonb default '{}'::jsonb;

create index if not exists idx_signals_llm_shadow
    on signals (created_at desc)
    where llm_shadow_json is not null and llm_shadow_json <> '{}'::jsonb;

-- 4. trades — grade columns for post-trade analysis
alter table if exists trades
    add column if not exists setup_grade            text,
    add column if not exists partial_exit_done      boolean default false,
    add column if not exists entry_tranche_count    smallint default 1,
    add column if not exists post_exit_checked_at   timestamptz,
    add column if not exists post_exit_horizon_minutes smallint,
    add column if not exists post_exit_max_favorable_pct numeric(8,4),
    add column if not exists post_exit_max_adverse_pct numeric(8,4),
    add column if not exists post_exit_close_after_pct numeric(8,4),
    add column if not exists post_exit_result_json  jsonb;

create index if not exists idx_trades_post_exit_replay_pending
    on trades (created_at)
    where post_exit_checked_at is null;

create index if not exists idx_trades_post_exit_reason
    on trades (exit_reason, created_at desc)
    where post_exit_checked_at is not null;

-- 5. blocked_opportunities — grade + A+ flag
alter table if exists blocked_opportunities
    add column if not exists setup_grade            text,
    add column if not exists a_plus_blocked         boolean default false;

-- Widen exit_reason constraint again to include new types
alter table if exists trades drop constraint if exists trades_exit_reason_check;
alter table if exists trades
    add constraint trades_exit_reason_check check (exit_reason in (
        'stop_loss','take_profit','time_exit',
        'signal_reversal','manual','circuit_breaker',
        'chandelier_stop','swing_exit','earnings_tomorrow',
        'regime_turned_bear','momentum_reversed',
        'macro_shock','take_profit_8pct','swing_promoted',
        'momentum_peak_decay','eod_cleanup',
        'thesis_invalidated','partial_runner_stop','a_plus_override',
        'stale_no_position','leveraged_etf_time_exit'
    ));

-- ============================================================
-- MIGRATION v4 - Advisory EU-listed US mirror metadata
-- Run in Supabase SQL Editor after advisory_signals exists.
-- ============================================================

alter table if exists advisory_signals
    add column if not exists listing_type   text,
    add column if not exists primary_symbol text,
    add column if not exists origin_market  text;

do $$ begin
  if to_regclass('public.advisory_signals') is not null then
    execute 'create index if not exists idx_advisory_signals_listing_type
      on advisory_signals (listing_type, created_at desc)
      where listing_type is not null';
  end if;
end $$;

-- ============================================================
-- MIGRATION v5 - Advisory forward-return scoreboard
-- Run in Supabase SQL Editor after advisory_signals exists.
-- ============================================================

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

-- ============================================================
-- MIGRATION v8 - Advisory per-cycle scan snapshots (Codex)
-- ============================================================

create table if not exists advisory_scan_snapshots (
    id                  bigint generated always as identity primary key,
    created_at          timestamptz not null default now(),
    cycle_id            text not null,
    cycle_started_at    timestamptz not null,
    market              text not null check (market in ('US','EU')),
    mode                text not null check (mode in ('live','shadow')),
    session_window      text,
    broker_profile      text,
    data_symbol         text not null,
    primary_symbol      text,
    broker_display_name text,
    exchange            text,
    currency            text,
    listing_type        text,
    side                text check (side in ('BUY','SELL')),
    grade               text,
    alert_stage         text,
    status              text,
    gate_reason         text,
    composite_score     numeric(7,4),
    ev_net_pct          numeric(8,4),
    breakout_quality    numeric(7,4),
    last_price          numeric(14,4),
    move_pct            numeric(8,4),
    volume              numeric(18,2),
    signal_json         jsonb default '{}'::jsonb,
    data_quality_json   jsonb default '{}'::jsonb,
    meta_json           jsonb default '{}'::jsonb,
    unique (cycle_id, market, data_symbol)
);

create index if not exists idx_advisory_scan_snapshots_latest
    on advisory_scan_snapshots (market, cycle_started_at desc);

create index if not exists idx_advisory_scan_snapshots_symbol
    on advisory_scan_snapshots (data_symbol, cycle_started_at desc);

alter table advisory_scan_snapshots enable row level security;

do $$
begin
    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'advisory_scan_snapshots'
          and policyname = 'anon read advisory_scan_snapshots'
    ) then
        create policy "anon read advisory_scan_snapshots"
            on advisory_scan_snapshots
            for select
            to anon
            using (true);
    end if;
end $$;

-- MIGRATION v9 - Advisory scan log + virtual positions
-- ============================================================

-- Advisory scan log: one row per ticker per advisory cycle, for diagnostics
create table if not exists advisory_scan_log (
    id              bigint generated always as identity primary key,
    scanned_at      timestamptz not null default now(),
    data_symbol     text not null,
    primary_symbol  text,
    market          text not null,
    session_window  text,
    listing_type    text,
    composite_score numeric(6,4),
    grade           text,
    side            text,
    alert_stage     text,
    alerted         boolean not null default false,
    gate_reason     text,
    gate_detail     jsonb default '{}'::jsonb,
    ev_net_pct      numeric(8,4),
    breakout_quality numeric(6,4),
    price_native    numeric(12,4),
    move_pct_open   numeric(8,4),
    vwap_score      real,
    macd_score      real,
    rel_strength_score real,
    tape_score      real,
    rsi_score       real,
    orb_active      boolean,
    downside_risk   boolean default false
);

create index if not exists idx_advisory_scan_log_time
    on advisory_scan_log (scanned_at desc);
create index if not exists idx_advisory_scan_log_symbol_time
    on advisory_scan_log (data_symbol, scanned_at desc);
create index if not exists idx_advisory_scan_log_market_time
    on advisory_scan_log (market, scanned_at desc);

alter table advisory_scan_log enable row level security;
create policy "anon read advisory_scan_log"
    on advisory_scan_log for select to anon using (true);

-- Virtual positions: assumed entries for A/A+ advisory alerts (auto-tracked exits)
create table if not exists advisory_virtual_positions (
    id                  bigint generated always as identity primary key,
    created_at          timestamptz not null default now(),
    advisory_signal_id  bigint,
    data_symbol         text not null,
    market              text not null,
    side                text not null,
    session_window      text,
    grade               text,
    entry_price_native  numeric(12,4),
    entry_assumed_at    timestamptz not null default now(),
    stop_price          numeric(12,4),
    target_1            numeric(12,4),
    target_2            numeric(12,4),
    currency            text default 'USD',
    fx_rate             numeric(8,4),
    suggested_size_eur  numeric(10,2),
    status              text not null default 'open'
                        check (status in ('open','hit_stop','hit_t1','hit_t2','time_exit','dismissed','signal_reversal')),
    dismissed_at        timestamptz,
    closed_at           timestamptz,
    close_price_native  numeric(12,4),
    pnl_pct             numeric(8,4),
    exit_monitor_json   jsonb default '{}'::jsonb
);

create index if not exists idx_advisory_virt_pos_status
    on advisory_virtual_positions (status, created_at desc);
create index if not exists idx_advisory_virt_pos_symbol
    on advisory_virtual_positions (data_symbol, status);

alter table advisory_virtual_positions enable row level security;
create policy "anon read advisory_virtual_positions"
    on advisory_virtual_positions for select to anon using (true);
