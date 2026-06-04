-- Watch-limit simulations for advisory-auto dry-run measurement.
-- Records what a limit order at each BUY watch's entry band WOULD have done,
-- so we can measure advisory edge from the way trades actually happen
-- (watch → pullback fill) instead of only the rare same-bar trade-stage path.

create table if not exists advisory_auto_simulations (
    id bigint generated always as identity primary key,
    advisory_signal_id bigint not null references advisory_signals(id) on delete cascade,
    data_symbol text not null,
    market text not null,
    side text not null,
    grade text,
    alert_stage text,
    composite_score numeric(8,4),
    breakout_quality numeric(8,4),
    currency text,
    entry_min numeric(14,4) not null,
    entry_max numeric(14,4) not null,
    stop_price numeric(14,4) not null,
    target_1 numeric(14,4),
    target_2 numeric(14,4),
    suggested_size_eur numeric(14,2),
    simulated_at timestamptz not null default now(),
    valid_until timestamptz,
    status text not null default 'pending'
        check (status in ('pending','filled','expired','hit_stop','hit_target_1','hit_target_2')),
    fill_at timestamptz,
    fill_price numeric(14,4),
    mfe_pct numeric(8,4),
    mae_pct numeric(8,4),
    last_checked_at timestamptz,
    last_price numeric(14,4),
    closed_at timestamptz,
    notes jsonb default '{}'::jsonb,
    unique (advisory_signal_id)
);

create index if not exists idx_advisory_auto_sim_status
    on advisory_auto_simulations (status, simulated_at desc);

create index if not exists idx_advisory_auto_sim_symbol
    on advisory_auto_simulations (data_symbol, simulated_at desc);

alter table advisory_auto_simulations enable row level security;

do $$
begin
    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'advisory_auto_simulations'
          and policyname = 'anon read advisory_auto_simulations'
    ) then
        create policy "anon read advisory_auto_simulations"
            on advisory_auto_simulations
            for select
            to anon
            using (true);
    end if;
end $$;
