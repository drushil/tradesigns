# Advisory Improvement Roadmap

This captures the fuller design discussed after the 2026-05-28 AMD advisory case.
The current implementation should stay lean until trade volume justifies the
larger lifecycle model.

## Current Status — 2026-05-29

Implemented:

- Concise EUR-first advisory cards.
- Mark-as-taken deep links from Discord into Streamlit.
- Open advisory positions panel and manual close flow.
- Replay scoreboard for signal-level forward returns.
- Daily EUR/USD fetch with DB cache; fixed the fallback bug that caused stale
  `EURUSD_RATE=1.08` to appear in cards.
- Exit monitoring for manually entered advisory rows.
- Guarded runner continuation context:
  - Requires a prior same-day B+ watch/trade signal.
  - Distinguishes fresh-entry risk from holder/active-trader continuation.
  - Adds holder wording only when an open position is not materially underwater.
- PLTR and MU 2026-05-29 manual outcomes backfilled into `advisory_signals`.

Keep using the lightweight `advisory_signals` lifecycle model for now. Do not
promote to separate position/event tables until trade volume or multi-leg
complexity makes the extra model worth it.

## Lean Plan

1. Concise Discord cards
   - Keep symbol, action, EUR entry/stop/targets, size, valid time, why, and action.
   - Remove duplicate rationale, composite/EV noise, FX source, and USD reference lines.

2. Manual advisory action recording
   - Use existing `advisory_signals` lifecycle fields:
     `entry_triggered`, `manual_entry_price`, `manual_exit_price`,
     `manual_pnl_eur`, and `status='entered'`.
   - User can tell Codex/Claude "bought AMD at 460" or "sold AMD at 482" and the
     assistant updates the row through Supabase.

3. Exit monitoring
   - Existing advisory cycle checks entered rows.
   - Send recommendation alerts for T1, T2, stop zone, time window closing, and
     repeated same-direction momentum.
   - Alerts do not close the position. Only explicit user action does.

4. Signal quality
   - Cap A/A+ grades during US open when BUY has both negative ORB and negative
     VWAP structure, or SELL has both positive.
   - Store `premium_setup=true` when MACD and relative strength are both strongly
     aligned. Track first, act later.

## Near-Term Remaining Plan

1. Verify FX in the next live advisory cycle
   - Confirm cards use `yfinance_daily` or `daily_cache`, not `env_fallback`.
   - Confirm EUR levels line up with Trade Republic / Scalable prices.
   - If cache writes fail in GitHub Actions, inspect `fx_rate_cache` permissions
     and workflow env propagation.

2. Use the journaling flow consistently
   - For every advisory trade taken, click `Mark as taken` or tell the assistant
     the ticker, entry price, size, and later exit price.
   - Treat existing-holding trims, such as MSFT on 2026-05-29, as journal notes
     unless they map cleanly to a specific advisory row.

3. Review runner continuation in live cards
   - PLTR-like runners should read as continuation/holder context, not generic
     "wait only" warnings.
   - If runner cards become noisy, raise `ADVISORY_RUNNER_MIN_COMPOSITE` or
     require stronger tape/ORB support.

4. Backfill important manual trades while fresh
   - For future days, backfill the same day if a trade was not recorded live.
   - Store actual entry/exit prices and realised EUR P&L; do not rely only on
     replay forward returns.

5. Advisory vs autonomous trading alignment
   - Keep autonomous trading defensive until its universe and gates are reviewed.
   - Do not auto-execute advisory alerts directly yet.
   - Later design an advisory-confirmation layer for overlapping tickers:
     A/B advisory alignment can permit or size up trades; late-chase/weak
     advisory context can block or reduce trades.

## V2+ Plan

Adopt when manual advisory trading volume grows, multi-leg entries become common,
or position-level analytics become more important than simplicity.

1. Position lifecycle tables
   - `advisory_positions`: what the user actually holds.
   - `advisory_position_events`: entry, add, trim, close, alert fired, stop moved.
   - Keep `advisory_signals` immutable as the alert log.

2. Dashboard position workflow
   - Mark bought, add, trim, close.
   - Show current P&L, T1/T2/stop, and latest advisory thesis.

3. Runner continuation alerts
   - Initial lightweight runner context exists.
   - V2 should thread related ignition/watch/trade/runner alerts into one setup
     id so the dashboard can show the whole lifecycle.
   - Add smarter continuation exits: trailing stop suggestions, trim prompts,
     and "runner weakening" warnings.

4. Discord interactions
   - Deep links from Discord into Streamlit forms.
   - Later, a Discord bot/slash command path if the workflow warrants it.

5. Position-level scoreboard
   - Show signal-level forward returns and actual user execution P&L separately.
   - Attribute adds/trims/exits to advisory signals and continuation alerts.

6. Sizing
   - Move from fixed notional to grade-based risk budgets only after enough
     scored history supports it.
   - Keep conservative caps for small-account concentration risk.

7. EU noise and runtime
   - First add a persistence/logging gate for EU shadow signals.
   - Later add true early pre-filters or lower EU scan cadence if runtime matters.

8. Additional signal quality
   - Validate premium setup over multiple weeks before using it for sizing.
   - Explore opening-gap recognition for MSFT-like bar-one momentum.
   - Consider cross-ticker confirmation for semiconductor and mega-cap clusters.
   - Review NVDA-like false A cases where backward-looking trend signals overpower
     weak real-time intraday structure.

9. Autonomous trading convergence, cautiously
   - Consider scanning the advisory universe inside autonomous trading.
   - Add advisory-signal lookup as a confirmation layer before orders.
   - Keep paper-only until replay and manual execution data show a reliable edge.
   - Preserve separate horizons: advisory is human-in-the-loop intraday momentum;
     autonomous trading needs stricter precision and lower tolerance for noise.
