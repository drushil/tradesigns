"""Ticker summaries used across the dashboard."""
from html import escape
from typing import Optional


_PROFILE_CACHE = {}
_FETCH_MISSES = set()


TICKER_PROFILES = {
    "SPY": {
        "name": "SPDR S&P 500 ETF Trust",
        "type": "Core US large-cap equity",
        "summary": "Tracks the S&P 500, giving broad exposure to large US companies across all 11 GICS sectors.",
        "agent_role": "Baseline risk appetite and broad-market direction.",
        "source": "State Street",
        "url": "https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-sp-500-etf-trust-spy",
    },
    "QQQ": {
        "name": "Invesco QQQ Trust",
        "type": "Nasdaq-100 growth equity",
        "summary": "Tracks the Nasdaq-100, concentrating on large non-financial growth companies listed on Nasdaq.",
        "agent_role": "High-liquidity growth and mega-cap tech momentum proxy.",
        "source": "Invesco",
        "url": "https://www.invesco.com/qqq-etf/en/home.html",
    },
    "IWM": {
        "name": "iShares Russell 2000 ETF",
        "type": "US small-cap equity",
        "summary": "Tracks the Russell 2000, providing targeted exposure to smaller US public companies.",
        "agent_role": "Small-cap, domestic cyclicality, and rate-sensitive risk-on signal.",
        "source": "iShares",
        "url": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf",
    },
    "GLD": {
        "name": "SPDR Gold Shares",
        "type": "Gold commodity trust",
        "summary": "Designed to reflect the price of gold bullion, less expenses, through shares backed by physical gold.",
        "agent_role": "Defensive, inflation, USD, and real-rate stress hedge.",
        "source": "State Street",
        "url": "https://www.ssga.com/us/en/individual/etfs/spdr-gold-shares-gld",
    },
    "TLT": {
        "name": "iShares 20+ Year Treasury Bond ETF",
        "type": "Long-duration US Treasuries",
        "summary": "Tracks US Treasury bonds with remaining maturities greater than 20 years.",
        "agent_role": "Duration, recession-risk, and rate-expectation signal.",
        "source": "iShares",
        "url": "https://www.ishares.com/us/products/239454/ishares-20-year-treasury-bond-etf",
    },
    "XLV": {
        "name": "Health Care Select Sector SPDR ETF",
        "type": "S&P 500 health care sector",
        "summary": "Targets S&P 500 health care companies including pharma, biotech, providers, equipment, and life sciences.",
        "agent_role": "Defensive equity sector and rotation signal.",
        "source": "State Street",
        "url": "https://www.ssga.com/us/en/intermediary/etfs/state-street-health-care-select-sector-spdr-etf-xlv",
    },
    "XLK": {
        "name": "Technology Select Sector SPDR ETF",
        "type": "S&P 500 technology sector",
        "summary": "Targets S&P 500 technology industries including software, hardware, semiconductors, IT services, and equipment.",
        "agent_role": "Broad technology leadership and AI/semiconductor spillover signal.",
        "source": "State Street",
        "url": "https://www.ssga.com/us/en/intermediary/etfs/the-technology-select-sector-spdr-fund-xlk",
    },
    "VGT": {
        "name": "Vanguard Information Technology ETF",
        "type": "US information technology sector ETF",
        "summary": "Tracks a market-cap-weighted index of US information technology companies across software, hardware, semiconductors, and IT services.",
        "agent_role": "Broad technology sector momentum, AI infrastructure, software, and semiconductor cycle signal.",
        "source": "Vanguard",
        "url": "https://investor.vanguard.com/investment-products/etfs/profile/vgt",
    },
    "XLE": {
        "name": "Energy Select Sector SPDR ETF",
        "type": "S&P 500 energy sector",
        "summary": "Targets S&P 500 energy companies in oil, gas, consumable fuels, and energy equipment and services.",
        "agent_role": "Oil, inflation, commodity, and cyclical sector signal.",
        "source": "State Street",
        "url": "https://www.ssga.com/us/en/intermediary/etfs/the-energy-select-sector-spdr-fund-xle",
    },
    "XLF": {
        "name": "Financial Select Sector SPDR ETF",
        "type": "S&P 500 financials sector",
        "summary": "Targets S&P 500 financial companies including banks, insurance, capital markets, REITs, and consumer finance.",
        "agent_role": "Yield-curve, credit, liquidity, and cyclical finance signal.",
        "source": "State Street",
        "url": "https://www.ssga.com/us/en/individual/etfs/the-financial-select-sector-spdr-fund-xlf",
    },
    "SOXX": {
        "name": "iShares Semiconductor ETF",
        "type": "US semiconductor equity",
        "summary": "Tracks US-listed semiconductor companies across design, manufacturing, and distribution.",
        "agent_role": "Concentrated AI hardware and chip-cycle momentum signal.",
        "source": "iShares",
        "url": "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf",
    },
    "ARKK": {
        "name": "ARK Innovation ETF",
        "type": "Active disruptive innovation equity",
        "summary": "Actively invests in companies tied to disruptive innovation themes such as AI, autonomous mobility, digital wallets, and biotech.",
        "agent_role": "High-beta speculative growth and risk-appetite signal.",
        "source": "ARK",
        "url": "https://www.ark-funds.com/funds/arkk",
    },
    "NVDA": {
        "name": "NVIDIA",
        "type": "AI compute and semiconductors",
        "summary": "Builds accelerated computing chips, systems, software, and AI infrastructure platforms.",
        "agent_role": "AI infrastructure bellwether and high-beta mega-cap momentum signal.",
        "source": "NVIDIA",
        "url": "https://www.nvidia.com/en-us/about-nvidia/",
    },
    "TSLA": {
        "name": "Tesla",
        "type": "Electric vehicles and energy",
        "summary": "Designs and builds electric vehicles, batteries, solar products, and scalable energy systems.",
        "agent_role": "High-volatility consumer-tech, EV, and sentiment-driven momentum signal.",
        "source": "Tesla",
        "url": "https://www.tesla.com/about",
    },
    "META": {
        "name": "Meta Platforms",
        "type": "Social platforms and AI",
        "summary": "Operates Facebook, Instagram, WhatsApp, Threads, Meta AI, and Reality Labs products.",
        "agent_role": "Digital ads, social engagement, AI capex, and mega-cap communication-services signal.",
        "source": "Meta",
        "url": "https://about.meta.com/company-info/",
    },
    "AAPL": {
        "name": "Apple",
        "type": "Consumer devices and services",
        "summary": "Designs hardware, software, and services around iPhone, Mac, iPad, wearables, and its services ecosystem.",
        "agent_role": "Consumer hardware, services resilience, and mega-cap quality signal.",
        "source": "Apple",
        "url": "https://investor.apple.com/",
    },
    "AMZN": {
        "name": "Amazon",
        "type": "E-commerce, cloud, ads, logistics",
        "summary": "Runs large-scale retail, marketplace, cloud infrastructure, advertising, logistics, and subscription businesses.",
        "agent_role": "Consumer demand, cloud growth, logistics, and mega-cap discretionary signal.",
        "source": "Amazon",
        "url": "https://www.aboutamazon.com/about-us/",
    },
    "GEV": {
        "name": "GE Vernova",
        "type": "Power, wind, and electrification",
        "summary": "Provides technologies and services that generate, transfer, convert, store, and orchestrate electricity across power, wind, and electrification markets.",
        "agent_role": "Grid buildout, power-generation demand, electrification, energy transition, and AI data-center power signal.",
        "source": "GE Vernova",
        "url": "https://www.gevernova.com/about",
    },
    "CAT": {
        "name": "Caterpillar",
        "type": "Construction, mining, and power equipment",
        "summary": "Manufactures construction and mining equipment, off-highway engines, industrial gas turbines, and diesel-electric locomotives.",
        "agent_role": "Industrial cycle, infrastructure capex, mining, energy, and data-center power equipment demand signal.",
        "source": "Caterpillar",
        "url": "https://www.caterpillar.com/en/company/strategy-purpose/about-caterpillar.html",
    },
    "COIN": {
        "name": "Coinbase",
        "type": "Crypto exchange and infrastructure",
        "summary": "Provides a trusted platform for trading, custody, staking, payments, and onchain infrastructure.",
        "agent_role": "Crypto beta, risk appetite, and digital-asset market structure signal.",
        "source": "Coinbase",
        "url": "https://www.coinbase.com/about/",
    },
    "IBIT": {
        "name": "iShares Bitcoin Trust ETF",
        "type": "Spot bitcoin ETP",
        "summary": "Seeks to reflect the price performance of bitcoin through an exchange-traded product structure.",
        "agent_role": "Direct bitcoin exposure and crypto liquidity signal.",
        "source": "BlackRock",
        "url": "https://www.blackrock.com/us/individual/products/333011/ishares-bitcoin-trust-etf/",
    },
    "BITO": {
        "name": "ProShares Bitcoin Strategy ETF",
        "type": "Bitcoin futures ETF",
        "summary": "Provides bitcoin-linked exposure primarily through bitcoin futures rather than holding spot bitcoin directly.",
        "agent_role": "Futures-based bitcoin sentiment and roll/liquidity contrast to IBIT.",
        "source": "ProShares",
        "url": "https://www.proshares.com/our-etfs/strategic/bito",
    },
    "VT": {
        "name": "Vanguard Total World Stock ETF",
        "type": "Global total-market equity",
        "summary": "Tracks a global market-cap-weighted stock index covering developed and emerging markets.",
        "agent_role": "Global equity backdrop and US-vs-world risk comparison.",
        "source": "Vanguard",
        "url": "https://investor.vanguard.com/investment-products/etfs/profile/vt",
    },
    "VTI": {
        "name": "Vanguard Total Stock Market ETF",
        "type": "Total US equity market",
        "summary": "Tracks a broad US stock market portfolio spanning large-, mid-, small-, and micro-cap companies.",
        "agent_role": "Full US equity-market breadth compared with SPY large caps.",
        "source": "Vanguard",
        "url": "https://investor.vanguard.com/investment-products/etfs/profile/vti",
    },
}


