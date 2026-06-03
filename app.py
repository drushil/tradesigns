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
from frontend.ui_theme import inject_theme

# ── Streamlit Cloud secrets → env vars ───────────────────────────────────────
# Streamlit Cloud stores secrets in st.secrets, not os.environ.
# This block pushes them into os.environ so all existing code works unchanged.
try:
    if hasattr(st, "secrets"):
        for k, v in st.secrets.items():
            if isinstance(v, str):
                # Streamlit Cloud secrets are the dashboard's configured source
                # of truth. Override stale process env values from old deploys.
                os.environ[k] = v
except st.errors.StreamlitSecretNotFoundError:
    # Local runs can rely on .env; Streamlit Cloud still injects st.secrets.
    pass

st.set_page_config(
    page_title="AI Trading Agent",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

inject_theme()

PAGES = {
    "📊 Overview":         "overview",
    "📅 EOD Review":       "eod_review",
    "🚧 Blocked Ops":      "blocked_opportunities",
    "📡 Live Signals":     "signals",
    "🌅 Pre-Market":       "premarket",
    "🎯 Advisory":         "advisory",
    "🔄 Trades":           "trades",
    "📈 Performance":      "performance",
    "🏆 Grading":          "grading",
    "🧠 Learning":         "learning",
    "💰 Yield & Sweep":    "yield",
    "📋 Portfolio Review": "portfolio_review",
    "⚙️  Config":          "config_page",
    "📋 Agent Logs":       "logs",
}

# Reverse map: module slug → full page label (for ?page= deep-links)
_SLUG_TO_PAGE = {module: label for label, module in PAGES.items()}
# Also accept short aliases
_SLUG_TO_PAGE.update({
    "eod":       "📅 EOD Review",
    "blocked":   "🚧 Blocked Ops",
    "pre-market": "🌅 Pre-Market",
    "pre_market": "🌅 Pre-Market",
    "portfolio": "📋 Portfolio Review",
    "config":    "⚙️  Config",
})

def _query_param_present(name: str) -> bool:
    try:
        value = st.query_params.get(name)
        if isinstance(value, list):
            return bool(value and value[0])
        return bool(value)
    except Exception:
        return False

def _page_from_query_params(page_names: list) -> str:
    """Return the page label to select based on ?page= or ?mark_id= query params."""
    try:
        if _query_param_present("mark_id"):
            return "🎯 Advisory"
        slug = st.query_params.get("page", "")
        if isinstance(slug, list):
            slug = slug[0] if slug else ""
        slug = str(slug).strip().lower()
        if slug:
            candidate = _SLUG_TO_PAGE.get(slug)
            if candidate and candidate in page_names:
                return candidate
    except Exception:
        pass
    return page_names[0]


def _sync_page_query_param(selection: str):
    """Keep the browser URL aligned with the selected sidebar page."""
    try:
        slug = PAGES.get(selection)
        if not slug:
            return
        current = st.query_params.get("page", "")
        if isinstance(current, list):
            current = current[0] if current else ""
        if str(current).strip().lower() != slug:
            st.query_params["page"] = slug
    except Exception:
        pass


with st.sidebar:
    st.markdown(
        """
        <div class="td-brand">
          <div class="td-brand-title">AI Trading Agent</div>
          <div class="td-brand-subtitle">Paper trading command center</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("---")
    page_names = list(PAGES.keys())
    default_page = _page_from_query_params(page_names)
    selection = st.radio(
        "Navigation",
        page_names,
        index=page_names.index(default_page),
        label_visibility="collapsed",
        help=help_text("Navigation"),
    )
    _sync_page_query_param(selection)
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
