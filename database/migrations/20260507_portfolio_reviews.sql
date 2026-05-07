-- Advisory portfolio review table.
-- Weekly observation-only recommendations — no execution authority.
-- run_portfolio_review() writes here every Sunday 17:00 UTC.

create table if not exists portfolio_reviews (
    id              bigint generated always as identity primary key,
    reviewed_at     timestamptz not null default now(),
    equity_eur      numeric(12,2),
    position_count  int,
    summary         jsonb,   -- {"hold":n,"add":n,"trim":n,"exit":n}
    alerts          jsonb,   -- list of concentration/cash alert strings
    positions       jsonb,   -- per-ticker recommendation detail
    exposure        jsonb    -- full compute_exposure() output
);

create index if not exists portfolio_reviews_reviewed_at_idx
    on portfolio_reviews (reviewed_at desc);