def get_ticker_profile(ticker: str, allow_fetch: bool = True) -> dict:
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return {}
    if symbol in TICKER_PROFILES:
        return TICKER_PROFILES[symbol]
    if symbol in _PROFILE_CACHE:
        return _PROFILE_CACHE[symbol]
    if not allow_fetch or symbol in _FETCH_MISSES:
        return {}

    cached = _load_cached_profile(symbol)
    if cached:
        _PROFILE_CACHE[symbol] = cached
        return cached

    fetched = _fetch_profile_from_yfinance(symbol)
    if fetched:
        _PROFILE_CACHE[symbol] = fetched
        _save_cached_profile(symbol, fetched)
        return fetched

    _FETCH_MISSES.add(symbol)
    return {}


def _load_cached_profile(ticker: str) -> dict:
    try:
        from database.client import get_ticker_profile_cache
        profile = get_ticker_profile_cache(ticker)
        return profile or {}
    except Exception:
        return {}


def _save_cached_profile(ticker: str, profile: dict):
    try:
        from database.client import upsert_ticker_profile_cache
        upsert_ticker_profile_cache(ticker, profile)
    except Exception:
        pass


def _fetch_profile_from_yfinance(ticker: str) -> dict:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).get_info() or {}
    except Exception:
        return {}

    name = (
        info.get("longName")
        or info.get("shortName")
        or info.get("displayName")
        or ticker
    )
    quote_type = (info.get("quoteType") or "").upper()
    sector = info.get("sector")
    industry = info.get("industry")
    category = info.get("category")
    fund_family = info.get("fundFamily")
    website = info.get("website") or f"https://finance.yahoo.com/quote/{ticker}"
    raw_summary = _clean_summary(info.get("longBusinessSummary") or info.get("description") or "")

    type_ = _infer_type(quote_type, sector, industry, category, fund_family)
    summary = raw_summary or _fallback_summary(ticker, name, quote_type, type_)
    return {
        "name": str(name),
        "type": type_,
        "summary": _shorten(summary, 210),
        "agent_role": _infer_agent_role(ticker, quote_type, sector, industry, category, type_),
        "source": "Yahoo Finance",
        "url": website,
        "auto_generated": True,
    }


