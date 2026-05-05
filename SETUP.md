# AI Trading Agent — Complete Setup Guide
## Zero upfront cost · Paper trading · Full stack

---

## YOUR STEPS (what only you can do)

### Step 1 — Create accounts (30 min, all free)

#### 1a. Alpaca Markets (paper broker)
1. Go to https://alpaca.markets → Sign Up (email only, no ID needed for paper)
2. Dashboard → switch to **Paper Trading** mode (toggle top-right)
3. Go to **API Keys** → **Generate New Key**
4. Copy your `Key ID` and `Secret Key` — save them now (shown once)

#### 1b. Anthropic (Claude API)
1. Go to https://console.anthropic.com → Sign Up
2. Go to **API Keys** → **Create Key** → copy it
3. Go to **Settings → Limits** → set monthly spend cap to **€10** (safety net)
   - Note: your Claude Pro subscription does NOT cover API usage —
     the API has its own billing. At our usage level (~€3-8/month) it's minimal.

#### 1c. Supabase (free database)
1. Go to https://supabase.com → Sign up with GitHub
2. **New Project** → name it `trading-agent` → set a DB password → create
3. Wait ~2 min for provisioning
4. Go to **Settings → API** → copy:
   - `Project URL` (looks like `https://xxxx.supabase.co`)
   - `anon public` key (long JWT string)
5. Go to **SQL Editor → New Query** → paste the entire contents of
   `database/schema.sql` → click **Run**
   ✅ You should see "Success" and 6 tables created

#### 1d. NewsAPI (free news feed)
1. Go to https://newsapi.org → Get API Key (free, instant)
2. Copy your API key

#### 1e. Reddit app (free sentiment)
1. Go to https://www.reddit.com/prefs/apps (logged into Reddit)
2. **Create App** → name: `trading_agent` → type: **script**
3. Redirect URI: `http://localhost:8080`
4. Copy `client_id` (under app name) and `secret`

#### 1f. Telegram Bot (free alerts)
1. Open Telegram → search `@BotFather` → `/newbot`
2. Choose a name and username → copy the **Bot Token**
3. Start a chat with your new bot (send it any message)
4. Get your chat ID: message `@userinfobot` → copy the `id` number

---

### Step 2 — Set up the GitHub repository (10 min)

1. Go to https://github.com → **New repository**
2. Name it `trading-agent` → **Private** → Create
3. Upload all files from this project (or use GitHub Desktop)
4. Go to **Settings → Secrets and variables → Actions**
5. Add these **Secrets** (sensitive values):
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
   - `ANTHROPIC_API_KEY`
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `NEWSAPI_KEY`
   - `REDDIT_CLIENT_ID`
   - `REDDIT_CLIENT_SECRET`
   - `REDDIT_USER_AGENT` → value: `trading_agent_v1/your@email.com`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
6. Add these **Variables** (non-sensitive config):
   - `RISK_PROFILE` → `moderate`
   - `INVESTMENT_HORIZON` → `short`
   - `TICKER_UNIVERSE` → `SPY,QQQ,GLD,TLT,AAPL`
   - `STARTING_CAPITAL_EUR` → `100`
   - `ALLOW_SHORT_SELLING` → `true` or `false` (optional profile override)
   - `LLM_CALLS_PER_HOUR_LIMIT` → `40` (optional paper-learning override)

7. Go to **Actions** tab → enable Actions if prompted
8. The agent will now run automatically every 5 minutes on weekdays! 🎉

---

### Step 3 — Deploy the dashboard to Streamlit Cloud (10 min)

1. Go to https://share.streamlit.io → **Sign in with GitHub**
2. **New app** → select your `trading-agent` repo
3. Main file path: `app.py`
4. **Advanced settings → Secrets** → paste your secrets in TOML format:

