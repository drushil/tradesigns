# Advisory Improvement Roadmap

This captures the fuller design discussed after the 2026-05-28 AMD advisory case.
The current implementation should stay lean until trade volume justifies the
larger lifecycle model.

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
   - If a user holds a ticker and new same-direction advisories fire, send
     holder-context messages instead of fresh-entry watch messages.
   - Separate "hold runner" from "fresh chase".

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
