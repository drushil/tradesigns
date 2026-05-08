-- Blocked opportunity replay table.
-- Records gates, EV blocks, and ranking skips so later jobs can measure
-- whether the agent avoided bad trades or missed profitable moves.

create table if not exists blocked_opportunities (
    id                      bigint generated always as identity primary key,
    created_at              timestamptz not null default now(),
    ticker                  text not null,
    action_hint             text check (action_hint in ('BUY','SELL','HOLD',null)),
    composite_score         numeric(6,4),
    block_stage             text not null check (block_stage in (
                                'gate','ev','ranking','llm','conviction','price')),
    block_reason            text,
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

alter table blocked_opportunities enable row level security;

drop policy if exists "anon read blocked_opportunities" on blocked_opportunities;
create policy "anon read blocked_opportunities"
    on blocked_opportunities for select to anon using (true);
