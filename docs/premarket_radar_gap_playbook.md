# Pre-Market Radar and Gap Playbook

## Problem

The agent's autonomous trading path is regular-hours first. Advisory mode has a
short US pre-market watch window, but pre-market alerts are intentionally capped
to watch/ignition and are not executable advisory-auto candidates. Weekend and
early pre-market moves can therefore arrive as surprise context at the open.

## Principles

- Awareness before execution.
- Treat pre-market as its own regime, not regular trading shifted earlier.
- Free-stack-first design; paid auction/imbalance data is optional.
- Human-in-the-loop until replay data proves a setup class.
- Make data quality visible: rows, volume, spread, catalyst, and source.
- Anchor market sessions in `America/New_York`; use Berlin time for display.

## Phase 1: Radar

Run a read-only radar before the US open and during Sunday futures-watch time.
The scanner ranks configured tickers, US advisory tickers, and optional
`PREMARKET_EXTRA_TICKERS`.

Captured features:

- Gap from prior regular close.
- Pre-market high, low, VWAP, and volume.
- Pre-market relative volume when prior samples exist.
- Latest spread when Alpaca quote data is available.
- News headline/sentiment and earnings context.
- Playbook classification and opening plan.

## Phase 2: Catalyst Layer

Use earnings data as both risk and opportunity context. Catalyst labels include
earnings, analyst, M&A, company news, news sentiment, and unknown. Unknown
catalysts are treated conservatively until volume and opening confirmation
arrive.

## Phase 3: Gap Learning

Persist radar rows so post-open replay can later learn whether a candidate
faded or continued. Learning targets should include:

- MFE/MAE after 5, 15, 30, and 60 minutes.
- Whether PMH/PML broke or rejected.
- Whether pre-market VWAP held.
- Outcome by gap size, catalyst, RVOL, spread, regime, and sector.

## Phase 4: Opening Confirmation

Free-stack confirmation must work without paid auction data:

- PMH/PML break or rejection.
- Pre-market VWAP hold/reclaim.
- First 1-5 minute opening-range breakout.
- Spread normalization.
- RVOL/volume continuation.

Optional paid input: Nasdaq Opening Cross imbalance / NOII through a market-data
vendor. This is high value but not required for the design to function.

## Phase 5: Execution Constraints

Pre-market execution is not enabled in this phase. If it is ever enabled, it
must use a separate lifecycle:

- Limit orders only.
- DAY or GTC only.
- No bracket or OCO assumptions in extended hours.
- No stop-market protection during extended hours.
- Client-side polling/cancel logic until regular-session protective orders are
safe to submit.
- Hard spread cap, current-price validation, small size, and stale-order
cancellation.

## Current Implementation

The first implementation adds `backend.premarket.radar.run_premarket_radar()`,
`premarket_radar_snapshots`, and a dedicated GitHub Actions workflow. It is
read-only and does not touch broker order code.

