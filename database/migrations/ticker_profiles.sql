-- Ticker profile cache migration
-- Run once in: Supabase -> SQL Editor -> New Query -> Run
-- All statements are idempotent.

create table if not exists ticker_profiles (
    id              bigint generated always as identity primary key,
    ticker          text not null unique,
    updated_at      timestamptz not null default now(),
    profile_json    jsonb not null default '{}'::jsonb
);

create index if not exists idx_ticker_profiles_ticker
    on ticker_profiles (ticker);

alter table ticker_profiles enable row level security;

drop policy if exists "anon read ticker_profiles" on ticker_profiles;
create policy "anon read ticker_profiles" on ticker_profiles for select to anon using (true);
