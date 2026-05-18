-- Daily post-market review loop. The agent writes deterministic metrics,
-- LLM synthesis, and human-approved config recommendations here.

create table if not exists daily_reviews (
    id                          bigint generated always as identity primary key,
    review_date                 date not null unique,
    created_at                  timestamptz not null default now(),
    status                      text not null default 'pending'
                                check (status in ('pending','reviewed','partially_applied','applied','archived')),
    summary                     text,
    confidence                  numeric(5,4),
    metrics_json                jsonb not null default '{}'::jsonb,
    review_json                 jsonb not null default '{}'::jsonb,
    recommendations_json        jsonb not null default '[]'::jsonb,
    discord_message             text,
    model                       text,
    error                       text
);

create index if not exists idx_daily_reviews_review_date
    on daily_reviews (review_date desc);

create table if not exists config_change_recommendations (
    id                          bigint generated always as identity primary key,
    created_at                  timestamptz not null default now(),
    review_date                 date not null,
    daily_review_id             bigint references daily_reviews(id) on delete set null,
    category                    text not null default 'parameter',
    variable                    text,
    current_value               text,
    suggested_value             text,
    command_text                text,
    reason                      text,
    evidence                    jsonb not null default '{}'::jsonb,
    confidence                  numeric(5,4),
    evidence_days               integer not null default 1,
    expected_effect             text,
    success_metric              text,
    rollback_condition          text,
    autonomy_level              text not null default 'human_approval'
                                check (autonomy_level in ('auto_log','human_approval','never_auto')),
    status                      text not null default 'pending'
                                check (status in ('pending','accepted','rejected','applied','rolled_back','expired'))
);

create index if not exists idx_config_change_recommendations_review_date
    on config_change_recommendations (review_date desc);

create index if not exists idx_config_change_recommendations_status
    on config_change_recommendations (status, created_at desc);

alter table daily_reviews enable row level security;
alter table config_change_recommendations enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'daily_reviews'
          and policyname = 'anon read daily_reviews'
    ) then
        create policy "anon read daily_reviews"
            on daily_reviews for select to anon using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'config_change_recommendations'
          and policyname = 'anon read config_change_recommendations'
    ) then
        create policy "anon read config_change_recommendations"
            on config_change_recommendations for select to anon using (true);
    end if;
end $$;

grant select on daily_reviews to anon;
grant select on config_change_recommendations to anon;
