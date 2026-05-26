# Unattended Post-Market Review Snapshot Design

## Goal

Allow the post-market review automation to produce a useful session packet even
when live Supabase and Alpaca network reads are unavailable from the sandbox or
cannot pause for human approval.

## Current Constraint

The nightly review path computes metrics in `backend/daily_review.py` and saves
them to Supabase. The automation can only read those results later if network
access is available.

Today:

- post-market analytics runs at `21:05 UTC`
- daily EOD review runs at `21:25 UTC`
- review facts are persisted to Supabase and optionally Discord
- no local snapshot artifact is written for later offline reads

## Recommended Design

Add a second persistence path for read-only review artifacts:

1. Keep the existing Supabase write path unchanged.
2. After `run_daily_eod_review()` completes, also write a local JSON snapshot.
3. Store snapshots in a committed-safe workspace path such as:
   - `artifacts/daily_reviews/latest.json`
   - `artifacts/daily_reviews/YYYY-MM-DD.json`
4. Let the automation read the local snapshot first.
5. Use live Supabase/Alpaca reads only as an optional freshness upgrade.

## Snapshot Contents

The JSON snapshot should include:

- `generated_at`
- `review_date`
- `daily_review`
  - summary
  - confidence
  - worked_well
  - did_not_work
  - recommendations
- `metrics`
  - trade_summary
  - blocked_opportunities
  - near_miss_distribution
  - direction_error_candidates
  - gate_activity
  - shadow_universe
  - advisory
  - open_positions
- `broker_account_snapshot`
  - equity
  - cash
  - buying_power
  - daytrade_count
  - pattern_day_trader
  - trading_blocked
  - account_blocked
- `broker_rejections`
  - count
  - grouped_by_error
  - grouped_by_ticker
  - recent_examples
- `data_source`
  - supabase_saved: true/false
  - local_snapshot_saved: true/false
  - discord_sent: true/false

## Why Add Broker Fields

The current EOD review counts generic gate events, but broker order rejections
such as PDT failures are only visible in logs. That creates misleading
"zero-trade" days where execution was blocked for structural reasons rather
than strategy reasons.

The snapshot should therefore include:

- the broker account state at review time
- the day-trade count used by PDT logic
- parsed `order_failed` events from logs for the review date

This makes no-trade sessions explainable offline.

## Write Path

### Trigger point

Extend `run_daily_eod_review()` after:

- metrics are collected
- synthesis is produced
- Discord text is formatted
- Supabase save is attempted

### Write behavior

- Always attempt local snapshot write, even if Supabase save fails.
- Write to a temp file first, then atomically rename.
- Overwrite `latest.json` every run.
- Keep one dated file per review day.

### Failure policy

- Snapshot failure should log an error but should not fail the trading job.
- Supabase failure should not block local snapshot persistence.

## Read Path for Automation

The automation should use this order:

1. Read `artifacts/daily_reviews/latest.json` if present.
2. If absent, read the newest dated snapshot.
3. Only if no snapshot exists, fall back to live Supabase helper scripts.
4. If all reads fail, fall back to repo code + memory, as today.

## Minimal Implementation Plan

1. Add a helper such as `save_local_daily_review_snapshot(result: dict)`.
2. Call it from `backend/daily_review.py`.
3. Add a small helper to summarize `order_failed` log events for the review day.
4. Add `artifacts/daily_reviews/` to `.gitignore` unless you intentionally want
   snapshots committed.
5. Optionally add a helper script:
   - `scripts/check_latest_review_snapshot.py`

## Nice-to-Have Extension

Add a nightly GitHub Actions artifact upload for the snapshot files. That gives:

- local workspace fallback during development
- GitHub artifact fallback for CI-generated unattended runs

This is useful if the automation sometimes runs in a fresh workspace where the
local snapshot directory is empty.

## Recommendation

Implement the local snapshot path first. It is the smallest change that removes
approval dependency for post-market review reads and gives the automation a
stable offline evidence source.
