-- News sentiment cache migration
-- Run once in: Supabase → SQL Editor → New Query → Run
-- All statements are idempotent.

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

alter table news_cache    enable row level security;
alter table newsapi_usage enable row level security;

drop policy if exists "anon read news_cache" on news_cache;
create policy "anon read news_cache" on news_cache for select to anon using (true);

drop policy if exists "anon read newsapi_usage" on newsapi_usage;
create policy "anon read newsapi_usage" on newsapi_usage for select to anon using (true);
