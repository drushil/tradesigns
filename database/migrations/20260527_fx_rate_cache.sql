-- Daily FX cache used by advisory display conversion.

create table if not exists fx_rate_cache (
    pair        text not null,
    rate_date   date not null,
    rate        numeric(12,6) not null,
    source      text,
    fetched_at  timestamptz not null default now(),
    meta_json   jsonb default '{}'::jsonb,
    primary key (pair, rate_date)
);

create index if not exists idx_fx_rate_cache_pair_date
    on fx_rate_cache (pair, rate_date desc);

alter table fx_rate_cache enable row level security;

do $$
begin
    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'fx_rate_cache'
          and policyname = 'anon read fx_rate_cache'
    ) then
        create policy "anon read fx_rate_cache"
            on fx_rate_cache
            for select
            to anon
            using (true);
    end if;
end $$;
