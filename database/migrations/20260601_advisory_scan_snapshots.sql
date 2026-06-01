-- Latest per-cycle advisory scan state for every scanned ticker.
-- This is diagnostic/operational data, separate from advisory_signals alert rows.

create table if not exists advisory_scan_snapshots (
    id                  bigint generated always as identity primary key,
    created_at          timestamptz not null default now(),
    cycle_id            text not null,
    cycle_started_at    timestamptz not null,
    market              text not null check (market in ('US','EU')),
    mode                text not null check (mode in ('live','shadow')),
    window              text,
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
