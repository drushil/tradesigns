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
    stop_price      numeric(12,4),
    take_profit_price numeric(12,4),
    size_eur        numeric(10,2),
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
    composite_score numeric(6,4) check (composite_score between -1 and 1),
    llm_conviction  numeric(6,4) check (llm_conviction between 0 and 1),
    llm_rationale   text,
    signals_json    jsonb,
    order_id        text,
    close_order_id  text,
    close_error     text,
    commission_eur  numeric(8,4) default 0,
    slippage_eur    numeric(8,4) default 0,
    llm_cost_eur    numeric(8,5) default 0,
    risk_profile    text,
    horizon         text check (horizon in ('short','mid','both'))
);

create index if not exists idx_trades_created_at  on trades (created_at desc);
create index if not exists idx_trades_ticker_time on trades (ticker, created_at desc);
create index if not exists idx_trades_regime      on trades (regime);
create index if not exists idx_trades_pnl         on trades (net_pnl_pct)
    where net_pnl_pct is not null;

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
    stop_price          numeric(12,4),
    take_profit_price   numeric(12,4),
    hold_minutes        smallint,
    size_eur            numeric(10,2),
    order_id            text,
    status              text not null default 'open' check (status in ('open','closed')),
    close_reason        text,
    regime              text,
    composite_score     numeric(6,4),
    llm_conviction      numeric(6,4),
    llm_rationale       text,
    signals_json        jsonb default '{}'::jsonb
);

create index if not exists idx_open_trades_status on open_trades (status, created_at desc);

alter table if exists open_trades
    add column if not exists entry_time timestamptz,
    add column if not exists quantity numeric(14,6),
    add column if not exists hold_minutes smallint;

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
    add column if not exists atr_pct          numeric(6,4);

alter table if exists trades
    add column if not exists stop_price numeric(12,4),
    add column if not exists take_profit_price numeric(12,4),
    add column if not exists order_id text,
    add column if not exists close_order_id text,
    add column if not exists close_error text;

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
