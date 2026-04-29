# AI Trading Agent — Codex Project

## Project Overview

A paper-trading AI agent that:
- Computes 5 micro-signals per ticker every 5 minutes during market hours
- Gates trades through a risk profile and EV filter
- Uses Codex Haiku (fast, cheap) for signal decisions and Sonnet for weekly digests
- Learns from every trade via Exponential Weight Averaging (EWA)
- Displays everything on a Streamlit dashboard backed by Supabase

**Stack:** Python · Alpaca (paper broker) · Supabase (Postgres) · Anthropic API · Streamlit · GitHub Actions (scheduler)

**MCP:** Supabase MCP server connected at `https://mcp.supabase.com/mcp?project_ref=crxrnmmvrbwhqulyehmo`

---

## Repository Structure

```
trading-agent/
├── app.py                          # Streamlit entry point (multipage)
├── AGENTS.md                       # This file — Codex project context
├── SETUP.md                        # Human setup guide
├── requirements.txt
│
├── backend/
│   ├── agent.py                    # Main orchestrator — signal cycle + scheduler
│   ├── signals/engine.py           # 5 signal computations (yfinance, Alpaca, NewsAPI)
│   ├── broker/alpaca.py            # Order submission, positions, risk gate
│   └── learning/engine.py         # EWA weights, attribution, EV gate, LLM digest
│
├── config/
│   └── risk_profiles.py            # 5 risk profiles (conservative → aggressive)
│
├── database/
│   ├── schema.sql                  # Supabase schema (run once to bootstrap)
│   └── client.py                   # All DB operations (read=anon, write=service_role)
│
├── frontend/pages/
│   ├── overview.py                 # Portfolio equity curve + KPIs
│   ├── signals.py                  # Live signal scores per ticker
│   ├── trades.py                   # Trade history + P&L analysis
│   ├── learning.py                 # Signal weights + weekly insights
│   ├── config_page.py              # Active risk profile viewer
│   └── logs.py                     # Agent runtime log stream
│
├── scripts/
│   └── telegram_notify.py          # Post-cycle Telegram summary
│
├── skills/                         # Supabase agent skills (installed via npx)
│   ├── supabase/SKILL.md
│   └── supabase-postgres-best-practices/SKILL.md
│
├── .mcp.json                       # MCP server config (Supabase)
├── .github/workflows/agent.yml     # GitHub Actions: runs agent every 5 min
└── .streamlit/config.toml          # Dark theme config
```

---

## Key Architecture Decisions

### Signal pipeline
Each ticker runs through 5 signals in `backend/signals/engine.py`:
1. `rsi_divergence_score` — RSI overbought/oversold + divergence detection (yfinance)
2. `vwap_deviation_score` — distance from intraday VWAP (yfinance 1m bars)
3. `news_sentiment_score` — keyword scoring on NewsAPI headlines (cached 15 min)
4. `tape_aggression_score` — volume spike × momentum direction (yfinance 5m bars)
5. `order_book_score` — bid/ask imbalance via Alpaca latest quote

Composite = weighted sum of all 5. Weights start as profile priors, then update via EWA learning.

### Decision flow
```
Signal computed
  → pre_trade_gate() [hard rules: drawdown, VIX, cash, signal threshold]
    → compute_expected_value() [EV > 0.03% after fees required]
      → llm_signal_decision() [Haiku: structured JSON BUY/SELL/HOLD]
        → submit_market_order() [Alpaca paper API]
          → monitor exit [stop-loss or time-based]
            → attribute_signals() → update EWA weights
```

### Database key rules
- **Always use `get_client(write=True)`** for INSERT/UPDATE — uses service_role key (bypasses RLS)
- **`get_client()` (default)** for SELECT — uses anon key (respects RLS, safe for dashboard)
- **Never use `gen_random_uuid()`** — tables use `bigint generated always as identity` PKs
- **3 pre-computed views** for dashboard: `trade_stats_30d`, `regime_performance`, `latest_signal_weights`

### LLM usage — cost control
- **Haiku** (`Codex-haiku-4-5-20251001`) — every signal decision. Limit: 20 calls/hour via `LLM_CALLS_PER_HOUR_LIMIT`
- **Sonnet** (`Codex-sonnet-4-6`) — weekly digest only. Called once/week on Sunday 18:00 UTC
- Gate: LLM is only called if `abs(composite_score) > min_signal_score` AND `EV > 0.03%`
- Prompt format: always structured JSON output, no markdown, ≤ 120 tokens response

