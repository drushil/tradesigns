-- Separate manual-trading advisory log, independent from Alpaca paper trades.

create table if not exists advisory_signals (
    id                      bigint generated always as identity primary key,
    created_at              timestamptz not null default now(),
    market                  text not null check (market in ('US','EU')),
    mode                    text not null default 'shadow' check (mode in ('live','shadow')),
    status                  text not null default 'sent' check (status in (
                                'sent','shadow_logged','expired','entered','skipped',
                                'hit_stop','hit_target','blocked_data_quality',
                                'blocked_filter','blocked_limit'
                            )),
    data_symbol             text not null,
    broker_display_name     text,
    exchange                text,
    currency                text not null default 'EUR',
    side                    text not null check (side in ('BUY','SELL')),
    grade                   text,
    composite_score         numeric(7,4),
    ev_net_pct              numeric(8,4),
    breakout_quality        numeric(7,4),
    confidence              numeric(7,4),
    entry_min               numeric(14,4),
    entry_max               numeric(14,4),
    do_not_chase_price      numeric(14,4),
    stop_price              numeric(14,4),
    target_1                numeric(14,4),
    target_2                numeric(14,4),
    suggested_size_eur      numeric(12,2),
    risk_eur                numeric(12,2),
    risk_pct                numeric(8,4),
    reward_risk             numeric(8,4),
    valid_until             timestamptz,
    time_exit_at            timestamptz,
    rationale               text,
    signal_json             jsonb default '{}'::jsonb,
    market_context_json     jsonb default '{}'::jsonb,
    data_quality_json       jsonb default '{}'::jsonb,
    message_text            text,
    fx_rate                 numeric(10,6),
    replay_checked_at       timestamptz,
    entry_triggered         boolean,
    stop_hit_first          boolean,
    target_hit_first        boolean,
    max_favorable_pct       numeric(8,4),
    max_adverse_pct         numeric(8,4),
    close_after_pct         numeric(8,4),
    manual_entry_price      numeric(14,4),
    manual_exit_price       numeric(14,4),
    manual_pnl_eur          numeric(12,2),
    notes                   text
);

create index if not exists idx_advisory_signals_created_at
    on advisory_signals (created_at desc);

create index if not exists idx_advisory_signals_market_mode
    on advisory_signals (market, mode, created_at desc);

create index if not exists idx_advisory_signals_status
    on advisory_signals (status, created_at desc);

alter table advisory_signals enable row level security;

do $$
begin
    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'advisory_signals'
          and policyname = 'anon read advisory_signals'
    ) then
        create policy "anon read advisory_signals"
            on advisory_signals
            for select
            to anon
            using (true);
    end if;
end $$;
