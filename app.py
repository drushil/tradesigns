"""
app.py — Streamlit dashboard entry point
Run locally:  streamlit run app.py
Deploy:       Push to GitHub → connect Streamlit Cloud
"""
import os
import streamlit as st

# ── Streamlit Cloud secrets → env vars ───────────────────────────────────────
# Streamlit Cloud stores secrets in st.secrets, not os.environ.
# This block pushes them into os.environ so all existing code works unchanged.
if hasattr(st, "secrets"):
    for k, v in st.secrets.items():
        if isinstance(v, str):
            os.environ.setdefault(k, v)

st.set_page_config(
    page_title="AI Trading Agent",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap');
html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
.stMetric label { font-size: 11px !important; letter-spacing: .06em; text-transform: uppercase; }
.stMetric [data-testid="metric-container"] { background: #0f0f0f; border-radius: 10px; padding: 14px 18px; border: 0.5px solid #222; }
.signal-card { background: #111; border: 0.5px solid #222; border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; }
.signal-name { font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 4px; }
.signal-score { font-size: 28px; font-weight: 500; font-family: 'DM Mono', monospace; }
.positive { color: #00d4a0; }
.negative { color: #ff5c5c; }
.neutral  { color: #888; }
.trade-row { font-family: 'DM Mono', monospace; font-size: 12px; }
.section-header { font-size: 11px; font-weight: 500; letter-spacing: .1em; text-transform: uppercase; color: #555; margin: 24px 0 12px; }
div[data-testid="stSidebar"] { background: #0a0a0a; border-right: 0.5px solid #1a1a1a; }
</style>
""", unsafe_allow_html=True)

PAGES = {
    "📊 Overview":    "overview",
    "📡 Live Signals": "signals",
    "🔄 Trades":      "trades",
    "🧠 Learning":    "learning",
    "⚙️  Config":     "config_page",
    "📋 Agent Logs":  "logs",
}

with st.sidebar:
    st.markdown("### 🤖 AI Trading Agent")
    st.markdown("---")
    selection = st.radio("Navigation", list(PAGES.keys()), label_visibility="collapsed")
    st.markdown("---")
    st.markdown(
        "<div style='font-size:11px;color:#555;'>Paper trading · Alpaca + Claude</div>",
        unsafe_allow_html=True,
    )

# Lazy import — pages only loaded when selected, never at startup
page_module = PAGES[selection]
if page_module == "overview":
    from frontend.pages import overview as page
elif page_module == "signals":
    from frontend.pages import signals as page
elif page_module == "trades":
    from frontend.pages import trades as page
elif page_module == "learning":
    from frontend.pages import learning as page
elif page_module == "config_page":
    from frontend.pages import config_page as page
else:
    from frontend.pages import logs as page

page.render()