def _infer_type(quote_type: str, sector: Optional[str], industry: Optional[str],
                category: Optional[str], fund_family: Optional[str]) -> str:
    if quote_type in {"ETF", "MUTUALFUND"}:
        if category:
            return f"{category} fund"
        if fund_family:
            return f"{fund_family} fund"
        return "Exchange-traded fund"
    if quote_type == "EQUITY":
        if sector and industry:
            return f"{sector} / {industry}"
        return sector or industry or "Public company"
    if quote_type:
        return quote_type.replace("_", " ").title()
    return "Market instrument"


def _fallback_summary(ticker: str, name: str, quote_type: str, type_: str) -> str:
    if quote_type in {"ETF", "MUTUALFUND"}:
        return f"{name} is a {type_.lower()} tracked as {ticker}."
    if quote_type == "EQUITY":
        return f"{name} is a publicly traded company tracked as {ticker}."
    return f"{name} is a market instrument tracked as {ticker}."


def _infer_agent_role(ticker: str, quote_type: str, sector: Optional[str],
                      industry: Optional[str], category: Optional[str], type_: str) -> str:
    text = " ".join([ticker, sector or "", industry or "", category or "", type_]).lower()
    if "bond" in text or "treasury" in text:
        return "Rates, duration, and defensive macro signal."
    if "gold" in text or "commodity" in text:
        return "Inflation, real-rate, currency, and defensive stress signal."
    if "bitcoin" in text or "crypto" in text or "blockchain" in text:
        return "Crypto beta, liquidity, and digital-asset risk-appetite signal."
    if "semiconductor" in text or "technology" in text or "software" in text:
        return "Technology momentum, growth appetite, and AI-cycle signal."
    if "health" in text or "pharma" in text or "biotech" in text:
        return "Defensive health care and sector-rotation signal."
    if "financial" in text or "bank" in text or "insurance" in text:
        return "Credit, yield-curve, liquidity, and cyclical finance signal."
    if "energy" in text or "oil" in text or "gas" in text:
        return "Commodity, inflation, and cyclical energy signal."
    if quote_type == "ETF":
        return "Portfolio exposure and cross-asset regime signal."
    return "Single-name momentum, news, and relative-strength signal."


