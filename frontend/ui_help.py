"""Shared dashboard tooltip copy."""
from __future__ import annotations

import html

import streamlit as st


HELP_TEXT = {
    # Navigation and controls
    "Navigation": "Choose which dashboard page to open.",
    "Ticker": "Filter the view to one symbol or show all symbols.",
    "Regime": "Filter by the market regime recorded when the trade or signal was created.",
    "Side": "Filter by order direction, such as BUY or SELL.",
    "Outcome": "Filter closed trades by profitable or losing outcome.",
    "Exposure": "Filter by market exposure direction, including long or short-market trades.",
    "Hold type": "Separate intraday trades from positions promoted to swing holds.",
    "Filter by level": "Show only one log severity or event category.",
    "Show": "Maximum number of log rows to load.",
    "Select review week": "Pick the weekly portfolio review snapshot to inspect.",
    "Look-back (days)": "Number of recent days included in the grading dashboard.",
    "Alpha leakage threshold (%)": "Minimum favorable move after a block that counts as a missed opportunity.",
    "Compute Signals Now": "Run a live signal calculation for the configured ticker universe.",
    "Generate Insights Now": "Create a new AI learning digest from recent closed trades.",
    "Refresh": "Reload the latest logs from Supabase.",

    # Portfolio overview
    "Portfolio Value": "Current simulated portfolio value after applying the configured capital ceiling and FX rate.",
    "Cash Available": "Cash currently available for new positions inside the simulated portfolio.",
    "Total Trades": "Closed trades counted in the active stats window.",
    "Win Rate": "Percentage of closed trades with positive net P&L.",
    "Total P&L": "Total realised net profit or loss across closed trades.",
    "Equity Curve": "Portfolio value over time from stored portfolio snapshots.",
    "Open Positions": "Positions currently reported by the broker.",
    "Open Momentum Swings": "Intraday winners that were promoted to multi-day swing holds.",
    "Daily P&L": "Daily percentage gain or loss from portfolio snapshots.",
    "Performance by Market Regime": "Average trade performance grouped by recorded market regime.",

    # Signals
    "Signals": "Number of signal modules available to the engine.",
    "Weighted scores": "Signals that directly contribute to the composite score.",
    "Multiplier": "Context factor that scales the composite score instead of adding a raw score.",
    "Order Book": "Bid/ask imbalance from the latest quote; positive means buy-side pressure.",
    "Tape Aggrssn": "Short label for tape aggression: volume spike multiplied by price momentum.",
    "Tape Aggression": "Volume spike multiplied by price momentum.",
    "RSI Diverg": "Short label for RSI divergence: overbought/oversold and price-RSI divergence.",
    "RSI Divergence": "Overbought/oversold and price-RSI divergence score.",
    "News Sntmnt": "Short label for keyword sentiment from recent headlines.",
    "News Sentiment": "Keyword sentiment from recent headlines.",
    "VWAP Dev": "Distance from intraday VWAP; helps identify stretched or reclaiming moves.",
    "VWAP Deviation": "Distance from intraday VWAP; helps identify stretched or reclaiming moves.",
    "MACD": "MACD momentum direction and crossover score.",
    "MACD Crossover": "MACD momentum direction and crossover score.",
    "Rel Strength": "Ticker performance versus SPY over recent bars.",
    "Relative Strength": "Ticker performance versus SPY over recent bars.",
    "BB Squeeze": "Bollinger Band compression and breakout score.",
    "Put/Call": "Options put/call volume sentiment score.",
    "Put/Call Ratio": "Options put/call volume sentiment score.",
    "Earnings x": "Earnings proximity multiplier applied to the composite score.",
    "Earnings ×": "Earnings proximity multiplier applied to the composite score.",
    "ATR": "Average True Range as a percent of price; used as the volatility estimate.",
    "ATR %": "Average True Range as a percent of price; used as the volatility estimate.",
    "Size preview": "Estimated position size from risk, volatility, and conviction before placing a trade.",
    "Regime": "Market and macro regime used for gates, scoring, and sizing.",

    # Trades
    "Trades shown": "Closed trades remaining after the selected filters.",
    "Win rate": "Share of filtered trades with positive net P&L.",
    "Avg P&L": "Average net percentage P&L for the filtered trades.",
    "Avg hold": "Average holding time for the filtered trades.",
    "Bearish exposure": "Share of filtered trades that were short-market or bearish exposure.",
    "Swing trades": "Filtered trades classified as swing holds.",
    "Swing win rate": "Share of swing trades with positive net P&L.",
    "Swing avg P&L": "Average net percentage P&L for swing trades.",
    "Swing total P&L": "Total realised EUR P&L from swing trades.",
    "Swing avg hold": "Average holding time for swing trades.",
    "Avg conviction": "Average stored swing conviction at promotion or review.",
    "Swing vs Intraday": "Compares closed intraday trades against trades promoted to swing holds.",
    "P&L by Signal Score": "Relationship between entry composite score and realised trade P&L.",
    "Exit Reason Breakdown": "How often each exit rule closed a trade.",
    "P&L by Strategy": "Average P&L grouped by strategy family.",
    "P&L by Exposure": "Average P&L grouped by long, short, or other exposure direction.",
    "All Trades": "Latest closed trades with the most relevant execution and signal fields.",
    "Swing Trade Details": "Per-trade swing metadata, including conviction and re-evaluation count.",
    "Conviction": "Stored swing conviction score for the position.",
    "Hold days": "Actual number of trading days held.",
    "Daily re-evals": "Number of daily swing review passes recorded for the trade.",

    # Grading and learning
    "Graded signals": "Signals that received an A+/A/B/C setup grade.",
    "A+ setups": "Highest-quality setups according to adaptive grading.",
    "A setups": "Strong setups that did not reach A+ quality.",
    "A+ win rate": "Win rate for closed trades that entered with an A+ grade.",
    "A+ avg P&L": "Average net P&L for closed trades that entered with an A+ grade.",
    "Missed alpha": "Blocked opportunities whose later favorable move exceeded the selected threshold.",
    "Total blocked": "Candidates recorded as blocked or skipped.",
    "Replayed": "Blocked candidates that have enough future price data to evaluate.",
    "Profitable if taken": "Replayed blocks that later moved favorably by at least the selected threshold.",
    "Hit rate": "Share of replayed blocks that met the favorable-move threshold.",
    "Signals computed (7d)": "Signals stored during the last seven days.",
    "Signals gated": "Signals blocked before LLM or order submission.",
    "LLM calls saved": "Estimated LLM calls avoided because gates blocked low-EV candidates.",
    "Blocked/skipped (7d)": "Blocked or skipped candidates recorded in the last seven days.",
    "Potential misses": "Replayed blocks that later moved favorably enough to flag possible missed alpha.",
    "Likely saves": "Replayed blocks that moved adversely enough to suggest the block avoided loss.",

    # Performance
    "Expectancy": "Average expected return per trade after wins and losses.",
    "Profit Factor": "Gross winning P&L divided by gross losing P&L.",
    "Sharpe Ratio": "Annualised return-to-volatility ratio based on closed trade results.",
    "Calmar Ratio": "Annualised return divided by maximum drawdown.",
    "Avg R-Multiple": "Average realised return in units of initial risk.",
    "Positive R": "Trades that earned more than zero times initial risk.",
    "Negative R": "Trades that lost money in R-multiple terms.",
    "Avg R": "Average realised R-multiple for the displayed distribution.",
    "Est. Commission": "Estimated broker or execution commission for the closed trades.",
    "LLM Costs": "Recorded AI inference costs attached to the trades.",
    "Est. Slippage": "Estimated execution slippage recorded on trades.",
    "Costs / Gross P&L": "Total estimated costs as a share of gross winning P&L.",
    "Latest test expectancy": "Most recent walk-forward out-of-sample expectancy.",
    "Best observed threshold": "Score threshold with the best observed validation expectancy.",

    # Portfolio review and yield
    "Equity (€)": "Portfolio equity at the time of the weekly review.",
    "Positions": "Number of open positions included in the section.",
    "Hold": "Positions recommended to keep unchanged.",
    "Trim / Exit": "Positions recommended for risk reduction or closure.",
    "Add": "Positions recommended for additional allocation.",
    "Recommendation Mix": "Distribution of hold, add, trim, exit, and rebalance recommendations.",
    "Position Recommendations": "Ticker-level recommendation, thesis status, P&L, and rationale.",
    "Sector Exposure": "Portfolio exposure by sector as a percentage of equity.",
    "Cash (€)": "Cash balance captured in the portfolio review.",
    "Deployed (€)": "Capital currently deployed into open positions.",
    "Review History": "Recommendation mix across recent weekly reviews.",
    "Sweepable": "Cash eligible for a sweep after reserves are held back.",
    "Reserved": "Cash intentionally kept out of the sweep for trading and safety.",
    "Est. daily yield": "Estimated one-day income from sweepable cash.",
    "Est. annual yield": "Estimated annual income if the sweepable balance persisted.",
    "Total simulated yield": "Cumulative simulated yield recorded from eligible sweep rows.",
    "First sweep": "Date of the first stored sweep simulation row.",
    "Equiv. annual rate": "Annualised yield implied by the simulated sweep rows.",
    "Current Sweep Status": "Latest cash sweep calculation and whether it would execute.",
    "Simulated Yield Accumulator": "Cumulative yield that the sweep logic would have generated in simulation.",
    "Dividend Calendar": "Upcoming dividend opportunities found in the configured universe.",
    "Live Mode Readiness": "Checklist of requirements before real cash sweep execution.",

    # Config and logs
    "Active Risk Profile": "Risk profile currently loaded from environment configuration.",
    "Risk/trade": "Percent of capital the ATR sizing model is allowed to risk on one trade before grade and EV multipliers.",
    "Ticker Universe": "Symbols the agent scans, scores, and can trade.",
    "Signal Weights (current priors)": "Profile-level starting weights before learning adjustments.",
    "All Profiles Comparison": "Side-by-side summary of available risk profiles.",
    "How to change configuration": "Where to update environment values that control the agent.",
    "Recent Trade Blockers": "Recent log events that prevented trades from being placed.",

    # Common dataframe columns
    "Created At": "Timestamp when the row was created.",
    "created_at": "Timestamp when the row was created.",
    "Ticker": "Ticker symbol for the instrument.",
    "Name": "Human-readable security or fund name.",
    "Type": "Asset or ticker category.",
    "Agent context": "Why this ticker matters to the trading system.",
    "Stage": "Decision stage that blocked or processed the candidate.",
    "Reason": "Recorded reason for the block, exit, or recommendation.",
    "Grade": "Setup quality grade assigned by the grading engine.",
    "Composite": "Final weighted signal score used for decisioning.",
    "Max fav %": "Maximum favorable move after the candidate was blocked.",
    "Max adverse %": "Maximum adverse move after the candidate was blocked.",
    "Blocks": "Number of candidates blocked at that stage.",
    "Profitable": "Count of replayed blocks that later met the favorable-move threshold.",
    "Avg fav move": "Average maximum favorable move after blocking.",
    "Hold Type": "Whether the row represents an intraday trade or swing hold.",
    "Trades": "Number of trades in the group.",
    "Avg Net P&L (%)": "Average realised net percentage profit or loss.",
    "Win Rate (%)": "Percentage of trades in the group with positive net P&L.",
    "net_pnl_pct": "Realised net percentage profit or loss.",
    "pnl_eur": "Realised profit or loss in EUR.",
    "hold_minutes": "Total holding time in minutes.",
    "exit_reason": "Exit rule or condition that closed the trade.",
    "composite_score": "Weighted signal score at decision time.",
    "llm_conviction": "LLM confidence score stored with the trade decision.",
    "Status": "Visual status marker for the row.",
}


