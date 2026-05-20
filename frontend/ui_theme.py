"""Modern Streamlit UI primitives for the trading dashboard."""
from __future__ import annotations

import html
from typing import Iterable

import streamlit as st


ACCENT = "#4fd1c5"
POSITIVE = "#34d399"
NEGATIVE = "#fb7185"
WARNING = "#fbbf24"
INFO = "#60a5fa"
MUTED = "#8b949e"
SURFACE = "#111418"
SURFACE_SOFT = "#171b21"
BORDER = "rgba(148, 163, 184, 0.18)"


def inject_theme():
    """Install the shared visual system. Safe to call repeatedly."""
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Inter:wght@400;500;600;700&display=swap');

        :root {{
          --td-bg: #080a0d;
          --td-surface: {SURFACE};
          --td-surface-soft: {SURFACE_SOFT};
          --td-border: {BORDER};
          --td-text: #eef2f7;
          --td-muted: {MUTED};
          --td-positive: {POSITIVE};
          --td-negative: {NEGATIVE};
          --td-warning: {WARNING};
          --td-info: {INFO};
          --td-accent: {ACCENT};
        }}

        html, body, [class*="css"] {{
          font-family: 'Inter', 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
          letter-spacing: 0;
        }}

        .stApp {{
          background:
            linear-gradient(180deg, rgba(12, 16, 22, 0.92), rgba(8, 10, 13, 1) 32%),
            #080a0d;
          color: var(--td-text);
        }}

        .block-container {{
          padding-top: 3.25rem;
          padding-bottom: 3rem;
          max-width: 1440px;
        }}

        div[data-testid="stSidebar"] {{
          background: #090b0f;
          border-right: 1px solid var(--td-border);
        }}

        div[data-testid="stSidebar"] [role="radiogroup"] label {{
          border-radius: 8px;
          padding: 4px 8px;
        }}

        div[data-testid="stSidebar"] [role="radiogroup"] label:hover {{
          background: rgba(255, 255, 255, 0.04);
        }}

        .td-brand {{
          padding: 8px 2px 2px;
        }}

        .td-brand-title {{
          color: #f8fafc;
          font-size: 17px;
          font-weight: 700;
          letter-spacing: 0;
          line-height: 1.1;
        }}

        .td-brand-subtitle {{
          color: var(--td-muted);
          font-size: 11px;
          margin-top: 5px;
        }}

        .td-page-header {{
          display: flex;
          justify-content: space-between;
          align-items: flex-end;
          gap: 20px;
          padding: 4px 0 20px;
          border-bottom: 1px solid var(--td-border);
          margin-bottom: 18px;
        }}

        .td-eyebrow {{
          color: var(--td-accent);
          font-size: 11px;
          font-weight: 700;
          letter-spacing: .08em;
          text-transform: uppercase;
          margin-bottom: 7px;
        }}

        .td-page-title {{
          color: #f8fafc;
          font-size: 31px;
          font-weight: 700;
          letter-spacing: 0;
          line-height: 1.12;
          margin: 0;
        }}

        .td-page-subtitle {{
          color: var(--td-muted);
          max-width: 760px;
          font-size: 14px;
          line-height: 1.55;
          margin-top: 8px;
        }}

        .td-header-meta {{
          display: flex;
          align-items: center;
          justify-content: flex-end;
          gap: 8px;
          flex-wrap: wrap;
          min-width: 180px;
        }}

        .td-pill {{
          display: inline-flex;
          align-items: center;
          gap: 7px;
          height: 28px;
          padding: 0 10px;
          border-radius: 999px;
          border: 1px solid var(--td-border);
          background: rgba(255, 255, 255, 0.035);
          color: #cbd5e1;
          font-size: 12px;
          font-weight: 600;
          white-space: nowrap;
        }}

        .td-pill-dot {{
          width: 7px;
          height: 7px;
          border-radius: 50%;
          background: var(--td-muted);
        }}

        .td-pill.positive .td-pill-dot {{ background: var(--td-positive); }}
        .td-pill.negative .td-pill-dot {{ background: var(--td-negative); }}
        .td-pill.warning .td-pill-dot {{ background: var(--td-warning); }}
        .td-pill.info .td-pill-dot {{ background: var(--td-info); }}

        .td-metric-card {{
          min-height: 112px;
          padding: 16px 16px 14px;
          margin-bottom: 16px;
          border: 1px solid var(--td-border);
          border-radius: 8px;
          background:
            linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.015)),
            var(--td-surface);
          box-shadow: 0 16px 40px rgba(0, 0, 0, 0.18);
        }}

        .td-has-tooltip {{
          position: relative;
          cursor: help;
        }}

        .td-has-tooltip:hover::after {{
          content: attr(data-tooltip);
          position: absolute;
          z-index: 1000;
          left: 12px;
          top: calc(100% + 8px);
          max-width: min(360px, 80vw);
          width: max-content;
          padding: 9px 11px;
          border-radius: 7px;
          border: 1px solid rgba(148, 163, 184, 0.28);
          background: #05070a;
          color: #dbe7f3;
          font-size: 12px;
          font-weight: 500;
          line-height: 1.35;
          letter-spacing: 0;
          text-transform: none;
          white-space: normal;
          box-shadow: 0 18px 38px rgba(0, 0, 0, 0.42);
          pointer-events: none;
        }}

        .td-has-tooltip:hover::before {{
          content: "";
          position: absolute;
          z-index: 1001;
          left: 22px;
          top: calc(100% + 3px);
          width: 10px;
          height: 10px;
          transform: rotate(45deg);
          background: #05070a;
          border-left: 1px solid rgba(148, 163, 184, 0.28);
          border-top: 1px solid rgba(148, 163, 184, 0.28);
          pointer-events: none;
        }}

        .td-metric-card.td-has-tooltip:hover::after {{
          top: auto;
          bottom: calc(100% + 8px);
        }}

        .td-metric-card.td-has-tooltip:hover::before {{
          top: auto;
          bottom: calc(100% + 3px);
          border-left: 0;
          border-top: 0;
          border-right: 1px solid rgba(148, 163, 184, 0.28);
          border-bottom: 1px solid rgba(148, 163, 184, 0.28);
        }}

        .td-metric-label {{
          color: var(--td-muted);
          font-size: 11px;
          font-weight: 700;
          letter-spacing: .08em;
          text-transform: uppercase;
        }}

        .td-metric-value {{
          color: #f8fafc;
          font-family: 'DM Mono', monospace;
          font-size: 25px;
          font-weight: 500;
          line-height: 1.15;
          margin-top: 13px;
          overflow-wrap: anywhere;
        }}

        .td-metric-delta {{
          color: var(--td-muted);
          font-size: 12px;
          margin-top: 8px;
          line-height: 1.35;
        }}

        .td-metric-card.positive .td-metric-value,
        .td-metric-delta.positive {{ color: var(--td-positive); }}
        .td-metric-card.negative .td-metric-value,
        .td-metric-delta.negative {{ color: var(--td-negative); }}
        .td-metric-card.warning .td-metric-value,
        .td-metric-delta.warning {{ color: var(--td-warning); }}
        .td-metric-card.info .td-metric-value,
        .td-metric-delta.info {{ color: var(--td-info); }}

        .element-container:has(.td-metric-card) {{
          margin-bottom: 10px;
        }}

        div[data-testid="column"]:has(.td-metric-card) {{
          padding-bottom: 12px;
        }}

        .td-section {{
          margin: 20px 0 8px;
        }}

        .td-section-title {{
          color: #f1f5f9;
          font-size: 15px;
          font-weight: 700;
          letter-spacing: 0;
          margin: 0;
          display: inline-block;
        }}

        .td-section-subtitle {{
          color: var(--td-muted);
          font-size: 12px;
          margin-top: 4px;
          line-height: 1.45;
        }}

        .signal-card, .td-panel {{
          background:
            linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.012)),
            var(--td-surface);
          border: 1px solid var(--td-border);
          border-radius: 8px;
          padding: 14px 16px;
          margin-bottom: 10px;
        }}

        .signal-name {{
          font-size: 11px;
          color: var(--td-muted);
          text-transform: uppercase;
          letter-spacing: .08em;
          margin-bottom: 6px;
          font-weight: 700;
        }}

        .signal-score {{
          font-size: 26px;
          font-weight: 500;
          font-family: 'DM Mono', monospace;
          letter-spacing: 0;
        }}

        .positive {{ color: var(--td-positive); }}
        .negative {{ color: var(--td-negative); }}
        .neutral  {{ color: var(--td-muted); }}
        .warning  {{ color: var(--td-warning); }}
        .info     {{ color: var(--td-info); }}

        .section-header {{
          font-size: 11px;
          font-weight: 700;
          letter-spacing: .1em;
          text-transform: uppercase;
          color: var(--td-muted);
          margin: 24px 0 12px;
        }}

        .stMetric [data-testid="metric-container"] {{
          background: var(--td-surface);
          border-radius: 8px;
          padding: 14px 16px;
          border: 1px solid var(--td-border);
        }}

        div[data-testid="stDataFrame"] {{
          border: 1px solid var(--td-border);
          border-radius: 8px;
          overflow: hidden;
          margin-top: 0 !important;
        }}

        .element-container:has(div[data-testid="stDataFrame"]) {{
          margin-top: -4px;
        }}

        .element-container:has(.td-section) + .element-container:has(div[data-testid="stDataFrame"]) {{
          margin-top: -10px;
        }}

        .element-container:has(.td-section) + .element-container:has(div[data-testid="stAlert"]) {{
          margin-top: -8px;
        }}

        hr {{
          border-color: var(--td-border);
          margin: 24px 0;
        }}

        @media (max-width: 760px) {{
          .td-page-header {{
            display: block;
          }}
          .td-header-meta {{
            justify-content: flex-start;
            margin-top: 12px;
          }}
          .td-page-title {{
            font-size: 25px;
          }}
          .td-metric-value {{
            font-size: 21px;
          }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _tone_class(tone: str | None) -> str:
    return tone if tone in {"positive", "negative", "warning", "info"} else ""


def status_pill(label: str, tone: str = "neutral") -> str:
    safe_label = html.escape(str(label))
    return f'<span class="td-pill {_tone_class(tone)}"><span class="td-pill-dot"></span>{safe_label}</span>'


def page_header(title: str, subtitle: str | None = None, eyebrow: str | None = None,
                pills: Iterable[str] | None = None):
    eyebrow_html = f'<div class="td-eyebrow">{html.escape(eyebrow)}</div>' if eyebrow else ""
    subtitle_html = f'<div class="td-page-subtitle">{html.escape(subtitle)}</div>' if subtitle else ""
    pills_html = "".join(pills or [])
    st.markdown(
        f"""
        <div class="td-page-header">
          <div>
            {eyebrow_html}
            <h1 class="td-page-title">{html.escape(title)}</h1>
            {subtitle_html}
          </div>
          <div class="td-header-meta">{pills_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def metric_card(container, label: str, value, delta=None, tone: str = "neutral", help_text: str | None = None):
    delta_html = ""
    if delta is not None:
        delta_html = f'<div class="td-metric-delta {_tone_class(tone)}">{html.escape(str(delta))}</div>'
    title = help_text or str(label)
    title_attr = f' title="{html.escape(title, quote=True)}" data-tooltip="{html.escape(title, quote=True)}"'
    container.markdown(
        f"""
        <div class="td-metric-card td-has-tooltip {_tone_class(tone)}"{title_attr}>
          <div class="td-metric-label">{html.escape(str(label))}</div>
          <div class="td-metric-value">{html.escape(str(value))}</div>
          {delta_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def modern_section(title: str, subtitle: str | None = None, help_text: str | None = None):
    hover = help_text or subtitle or title
    title_attr = f' title="{html.escape(hover, quote=True)}" data-tooltip="{html.escape(hover, quote=True)}"'
    st.markdown(
        f"""
        <div class="td-section">
          <div class="td-section-title td-has-tooltip"{title_attr}>{html.escape(title)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def panel_html(title: str, body: str, meta: str | None = None) -> str:
    meta_html = f'<div style="color:var(--td-muted);font-size:12px;margin-top:7px">{html.escape(meta)}</div>' if meta else ""
    return (
        '<div class="td-panel">'
        f'<div class="signal-name">{html.escape(title)}</div>'
        f'<div style="color:#e5edf7;font-size:14px;line-height:1.45">{body}</div>'
        f'{meta_html}</div>'
    )


def apply_plotly_theme(fig, *, height: int | None = None, showlegend: bool | None = None):
    layout = dict(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=10, b=0),
        xaxis=dict(gridcolor="rgba(148, 163, 184, 0.12)", zerolinecolor="rgba(148, 163, 184, 0.16)"),
        yaxis=dict(gridcolor="rgba(148, 163, 184, 0.12)", zerolinecolor="rgba(148, 163, 184, 0.16)"),
        font=dict(family="Inter, sans-serif", color="#cbd5e1", size=12),
    )
    if height is not None:
        layout["height"] = height
    if showlegend is not None:
        layout["showlegend"] = showlegend
    fig.update_layout(**layout)
    return fig
