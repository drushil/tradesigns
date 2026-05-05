"""frontend/pages/config_page.py — Live configuration viewer."""
import streamlit as st
import os
from frontend.ticker_profiles import get_ticker_profile, ticker_profile_html


def render():
    st.title("⚙️ Configuration")

    profile_name = os.getenv("RISK_PROFILE", "moderate")
    horizon      = os.getenv("INVESTMENT_HORIZON", "short")
    tickers      = os.getenv("TICKER_UNIVERSE", "SPY,QQQ,GLD")
    capital      = os.getenv("STARTING_CAPITAL_EUR", "100")

    from config.risk_profiles import get_profile, RISK_PROFILES
    profile = get_profile(profile_name)

    st.markdown("### Active Risk Profile")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"""
        <div style="background:#111;border:0.5px solid #222;border-radius:10px;padding:20px">
            <div style="font-size:11px;color:#555;text-transform:uppercase;
                  letter-spacing:.06em;margin-bottom:8px">Active Profile</div>
            <div style="font-size:24px;font-weight:500;color:#fff;margin-bottom:4px">
              {profile['display_name']}</div>
            <div style="font-size:13px;color:#888">
              Horizon: {horizon} · Tickers: {tickers}<br>
              Starting capital: €{capital}
            </div>
        </div>""", unsafe_allow_html=True)

    with c2:
        params = {
            "Max drawdown": f"{profile['max_drawdown_pct']}%",
            "Max position":    f"{profile['max_position_pct']}%",
            "Capital/trade":   f"{profile['capital_per_trade_pct']}%",
            "Cash buffer":     f"{profile['cash_buffer_pct']}%",
            "Stop loss":       f"{profile['stop_loss_pct']}%",
            "Min conviction":  f"{profile['min_conviction']:.0%}",
            "Short selling":   "Enabled" if profile.get("allow_short_selling") else "Disabled",
            "Short cap":       f"{profile.get('max_short_position_pct', 0)}%",
            "VIX ceiling":     str(profile['vix_ceiling']),
            "Max trades/day":  str(profile['max_trades_per_day']),
            "Hold range":      f"{profile['min_hold_minutes']}–{profile['max_hold_minutes']} min",
        }
        for k, v in params.items():
            st.markdown(f"""
            <div style="display:flex;justify-content:space-between;padding:6px 0;
                 border-bottom:0.5px solid #1a1a1a;font-size:13px">
              <span style="color:#888">{k}</span>
              <span style="color:#eee;font-family:'DM Mono',monospace">{v}</span>
            </div>""", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### Ticker Universe")
    st.caption("What each symbol represents and why it is useful for signal learning.")

    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if ticker_list:
        rows = []
        for ticker in ticker_list:
            ticker_profile = get_ticker_profile(ticker)
            rows.append({
                "Ticker": ticker,
                "Name": ticker_profile.get("name", "Unknown"),
                "Type": ticker_profile.get("type", "Unknown"),
                "Agent context": ticker_profile.get("agent_role", "No summary configured"),
            })
        st.dataframe(rows, width="stretch", hide_index=True)

        with st.expander("Ticker summaries", expanded=False):
            for ticker in ticker_list:
                profile_html = ticker_profile_html(ticker, compact=True)
                if profile_html:
                    st.markdown(profile_html, unsafe_allow_html=True)
                else:
                    st.caption(f"{ticker}: no summary configured yet.")
    else:
        st.warning("No tickers configured.")

    st.markdown("---")
    st.markdown("### Signal Weights (current priors)")
    st.caption("7 weighted signal scores plus Earnings Proximity as an 8th multiplier signal.")

    sw = profile.get("signal_weights", {})
    if not sw:
        st.warning(f"No signal weights configured for `{profile_name}`.")
        sw = RISK_PROFILES["moderate"].get("signal_weights", {})
    for sig, w in sw.items():
        st.markdown(f"""
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
          <span style="font-size:12px;color:#888;min-width:200px">{sig.replace('_',' ').title()}</span>
          <div style="flex:1;background:#1a1a1a;border-radius:3px;height:8px">
            <div style="width:{w*100:.0f}%;height:100%;background:#00d4a0;border-radius:3px"></div>
          </div>
          <span style="font-family:'DM Mono',monospace;font-size:12px;
                color:#eee;min-width:40px;text-align:right">{w:.0%}</span>
        </div>""", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### All Profiles Comparison")

    cols = st.columns(len(RISK_PROFILES))
    for i, (name, p) in enumerate(RISK_PROFILES.items()):
        active = name == profile_name
        border = "border:2px solid #00d4a0" if active else "border:0.5px solid #222"
        cols[i].markdown(f"""
        <div style="background:#111;{border};border-radius:10px;padding:12px">
          <div style="font-size:12px;font-weight:500;color:{'#00d4a0' if active else '#eee'};
                margin-bottom:8px">{p['display_name']} {'✓' if active else ''}</div>
          <div style="font-size:11px;color:#555;line-height:1.8">
            Max loss: {p['max_drawdown_pct']}%<br>
            Per trade: {p['capital_per_trade_pct']}%<br>
            VIX cap: {p['vix_ceiling']}<br>
            Shorts: {'yes' if p.get('allow_short_selling') else 'no'}<br>
            Max trades: {p['max_trades_per_day']}/day
          </div>
        </div>""", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### How to change configuration")
    st.caption("Current values shown below. Edit them in `.env`, GitHub Secrets, or Streamlit Cloud Secrets.")
    st.code(f"""# Active configuration
RISK_PROFILE={profile_name}        # conservative|cautious|moderate|growth|aggressive
INVESTMENT_HORIZON={horizon}       # short|mid|both
TICKER_UNIVERSE={tickers}          # comma-separated tickers
STARTING_CAPITAL_EUR={capital}     # your paper trading amount

# New ticker summaries are auto-fetched, then cached in Supabase.
# Local agent restart: python backend/agent.py
# Streamlit Cloud/GitHub Actions: update secrets, then rerun/redeploy.""", language="bash")
