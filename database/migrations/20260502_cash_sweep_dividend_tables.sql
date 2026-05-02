-- Cash sweep and dividend opportunity tables for BROKER_ENV-gated features.
-- portfolio_snapshots: add sweep tracking columns.
-- Run manually in Supabase SQL Editor.

begin;

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

alter table if exists portfolio_snapshots
    add column if not exists sweep_active      boolean default false,
    add column if not exists sweep_ticker      text,
    add column if not exists sweep_value_eur   numeric(10,2),
    add column if not exists sim_yield_ytd_eur numeric(10,4);

-- RLS: anon can read, service_role can write (inherits from policy-less tables)
alter table cash_sweeps          enable row level security;
alter table dividend_opportunities enable row level security;

create policy "anon read cash_sweeps"
    on cash_sweeps for select to anon using (true);

create policy "anon read dividend_opportunities"
    on dividend_opportunities for select to anon using (true);

grant select on cash_sweeps           to anon;
grant select on dividend_opportunities to anon;

create index if not exists idx_cash_sweeps_executed_at
    on cash_sweeps (executed_at desc);

create index if not exists idx_dividend_opportunities_scanned_at
    on dividend_opportunities (scanned_at desc, ticker);

commit;
