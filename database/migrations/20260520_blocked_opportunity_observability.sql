-- Store structured block details for analytics without parsing human text.
-- The first consumer is near-threshold gate analysis. Trading behavior is unchanged.

alter table if exists blocked_opportunities
    add column if not exists block_detail jsonb default '{}'::jsonb;
