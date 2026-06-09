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
          color-scheme: light;
          --td-bg: #f7f8fa;
          --td-surface: #ffffff;
          --td-surface-soft: #f1f3f5;
          --td-border: rgba(15, 23, 42, 0.12);
          --td-text: #111827;
          --td-heading: #07090c;
          --td-muted: #667085;
          --td-positive: #047857;
          --td-negative: #be123c;
          --td-warning: #b45309;
          --td-info: #2563eb;
          --td-accent: #111827;
          --td-sidebar: #ffffff;
          --td-hover: rgba(15, 23, 42, 0.055);
          --td-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
          --td-tooltip-bg: #ffffff;
          --td-tooltip-text: #111827;
        }}

        @media (prefers-color-scheme: dark) {{
          :root {{
            color-scheme: dark;
            --td-bg: #080a0d;
            --td-surface: #111418;
            --td-surface-soft: #171b21;
            --td-border: rgba(148, 163, 184, 0.18);
            --td-text: #eef2f7;
            --td-heading: #f8fafc;
            --td-muted: #8b949e;
            --td-positive: #34d399;
            --td-negative: #fb7185;
            --td-warning: #fbbf24;
            --td-info: #60a5fa;
            --td-accent: #f8fafc;
            --td-sidebar: #090b0f;
            --td-hover: rgba(255, 255, 255, 0.055);
            --td-shadow: 0 16px 40px rgba(0, 0, 0, 0.22);
            --td-tooltip-bg: #05070a;
            --td-tooltip-text: #dbe7f3;
          }}
        }}

        html, body, [class*="css"] {{
          font-family: 'Inter', 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
          letter-spacing: 0;
        }}

        .stApp {{
          background: var(--td-bg);
          color: var(--td-text);
        }}

        .block-container {{
          padding-top: 2.25rem;
          padding-bottom: 3rem;
          max-width: 1440px;
        }}

        div[data-testid="stSidebar"] {{
          background: var(--td-sidebar);
          border-right: 1px solid var(--td-border);
        }}

        div[data-testid="stSidebar"] section {{
          background: var(--td-sidebar);
        }}

        div[data-testid="stSidebar"] [role="radiogroup"] label {{
          border-radius: 8px;
          padding: 6px 9px;
          min-height: 36px;
          color: var(--td-text);
        }}

        div[data-testid="stSidebar"] [role="radiogroup"] label:hover {{
          background: var(--td-hover);
        }}

        .td-brand {{
          padding: 16px 2px 10px;
          border-bottom: 1px solid var(--td-border);
        }}

        .td-brand-title {{
          color: var(--td-heading);
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
          padding: 2px 0 22px;
          border-bottom: 1px solid var(--td-border);
          margin-bottom: 20px;
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
          color: var(--td-heading);
          font-size: 32px;
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

        .td-toolbar {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 14px;
          padding: 14px;
          margin: 2px 0 18px;
          border: 1px solid var(--td-border);
          border-radius: 8px;
          background: var(--td-surface);
          box-shadow: var(--td-shadow);
        }}

        .td-toolbar-title {{
          color: var(--td-heading);
          font-size: 14px;
          font-weight: 750;
          margin-bottom: 3px;
        }}

        .td-toolbar-copy {{
          color: var(--td-muted);
          font-size: 12px;
          line-height: 1.4;
        }}

        .td-insight-grid {{
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 12px;
          margin: 8px 0 16px;
        }}

        .td-insight-card {{
          border: 1px solid var(--td-border);
          border-radius: 8px;
          background: var(--td-surface);
          padding: 14px;
          min-height: 92px;
        }}

        .td-insight-card strong {{
          color: var(--td-heading);
          font-size: 14px;
        }}

        .td-insight-card div {{
          color: var(--td-muted);
          font-size: 12px;
          line-height: 1.45;
          margin-top: 6px;
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
          background: var(--td-hover);
          color: var(--td-text);
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
          background: var(--td-surface);
          box-shadow: var(--td-shadow);
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
          background: var(--td-tooltip-bg);
          color: var(--td-tooltip-text);
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
          background: var(--td-tooltip-bg);
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
          color: var(--td-heading);
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
          color: var(--td-heading);
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
          background: var(--td-surface);
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

        div[data-testid="stVerticalBlockBorderWrapper"] {{
          border-color: var(--td-border);
          background: var(--td-surface);
        }}

        div[data-baseweb="tab-list"] {{
          gap: 6px;
          border-bottom: 1px solid var(--td-border);
        }}

        button[data-baseweb="tab"] {{
          border-radius: 8px 8px 0 0;
          color: var(--td-muted);
          font-weight: 650;
        }}

        button[data-baseweb="tab"][aria-selected="true"] {{
          color: var(--td-heading);
          background: var(--td-surface);
        }}

        div[data-testid="stSegmentedControl"] label {{
          background: var(--td-surface);
          border-color: var(--td-border);
          color: var(--td-text);
        }}

        div[data-testid="stSegmentedControl"] label[data-baseweb="radio"] {{
          border-radius: 8px;
        }}

        div[data-baseweb="select"] > div,
        div[data-baseweb="input"] input,
        textarea,
        input {{
          background: var(--td-surface);
          color: var(--td-text);
          border-color: var(--td-border);
        }}

        div[data-baseweb="popover"],
        div[data-baseweb="menu"] {{
          background: var(--td-surface);
          color: var(--td-text);
        }}

        label, p, span {{
          letter-spacing: 0;
        }}

        div[data-testid="stDataFrame"] {{
          border: 1px solid var(--td-border);
          border-radius: 8px;
          overflow: hidden;
          margin-top: 0 !important;
        }}

        .stButton > button {{
          border-radius: 8px;
          border: 1px solid var(--td-border);
          background: var(--td-surface);
          color: var(--td-heading);
          font-weight: 650;
        }}

        .stButton > button:hover {{
          border-color: var(--td-heading);
          color: var(--td-heading);
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

        @media (prefers-color-scheme: dark) {{
          .stApp {{
            background: var(--td-bg);
            color: var(--td-text);
          }}
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
          .td-toolbar {{
            display: block;
          }}
          .td-insight-grid {{
            grid-template-columns: 1fr;
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
        f'<div style="color:var(--td-text);font-size:14px;line-height:1.45">{body}</div>'
        f'{meta_html}</div>'
    )


def apply_plotly_theme(fig, *, height: int | None = None, showlegend: bool | None = None):
    layout = dict(
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=10, b=0),
        xaxis=dict(gridcolor="rgba(148, 163, 184, 0.12)", zerolinecolor="rgba(148, 163, 184, 0.16)"),
        yaxis=dict(gridcolor="rgba(148, 163, 184, 0.12)", zerolinecolor="rgba(148, 163, 184, 0.16)"),
        font=dict(family="Inter, sans-serif", color="#667085", size=12),
    )
    if height is not None:
        layout["height"] = height
    if showlegend is not None:
        layout["showlegend"] = showlegend
    fig.update_layout(**layout)
    return fig
