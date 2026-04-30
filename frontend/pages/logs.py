"""frontend/pages/logs.py — Live agent log viewer."""
import streamlit as st
import pandas as pd


LEVEL_COLORS = {
    "INFO":     "#888",
    "WARN":     "#ffd166",
    "ERROR":    "#ff5c5c",
    "TRADE":    "#00d4a0",
    "SIGNAL":   "#6c63ff",
    "LEARNING": "#4ecdc4",
}


def render():
    st.title("📋 Agent Logs")
    st.caption("Real-time log of every decision, trade, signal and learning event")

    try:
        from database.client import get_logs
    except Exception as e:
        st.error(f"DB error: {e}")
        return

    col_f1, col_f2, col_ref = st.columns([2, 2, 1])
    with col_f1:
        levels = ["All", "TRADE", "SIGNAL", "LEARNING", "INFO", "WARN", "ERROR"]
        sel_level = st.selectbox("Filter by level", levels)
    with col_f2:
        limit = st.selectbox("Show", [50, 100, 200, 500], index=1)
    with col_ref:
        st.markdown("<br>", unsafe_allow_html=True)
        refresh = st.button("🔄 Refresh", width="stretch")

    level_filter = None if sel_level == "All" else sel_level
    logs = get_logs(level=level_filter, limit=limit)

    if not logs:
        st.info("No logs yet. Start the agent to see activity here.")
        return

    # Summary badges
    all_logs = get_logs(limit=1000)
    if all_logs:
        df_all = pd.DataFrame(all_logs)
        badge_cols = st.columns(len(LEVEL_COLORS))
        for i, (level, color) in enumerate(LEVEL_COLORS.items()):
            count = len(df_all[df_all["level"] == level]) if "level" in df_all.columns else 0
            badge_cols[i].markdown(f"""
            <div style="text-align:center;background:#111;border:0.5px solid #222;
                 border-radius:8px;padding:8px">
              <div style="font-size:18px;font-weight:500;color:{color}">{count}</div>
              <div style="font-size:10px;color:#555;text-transform:uppercase;
                    letter-spacing:.06em">{level}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("---")

    # Log stream
    for log in logs:
        level  = log.get("level", "INFO")
        color  = LEVEL_COLORS.get(level, "#888")
        event  = log.get("event", "")
        detail = log.get("detail", {}) or {}
        ts     = str(log.get("logged_at", ""))[:19]

        # Format detail compactly
        detail_str = ""
        if isinstance(detail, dict):
            detail_str = "  ·  ".join(
                f"{k}: {v}" for k, v in list(detail.items())[:4]
                if v is not None and str(v) != ""
            )

        st.markdown(f"""
        <div style="display:flex;gap:12px;align-items:baseline;padding:7px 0;
             border-bottom:0.5px solid #111;font-size:12px">
          <span style="color:#444;font-family:'DM Mono',monospace;
                min-width:140px;flex-shrink:0">{ts}</span>
          <span style="color:{color};font-weight:500;min-width:70px;
                font-family:'DM Mono',monospace">{level}</span>
          <span style="color:#ccc;min-width:180px;flex-shrink:0">{event}</span>
          <span style="color:#555;font-family:'DM Mono',monospace;
                overflow:hidden;text-overflow:ellipsis">{detail_str}</span>
        </div>""", unsafe_allow_html=True)
