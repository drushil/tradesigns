"""Operations hub for advisory reviews, configuration, and logs."""
from __future__ import annotations

import streamlit as st

from frontend.ui_theme import page_header, status_pill


def _render_child(module_name: str):
    try:
        module = __import__(f"frontend.pages.{module_name}", fromlist=["render"])
        module.render()
    except Exception as exc:
        st.error(f"Could not load {module_name}: {str(exc)[:220]}")


def render():
    page_header(
        "Operations",
        "Daily review, weekly review, configuration, and runtime logs for the advisory stack.",
        eyebrow="Control room",
        pills=[
            status_pill("reviews", "info"),
            status_pill("config", "neutral"),
            status_pill("logs", "neutral"),
        ],
    )

    daily_tab, weekly_tab, config_tab, logs_tab = st.tabs(
        ["Daily Review", "Weekly Review", "Config", "Logs"]
    )

    with daily_tab:
        _render_child("eod_review")
    with weekly_tab:
        _render_child("portfolio_review")
    with config_tab:
        _render_child("config_page")
    with logs_tab:
        _render_child("logs")