def help_text(key: str, fallback: str | None = None) -> str:
    """Return concise tooltip text for a dashboard field."""
    return HELP_TEXT.get(key, fallback or f"What {key} represents on this dashboard.")


def metric(container, label: str, value, delta=None, *, help_key: str | None = None, **kwargs):
    """Render a metric with the shared help copy."""
    kwargs.setdefault("help", help_text(help_key or label))
    return container.metric(label, value, delta=delta, **kwargs)


def selectbox(label: str, options, *, help_key: str | None = None, **kwargs):
    """Render a selectbox with the shared help copy."""
    kwargs.setdefault("help", help_text(help_key or label))
    return st.selectbox(label, options, **kwargs)


def button(label: str, *, help_key: str | None = None, **kwargs):
    """Render a button with the shared help copy."""
    kwargs.setdefault("help", help_text(help_key or label.replace("🔄 ", "").replace("🧠 ", "")))
    return st.button(label, **kwargs)


def info_label(label: str, key: str | None = None) -> str:
    """Return HTML for a label with a browser tooltip."""
    help_value = HELP_TEXT.get(key or label, key or help_text(label))
    title = html.escape(help_value, quote=True)
    safe_label = html.escape(label)
    return (
        f"<span title=\"{title}\" "
        f"style=\"border-bottom:1px dotted #555;cursor:help\">{safe_label}</span>"
    )


def section_title(title: str, level: int = 5, *, help_key: str | None = None):
    """Render a hover-explained section heading without visible helper text."""
    tag = f"h{level}"
    help_value = HELP_TEXT.get(help_key or title, help_key or help_text(title))
    st.markdown(
        f"<{tag} title=\"{html.escape(help_value, quote=True)}\" "
        f"style=\"cursor:help\">{html.escape(title)}</{tag}>",
        unsafe_allow_html=True,
    )


def section_header(title: str, *, help_key: str | None = None):
    """Render the compact uppercase grading header with hover help."""
    help_value = HELP_TEXT.get(help_key or title, help_key or help_text(title))
    st.markdown(
        f"<div class=\"section-header\" "
        f"title=\"{html.escape(help_value, quote=True)}\">"
        f"{html.escape(title)}</div>",
        unsafe_allow_html=True,
    )


def column_config(columns) -> dict:
    """Return generic Streamlit column help for a dataframe."""
    config = {}
    for col in columns:
        key = str(col)
        label = key or "Status"
        config[key] = st.column_config.Column(
            label=label if not key else None,
            help=help_text(label),
        )
    return config