### Risk profiles
Defined in `config/risk_profiles.py`. Active profile set via `RISK_PROFILE` env var.
The `get_effective_profile()` function in `learning/engine.py` dynamically tightens limits based on:
- Consecutive losses (≥3 → halve position size, raise conviction threshold)
- Drawdown approaching limit (>60% of max → scale down)
- High VIX (>25 → reduce size 30%)

---

## Environment Variables

All config via `.env` (local) or GitHub Secrets + Streamlit Cloud Secrets (deployed):

```bash
# Broker
ALPACA_API_KEY          # Paper account key ID
ALPACA_SECRET_KEY       # Paper account secret
ALPACA_BASE_URL         # https://paper-api.alpaca.markets

# AI
ANTHROPIC_API_KEY       # sk-ant-...

# Database
SUPABASE_URL            # https://xxxx.supabase.co
SUPABASE_ANON_KEY       # For reads (dashboard)
SUPABASE_SERVICE_KEY    # For writes (agent) — never expose to frontend

# Data feeds (all free)
NEWSAPI_KEY
REDDIT_CLIENT_ID
REDDIT_CLIENT_SECRET
REDDIT_USER_AGENT

# Alerts
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID

# Agent config
RISK_PROFILE            # conservative|cautious|moderate|growth|aggressive
INVESTMENT_HORIZON      # short|mid|both
TICKER_UNIVERSE         # SPY,QQQ,GLD,TLT,AAPL (comma-separated)
STARTING_CAPITAL_EUR    # 100
LLM_CALLS_PER_HOUR_LIMIT # 20
```

---

## Running Locally

```bash
pip install -r requirements.txt
cp .env.template .env    # fill in your keys

# Dashboard
streamlit run app.py

# One signal cycle (test)
python -c "from backend.agent import run_signal_cycle; run_signal_cycle()"

# Weekly digest (test)
python -c "from backend.agent import run_weekly_digest; run_weekly_digest()"

# Full scheduler (blocking — runs on cron schedule)
python backend/agent.py
```

---

## Supabase Schema Notes

- Run `database/schema.sql` once in Supabase SQL Editor to create all tables
- RLS is enabled. anon key = read-only (dashboard). service_role = write (agent)
- Key tables: `trades`, `signals`, `signal_weights`, `learnings`, `portfolio_snapshots`, `agent_logs`
- Key views (pre-computed): `trade_stats_30d`, `regime_performance`, `latest_signal_weights`
- Use `execute_sql` via MCP to iterate on schema changes. Use `supabase db pull` when ready to commit migrations

---

## Common Tasks for Codex

**Add a new signal:**
1. Add computation function in `backend/signals/engine.py`
2. Add to `compute_all_signals()` weighted sum
3. Add column to `signals` table in `database/schema.sql` (use `execute_sql` MCP)
4. Add to `insert_signal()` in `database/client.py`
5. Add weight key in all 5 profiles in `config/risk_profiles.py`

**Add a new risk profile:**
1. Add entry to `RISK_PROFILES` dict in `config/risk_profiles.py`
2. Set `RISK_PROFILE=yourname` in `.env`

**Modify the dashboard:**
- Each page is a standalone module in `frontend/pages/`
- All use Streamlit + Plotly with dark theme (`template="plotly_dark"`)
- Data comes from `database/client.py` functions

**Debug a failing agent cycle:**
1. Check `agent_logs` table in Supabase → filter `level=ERROR`
2. Or check Logs page on dashboard
3. Check GitHub Actions logs for the failed run

---

## Deployment

- **Agent (scheduler):** GitHub Actions — `.github/workflows/agent.yml` runs on cron every 5 min weekdays 14:00-20:00 UTC
- **Dashboard:** Streamlit Cloud — connect repo, set secrets, deploy `app.py`
- **Database:** Supabase — free tier, project ref `crxrnmmvrbwhqulyehmo`
- **Zero ongoing cost** except Anthropic API (~€3-8/month at paper trading volume)