```toml
ALPACA_API_KEY       = "your_key_here"
ALPACA_SECRET_KEY    = "your_secret_here"
ALPACA_BASE_URL      = "https://paper-api.alpaca.markets"
ANTHROPIC_API_KEY    = "sk-ant-..."
SUPABASE_URL         = "https://xxxx.supabase.co"
SUPABASE_ANON_KEY    = "eyJ..."
NEWSAPI_KEY          = "your_newsapi_key"
REDDIT_CLIENT_ID     = "your_reddit_id"
REDDIT_CLIENT_SECRET = "your_reddit_secret"
REDDIT_USER_AGENT    = "trading_agent_v1/your@email.com"
TELEGRAM_BOT_TOKEN   = "your_bot_token"
TELEGRAM_CHAT_ID     = "your_chat_id"
RISK_PROFILE         = "moderate"
INVESTMENT_HORIZON   = "short"
TICKER_UNIVERSE      = "SPY,QQQ,GLD,TLT,AAPL"
STARTING_CAPITAL_EUR = "100"
ALLOW_SHORT_SELLING  = "true"
LLM_CALLS_PER_HOUR_LIMIT = "40"
```

5. Click **Deploy** → your dashboard goes live at a public URL in ~2 min
6. Share the URL with no one (it shows your trading data) — or set a password
   in Streamlit Cloud settings

---

## HOW IT ALL WORKS — The flow

```
Every 5 min (GitHub Actions):
  backend/agent.py
    ├── For each ticker: compute 5 signals (yfinance, Alpaca, NewsAPI)
    ├── Pre-trade gate: check drawdown, VIX, cash, signal threshold
    ├── EV gate: is this trade profitable after fees?
    ├── LLM gate (Haiku): interpret signal → BUY/SELL/HOLD + conviction
    ├── Submit order → Alpaca paper account
    ├── Monitor exits → stop-loss or time-based close
    ├── Attribute signals → update weights (EWA learning)
    └── Log everything → Supabase

Every Sunday 18:00 UTC:
  Generate weekly insights → Claude Sonnet reads trade log
  → Actionable patterns → stored in DB → shown on dashboard

Dashboard (Streamlit Cloud — always on):
  Reads from Supabase → shows live P&L, signals, trades, learnings
```

---

## COST BREAKDOWN (monthly, paper trading phase)

| Service | Cost | Notes |
|---------|------|-------|
| Alpaca paper trading | €0 | Free forever |
| Supabase | €0 | Free tier (500MB, plenty) |
| Streamlit Cloud | €0 | Free tier |
| GitHub Actions | €0 | Free tier (2000 min/month, we use ~400) |
| yfinance data | €0 | No API key needed |
| NewsAPI | €0 | 100 req/day free (we use ~50) |
| Reddit PRAW | €0 | Free |
| Anthropic (Haiku) | ~€3-8 | ~10-20 calls/day × €0.001 |
| Anthropic (Sonnet) | ~€0.05 | 1 weekly digest call |
| **Total** | **~€3-8/mo** | Only the LLM costs money |

---

## RUNNING LOCALLY (for testing before GitHub deploy)

```bash
# 1. Clone your repo
git clone https://github.com/yourusername/trading-agent
cd trading-agent

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and fill in your .env
cp .env.template .env
# Edit .env with your API keys

# 4. Run the dashboard
streamlit run app.py

# 5. Test one signal cycle manually
python -c "from backend.agent import run_signal_cycle; run_signal_cycle()"

# 6. Test weekly digest manually
python -c "from backend.agent import run_weekly_digest; run_weekly_digest()"
```

---

## WHAT TO WATCH IN THE FIRST WEEK

- **Overview page**: equity curve should be flat or slightly positive
- **Signals page**: are signals being computed for all tickers?
- **Logs page**: look for any ERROR entries — these need fixing
- **Telegram**: you should get a message after each cycle (if configured)
- **Supabase**: check Tables → signals to see data flowing in

## COMMON ISSUES

| Problem | Fix |
|---------|-----|
| No signals computed | Check Alpaca API keys in GitHub Secrets |
| DB errors | Re-run schema.sql in Supabase SQL Editor |
| LLM errors | Check Anthropic API key and spending limit |
| No Telegram alerts | Message your bot first (must initiate chat) |
| GitHub Actions not running | Check Actions tab → enable workflows |

---

## NEXT STEPS AFTER PAPER TRADING

Once you have 60+ days of paper trades showing consistent positive returns:

1. Change `ALPACA_BASE_URL` to `https://api.alpaca.markets` (live trading)
2. Fund your Alpaca account via ACH/wire transfer
3. Update `STARTING_CAPITAL_EUR` to your actual deposit
4. Monitor closely for the first 2 weeks of live trading
