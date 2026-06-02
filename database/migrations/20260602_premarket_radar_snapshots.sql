-- Read-only pre-market radar snapshots.
-- Captures gap candidates and opening playbook classifications for later replay.

create table if not exists premarket_radar_snapshots (
    id bigint generated always as identity primary key,
    cycle_started_at timestamptz not null,
    session_window text not null,
    ticker text not null,
    last_price numeric(14,4),
    prior_close numeric(14,4),
    gap_pct numeric(8,4),
    premarket_high numeric(14,4),
    premarket_low numeric(14,4),
    premarket_vwap numeric(14,4),
    premarket_volume numeric(18,2),
    premarket_rvol numeric(10,4),
    spread_pct numeric(8,4),
    news_score numeric(8,4),
    catalyst_label text,
    latest_headline text,
    classification text,
    direction text,
    radar_score numeric(8,4),
    opening_plan text,
    reasons_json jsonb default '[]'::jsonb,
    earnings_json jsonb default '{}'::jsonb,
    data_quality_json jsonb default '{}'::jsonb,
    playbook_json jsonb default '{}'::jsonb,
    created_at timestamptz default now(),
    unique (cycle_started_at, ticker)
);

create index if not exists idx_premarket_radar_latest
    on premarket_radar_snapshots (cycle_started_at desc, radar_score desc);

create index if not exists idx_premarket_radar_ticker
    on premarket_radar_snapshots (ticker, cycle_started_at desc);

alter table premarket_radar_snapshots enable row level security;

do $$
begin
    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'premarket_radar_snapshots'
          and policyname = 'anon read premarket_radar_snapshots'
    ) then
        create policy "anon read premarket_radar_snapshots"
            on premarket_radar_snapshots
            for select
            to anon
            using (true);
    end if;
end $$;

