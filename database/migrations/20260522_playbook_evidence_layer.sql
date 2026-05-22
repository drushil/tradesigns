-- Observability-only playbook evidence layer.
-- Adds tags for playbook lifecycle, data quality, estimated costs, and factor exposure.
-- Trading behavior is unchanged; these fields support replay and promotion analysis.

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
