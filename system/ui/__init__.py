"""
UI Theme — shared CSS, color tokens, and style utilities.

Design system: Accessible & Ethical (Academic)
Primary: #7C3AED  |  Secondary: #A78BFA  |  CTA: #F97316
Background: #FAF5FF  |  Surface: #FFFFFF  |  Text: #1E0B4A
"""

from __future__ import annotations

# ---- Color tokens ----
COLORS = {
    "primary": "#7C3AED",
    "primary_light": "#A78BFA",
    "primary_bg": "#F5F0FF",
    "cta": "#F97316",
    "cta_hover": "#EA580C",
    "bg": "#FAF5FF",
    "surface": "#FFFFFF",
    "text": "#1E0B4A",
    "text_secondary": "#6B5B8A",
    "text_muted": "#9A8AB5",
    "border": "#E8E0F0",
    "success": "#10B981",
    "warning": "#F59E0B",
    "danger": "#EF4444",
    "info": "#3B82F6",
}

STATUS_COLORS = {
    "candidate": "#6B7280",
    "saved": "#3B82F6",
    "read": "#10B981",
    "deep_read": "#7C3AED",
    "ignored": "#9CA3AF",
    "pending": "#F59E0B",
    "running": "#3B82F6",
    "done": "#10B981",
    "failed": "#EF4444",
}

LEVEL_COLORS = {
    "primary": "#10B981",
    "secondary": "#F59E0B",
    "tertiary": "#EF4444",
}

GLOBAL_CSS = """
/* ---- Typography ---- */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    color: #1E0B4A;
}

/* ---- Headers ---- */
h1, h2, h3, h4, h5, h6 {
    font-weight: 600;
    letter-spacing: -0.02em;
    color: #1E0B4A;
}

h1 { font-size: 1.75rem; }
h2 { font-size: 1.35rem; }
h3 { font-size: 1.15rem; }

/* ---- Cards ---- */
div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"] {
    background: #FFFFFF;
    border: 1px solid #E8E0F0;
    border-radius: 12px;
    padding: 1.25rem;
    margin-bottom: 0.75rem;
    transition: box-shadow 0.2s ease, border-color 0.2s ease;
}

div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"]:hover {
    border-color: #A78BFA;
    box-shadow: 0 2px 12px rgba(124, 58, 237, 0.08);
}

/* ---- Buttons ---- */
div[data-testid="stButton"] > button {
    border-radius: 8px;
    font-weight: 500;
    font-size: 0.875rem;
    transition: all 0.2s ease;
    border: 1px solid #E8E0F0;
    padding: 0.4rem 1rem;
}

div[data-testid="stButton"] > button[kind="primary"] {
    background: #7C3AED;
    color: white;
    border-color: #7C3AED;
}

div[data-testid="stButton"] > button[kind="primary"]:hover {
    background: #6D28D9;
    border-color: #6D28D9;
}

div[data-testid="stButton"] > button[kind="secondary"] {
    background: transparent;
    color: #6B5B8A;
    border-color: #E8E0F0;
}

/* ---- Expandable sections ---- */
details, summary {
    border-radius: 8px;
}

/* ---- Metrics ---- */
div[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #E8E0F0;
    border-radius: 10px;
    padding: 1rem;
}

div[data-testid="stMetric"] label {
    font-weight: 500;
    color: #6B5B8A;
}

/* ---- Sidebar ---- */
section[data-testid="stSidebar"] {
    background: #FFFFFF;
    border-right: 1px solid #E8E0F0;
}

section[data-testid="stSidebar"] h2 {
    color: #7C3AED;
}

/* ---- Chat messages ---- */
div[data-testid="stChatMessage"] {
    border-radius: 12px;
    padding: 1rem;
}

/* ---- Select boxes & inputs ---- */
div[data-testid="stSelectbox"] > div,
div[data-testid="stTextInput"] > div {
    border-radius: 8px;
}

input, textarea, select {
    border-radius: 8px !important;
    border-color: #E8E0F0 !important;
}

input:focus, textarea:focus, select:focus {
    border-color: #7C3AED !important;
    box-shadow: 0 0 0 3px rgba(124, 58, 237, 0.12) !important;
}

/* ---- Status badges ---- */
.status-badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}

.level-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

/* ---- Divider ---- */
hr {
    border-color: #E8E0F0;
    margin: 0.75rem 0;
}

/* ---- Tab bar ---- */
div[data-testid="stTabs"] button {
    font-weight: 500;
    font-size: 0.9rem;
    border-radius: 8px 8px 0 0;
}

div[data-testid="stTabs"] button[aria-selected="true"] {
    color: #7C3AED;
    border-bottom-color: #7C3AED;
}

/* ---- Info / warning / error boxes ---- */
div[data-testid="stAlert"] {
    border-radius: 10px;
    border-left-width: 4px;
}

/* ---- Expanders ---- */
details summary {
    color: #6B5B8A;
    font-weight: 500;
}
"""


def inject_global_css():
    """Inject the global CSS stylesheet into the Streamlit app."""
    import streamlit as st
    st.markdown(f"<style>{GLOBAL_CSS}</style>", unsafe_allow_html=True)


def status_badge_html(status: str) -> str:
    """Generate an HTML status badge."""
    color = STATUS_COLORS.get(status, "#6B7280")
    return (
        f'<span class="status-badge" style="background:{color}15;color:{color};'
        f'border:1px solid {color}30;">{status}</span>'
    )


def level_badge_html(level: str) -> str:
    """Generate an HTML source-level badge."""
    color = LEVEL_COLORS.get(level, "#6B7280")
    label = level or "unrated"
    return (
        f'<span class="level-badge" style="background:{color}12;color:{color};'
        f'border:1px solid {color}30;">{label}</span>'
    )


def card_html(title: str, subtitle: str = "", body: str = "", badge: str = "") -> str:
    """Generate a simple card as HTML."""
    badge_html_str = level_badge_html(badge) if badge else ""
    parts = [f'<div style="background:#FFFFFF;border:1px solid #E8E0F0;border-radius:12px;padding:1.25rem;margin-bottom:0.75rem;">']
    parts.append(f'<div style="font-weight:600;font-size:1rem;color:#1E0B4A;margin-bottom:0.25rem;">{title} {badge_html_str}</div>')
    if subtitle:
        parts.append(f'<div style="font-size:0.8rem;color:#6B5B8A;margin-bottom:0.5rem;">{subtitle}</div>')
    if body:
        parts.append(f'<div style="font-size:0.875rem;color:#4C1D95;line-height:1.6;">{body[:400]}</div>')
    parts.append("</div>")
    return "".join(parts)


def score_color(score: float) -> str:
    """Return a hex color based on score (0-10)."""
    if score >= 7:
        return "#10B981"
    if score >= 5:
        return "#F59E0B"
    return "#EF4444"


def section_header(title: str, description: str = ""):
    """Render a consistent section header."""
    import streamlit as st
    st.markdown(
        f'<div style="margin-bottom:1rem;">'
        f'<h3 style="margin:0;color:#1E0B4A;">{title}</h3>'
        f'{"<p style='font-size:0.85rem;color:#6B5B8A;margin:0.25rem 0 0 0;'>" + description + "</p>" if description else ""}'
        f'</div>',
        unsafe_allow_html=True,
    )
