-- Shadow LLM decision telemetry for primary-vs-shadow model comparison.

alter table if exists signals
    add column if not exists llm_shadow_json jsonb default '{}'::jsonb;

create index if not exists idx_signals_llm_shadow
    on signals (created_at desc)
    where llm_shadow_json is not null and llm_shadow_json <> '{}'::jsonb;