def _clean_summary(text: str) -> str:
    return " ".join(str(text or "").split())


def _shorten(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    trimmed = text[:max_len].rsplit(" ", 1)[0].rstrip(".,;:")
    return f"{trimmed}."


def ticker_profile_html(ticker: str, compact: bool = False) -> str:
    profile = get_ticker_profile(ticker)
    if not profile:
        return ""

    pad = "8px 0" if compact else "10px 0 12px"
    name = escape(profile["name"])
    type_ = escape(profile["type"])
    summary = escape(profile["summary"])
    role = escape(profile["agent_role"])
    source = escape(profile["source"])
    url = escape(profile["url"])
    ticker = escape(str(ticker).upper())

    return f"""
    <div style="padding:{pad};border-bottom:0.5px solid #1a1a1a;margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
        <div>
          <div style="font-size:13px;font-weight:600;color:#eee">{ticker} · {name}</div>
          <div style="font-size:11px;color:#777;margin-top:2px">{type_}</div>
        </div>
        <a href="{url}" target="_blank" style="font-size:11px;color:#00d4a0;text-decoration:none">
          {source}
        </a>
      </div>
      <div style="font-size:12px;color:#aaa;line-height:1.45;margin-top:8px">{summary}</div>
      <div style="font-size:11px;color:#777;line-height:1.45;margin-top:5px">
        Agent context: {role}
      </div>
    </div>
    """
