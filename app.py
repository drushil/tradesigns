"""
app.py — Streamlit dashboard entry point
Run locally:  streamlit run app.py
Deploy:       Push to GitHub → connect Streamlit Cloud
"""
import os
import importlib.util
from pathlib import Path
import streamlit as st
from frontend.ui_help import help_text

# ── Streamlit Cloud secrets → env vars ───────────────────────────────────────
# Streamlit Cloud stores secrets in st.secrets, not os.environ.
# This block pushes them into os.environ so all existing code works unchanged.
try:
    if hasattr(st, "secrets"):
        for k, v in st.secrets.items():
            if isinstance(v, str):
                os.environ.setdefault(k, v)
except st.errors.StreamlitSecretNotFoundError:
    # Local runs can rely on .env; Streamlit Cloud still injects st.secrets.
    pass

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
    "📊 Overview":         "overview",
    "📅 EOD Review":       "eod_review",
    "📡 Live Signals":     "signals",
    "🔄 Trades":           "trades",
    "📈 Performance":      "performance",
    "🏆 Grading":          "grading",
    "🧠 Learning":         "learning",
    "💰 Yield & Sweep":    "yield",
    "📋 Portfolio Review": "portfolio_review",
    "⚙️  Config":          "config_page",
    "📋 Agent Logs":       "logs",
}

with st.sidebar:
    st.markdown("### 🤖 AI Trading Agent")
    st.markdown("---")
    selection = st.radio(
        "Navigation",
        list(PAGES.keys()),
        label_visibility="collapsed",
        help=help_text("Navigation"),
    )
    st.markdown("---")
    try:
        from database.client import get_logs

        logs = get_logs(limit=100)
        latest = logs[0] if logs else {}
        last_seen = str(latest.get("logged_at") or latest.get("created_at") or "—")
        errors = sum(1 for row in logs if str(row.get("level") or "").upper() == "ERROR")
        health_color = "#ff5c5c" if errors else "#00d4a0"
        health_label = "needs attention" if errors else "healthy"
        st.markdown(
            f"""
            <div style="font-size:11px;color:#777;line-height:1.55">
              <div style="text-transform:uppercase;letter-spacing:.08em;color:#555">System health</div>
              <div><span style="color:{health_color}">●</span> {health_label}</div>
              <div>Last log: {last_seen[:16]}</div>
              <div>Errors in recent log: {errors}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    except Exception:
        st.markdown(
            "<div style='font-size:11px;color:#777;'>System health unavailable</div>",
            unsafe_allow_html=True,
        )
    st.markdown("---")
    st.markdown(
        "<div style='font-size:11px;color:#555;'>Paper trading · Alpaca + Claude</div>",
        unsafe_allow_html=True,
    )

def _load_page(module_name: str):
    """Load page modules by path to avoid Streamlit Cloud package import cache issues."""
    page_path = Path(__file__).parent / "frontend" / "pages" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"trading_agent_page_{module_name}", page_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load page module: {module_name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Lazy load — pages only loaded when selected, never at startup
page = _load_page(PAGES[selection])

page.render()
