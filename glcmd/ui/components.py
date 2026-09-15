"""
Shared presentation layer.

Colour is taken from a validated palette (blue / orange / aqua / yellow …) and
applied by *role*, not by rank: status colours are reserved for state, the
sequential blue carries magnitude, and the blue↔red diverging pair carries
polarity (favourable vs unfavourable variance).  Every chart is single-axis.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from .. import audit, config, db, seed

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
          "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

STATUS = {"good": "#0ca30c", "warning": "#fab219",
          "serious": "#ec835a", "critical": "#d03b3b"}

SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
DIVERGING = ["#d03b3b", "#f0efec", "#2a78d6"]   # unfavourable ← neutral → favourable

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    if not db.is_seeded(conn):
        seed.seed_all(conn)
    return conn


def page(title: str, icon: str = "📘", subtitle: str | None = None) -> sqlite3.Connection:
    """Standard page chrome. Returns the shared connection."""
    st.set_page_config(page_title=f"{title} · {config.APP_NAME}", page_icon=icon,
                       layout="wide", initial_sidebar_state="expanded")
    conn = get_conn()
    _inject_css()
    st.markdown(
        f"<div class='gl-head'><h1>{icon}&nbsp;&nbsp;{title}</h1>"
        + (f"<p>{subtitle}</p>" if subtitle else "")
        + "</div>",
        unsafe_allow_html=True,
    )
    return conn


def _inject_css() -> None:
    st.markdown(
        f"""
        <style>
          html, body, [class*="css"] {{ font-family: {FONT}; }}
          .gl-head h1 {{ font-size: 1.6rem; margin: 0 0 .15rem 0; letter-spacing: -.01em;
                         color: {INK}; }}
          .gl-head p  {{ color: {INK_2}; margin: 0 0 1.1rem 0; font-size: .93rem;
                         max-width: 74ch; line-height: 1.5; }}
          .gl-tile {{ border: 1px solid rgba(11,11,11,.10); border-radius: 10px;
                      padding: .75rem .9rem; background: {SURFACE}; height: 100%; }}
          .gl-tile .lbl {{ font-size: .74rem; text-transform: uppercase;
                           letter-spacing: .055em; color: {MUTED}; margin-bottom: .3rem; }}
          .gl-tile .val {{ font-size: 1.45rem; font-weight: 650; color: {INK};
                           line-height: 1.15; }}
          .gl-tile .sub {{ font-size: .78rem; color: {INK_2}; margin-top: .25rem;
                           line-height: 1.35; }}
          .gl-pill {{ display: inline-block; padding: .1rem .5rem; border-radius: 999px;
                      font-size: .74rem; font-weight: 600; border: 1px solid; }}
          .gl-note {{ border-left: 3px solid {SERIES[0]}; background: #f4f8fe;
                      padding: .6rem .85rem; border-radius: 0 8px 8px 0;
                      font-size: .85rem; color: {INK_2}; line-height: 1.5; }}
          .gl-rule {{ border: 0; border-top: 1px solid {GRID}; margin: 1.2rem 0 .9rem; }}
          section[data-testid="stSidebar"] {{ border-right: 1px solid {GRID}; }}
          div[data-testid="stMetricValue"] {{ font-size: 1.4rem; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Sidebar — identity, period, materiality
# --------------------------------------------------------------------------
def sidebar(conn: sqlite3.Connection, show_period: bool = True,
            show_materiality: bool = False) -> dict[str, Any]:
    st.sidebar.markdown(
        f"### {config.APP_NAME}\n<span style='color:{MUTED};font-size:.8rem'>"
        f"{config.APP_TAGLINE}</span>", unsafe_allow_html=True,
    )
    st.sidebar.markdown("---")

    st.sidebar.caption("ACTING AS")
    usernames = [u.username for u in config.USERS]
    default = usernames.index("m.buenaventura")
    actor = st.sidebar.selectbox(
        "Acting as", usernames, index=default,
        format_func=lambda u: config.USER_LABELS[u], label_visibility="collapsed",
        help="Switch identity to test segregation of duties. The app blocks a "
             "preparer from reviewing their own work and logs the attempt.",
    )

    out: dict[str, Any] = {"actor": actor, "role": config.USER_BY_NAME[actor].role}

    if show_period:
        st.sidebar.caption("PERIOD")
        available = db.query(conn, "SELECT DISTINCT period FROM trial_balance ORDER BY period DESC")
        opts = available["period"].tolist() if not available.empty else []
        if not opts:
            opts = [db.get_setting(conn, "active_period", date.today().strftime("%Y-%m"))]
        active = db.get_setting(conn, "active_period", opts[0])
        idx = opts.index(active) if active in opts else 0
        out["period"] = st.sidebar.selectbox("Period", opts, index=idx,
                                             label_visibility="collapsed")

        st.sidebar.caption("ENTITY")
        ent_opts = ["All"] + [e.code for e in config.ENTITIES]
        out["entity"] = st.sidebar.selectbox(
            "Entity", ent_opts, index=0, label_visibility="collapsed",
            format_func=lambda c: "All entities" if c == "All" else config.ENTITY_LABELS[c],
        )

    if show_materiality:
        st.sidebar.markdown("---")
        st.sidebar.caption("MATERIALITY POLICY")
        pct = st.sidebar.number_input("Percentage threshold (%)", 0.0, 100.0,
                                      config.DEFAULT_MATERIALITY.pct_threshold, 0.5)
        amt = st.sidebar.number_input(f"Absolute threshold ({config.GROUP_CURRENCY})",
                                      0.0, 100_000_000.0,
                                      config.DEFAULT_MATERIALITY.abs_threshold, 5_000.0)
        rule = st.sidebar.radio(
            "Gate", ["and", "or"], horizontal=True,
            index=0 if config.DEFAULT_MATERIALITY.rule == "and" else 1,
            help="'and' flags only when both thresholds break — the normal controller "
                 "setting. 'or' is the conservative audit-season setting and will "
                 "flag small accounts with large percentage swings.",
        )
        out["policy"] = config.MaterialityPolicy(pct, amt, rule)

    st.sidebar.markdown("---")
    integrity = audit.verify_chain(conn)
    if integrity["valid"]:
        st.sidebar.markdown(
            f"<span class='gl-pill' style='color:{STATUS['good']};border-color:{STATUS['good']};'>"
            f"✓ Audit trail intact</span><br>"
            f"<span style='font-size:.74rem;color:{MUTED}'>{integrity['checked']:,} "
            f"entries verified</span>", unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown(
            f"<span class='gl-pill' style='color:{STATUS['critical']};"
            f"border-color:{STATUS['critical']};'>✕ Audit trail broken</span><br>"
            f"<span style='font-size:.74rem;color:{MUTED}'>entry "
            f"{integrity['broken_at']}</span>", unsafe_allow_html=True,
        )
    return out


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
def money(v: float | None, dp: int = 0, dash_zero: bool = False) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if dash_zero and abs(v) < 0.005:
        return "—"
    if v < 0:
        return f"({abs(v):,.{dp}f})"
    return f"{v:,.{dp}f}"


def pct(v: float | None, dp: int = 1) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{dp}f}%"


def tile(label: str, value: str, sub: str | None = None,
         tone: str | None = None) -> None:
    colour = STATUS.get(tone or "", INK)
    st.markdown(
        f"<div class='gl-tile'><div class='lbl'>{label}</div>"
        f"<div class='val' style='color:{colour}'>{value}</div>"
        + (f"<div class='sub'>{sub}</div>" if sub else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def tiles(items: list[tuple]) -> None:
    """items = [(label, value, sub, tone), ...]"""
    cols = st.columns(len(items))
    for col, item in zip(cols, items):
        label, value, sub, tone = (list(item) + [None, None])[:4]
        with col:
            tile(label, value, sub, tone)


def pill(text: str, tone: str = "good") -> str:
    colour = STATUS.get(tone, INK_2)
    return (f"<span class='gl-pill' style='color:{colour};border-color:{colour}'>"
            f"{text}</span>")


def note(text: str) -> None:
    st.markdown(f"<div class='gl-note'>{text}</div>", unsafe_allow_html=True)


def rule() -> None:
    st.markdown("<hr class='gl-rule'>", unsafe_allow_html=True)


def status_tone(status: str) -> str:
    return {
        "Complete": "good", "Reviewed": "good", "Posted": "good", "Passed": "good",
        "Pass": "good", "In use": "good", "Matched": "good",
        "In progress": "warning", "Prepared": "warning", "Pending Review": "warning",
        "Approved": "warning", "Draft": "warning", "Open": "warning",
        "Blocked": "critical", "Rejected": "critical", "Fail": "critical",
        "Impairment required": "critical", "True mismatch": "critical",
    }.get(status, "serious")


# --------------------------------------------------------------------------
# Charts — single axis, recessive chrome, legend whenever ≥2 series
# --------------------------------------------------------------------------
def _base(chart: alt.Chart, height: int) -> alt.Chart:
    return (
        chart.properties(height=height, background=SURFACE)
        .configure_view(stroke=None)
        .configure_axis(
            labelFont=FONT, titleFont=FONT, labelColor=MUTED, titleColor=INK_2,
            labelFontSize=11, titleFontSize=11, gridColor=GRID, domainColor=AXIS,
            tickColor=AXIS, gridWidth=1,
        )
        .configure_legend(labelFont=FONT, titleFont=FONT, labelColor=INK_2,
                          titleColor=MUTED, labelFontSize=11, titleFontSize=10,
                          symbolType="square", orient="top", direction="horizontal",
                          titleOrient="left")
        .configure_title(font=FONT, color=INK, fontSize=13, anchor="start",
                         fontWeight=600, offset=10)
    )


def bar_h(df: pd.DataFrame, y: str, x: str, title: str = "",
          colour: str | None = None, tooltip: list[str] | None = None,
          height: int = 300, sort: str | list | None = "-x",
          label_fmt: str = ",.0f") -> None:
    """Horizontal magnitude bars with direct end labels."""
    if df.empty:
        st.caption("No data for this selection.")
        return
    base = alt.Chart(df).encode(
        y=alt.Y(f"{y}:N", sort=sort, title=None,
                axis=alt.Axis(labelLimit=260)),
        x=alt.X(f"{x}:Q", title=None, axis=alt.Axis(grid=True, format="~s")),
        tooltip=tooltip or [y, x],
    )
    bars = base.mark_bar(cornerRadiusEnd=4, height=14,
                         color=colour or SERIES[0])
    labels = base.mark_text(align="left", dx=5, font=FONT, fontSize=10,
                            color=INK_2).encode(text=alt.Text(f"{x}:Q", format=label_fmt))
    st.altair_chart(_base((bars + labels).properties(title=title), height),
                    width='stretch')


def bar_diverging(df: pd.DataFrame, y: str, x: str, title: str = "",
                  tooltip: list[str] | None = None, height: int = 340,
                  label_fmt: str = ",.0f") -> None:
    """Signed variance bars: blue above zero, red below, neutral midpoint."""
    if df.empty:
        st.caption("No data for this selection.")
        return
    base = alt.Chart(df).encode(
        y=alt.Y(f"{y}:N", sort=alt.EncodingSortField(field=x, order="descending"),
                title=None, axis=alt.Axis(labelLimit=300)),
        x=alt.X(f"{x}:Q", title=None, axis=alt.Axis(grid=True, format="~s")),
        tooltip=tooltip or [y, x],
    )
    bars = base.mark_bar(cornerRadiusEnd=4, height=14).encode(
        color=alt.condition(alt.datum[x] >= 0, alt.value(SERIES[0]),
                            alt.value(STATUS["critical"]))
    )
    # `align` is a mark property, not an encoding channel — so the positive and
    # negative labels are two layers rather than one conditional encoding.
    pos = (base.transform_filter(alt.datum[x] >= 0)
           .mark_text(align="left", dx=5, font=FONT, fontSize=10, color=INK_2)
           .encode(text=alt.Text(f"{x}:Q", format=label_fmt)))
    neg = (base.transform_filter(alt.datum[x] < 0)
           .mark_text(align="right", dx=-5, font=FONT, fontSize=10, color=INK_2)
           .encode(text=alt.Text(f"{x}:Q", format=label_fmt)))
    zero = alt.Chart(pd.DataFrame({"z": [0]})).mark_rule(color=AXIS, size=1).encode(x="z:Q")
    st.altair_chart(_base((zero + bars + pos + neg).properties(title=title), height),
                    width='stretch')


def line_series(df: pd.DataFrame, x: str, y: str, series: str | None = None,
                title: str = "", height: int = 280, y_title: str | None = None) -> None:
    """Change over time. One y-axis, always."""
    if df.empty:
        st.caption("No data for this selection.")
        return
    enc: dict[str, Any] = {
        "x": alt.X(f"{x}:N", title=None, axis=alt.Axis(labelAngle=0)),
        "y": alt.Y(f"{y}:Q", title=y_title, axis=alt.Axis(grid=True, format="~s")),
        "tooltip": [x, y] + ([series] if series else []),
    }
    if series:
        enc["color"] = alt.Color(f"{series}:N", title=None,
                                 scale=alt.Scale(range=SERIES))
    base = alt.Chart(df).encode(**enc)
    chart = base.mark_line(size=2, point=alt.OverlayMarkDef(size=60, filled=True))
    st.altair_chart(_base(chart.properties(title=title), height), width='stretch')


def bar_grouped(df: pd.DataFrame, x: str, y: str, series: str,
                title: str = "", height: int = 300) -> None:
    if df.empty:
        st.caption("No data for this selection.")
        return
    chart = alt.Chart(df).mark_bar(cornerRadiusEnd=3, size=18).encode(
        x=alt.X(f"{x}:N", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y(f"{y}:Q", title=None, axis=alt.Axis(grid=True, format="~s")),
        color=alt.Color(f"{series}:N", title=None, scale=alt.Scale(range=SERIES)),
        xOffset=alt.XOffset(f"{series}:N"),
        tooltip=[x, series, y],
    )
    st.altair_chart(_base(chart.properties(title=title), height), width='stretch')


def progress_bar(df: pd.DataFrame, label: str, value: str, title: str = "",
                 height: int = 260) -> None:
    """Completion percentage per category, 0–100 with a reference line at 100."""
    if df.empty:
        st.caption("No data for this selection.")
        return
    d = df.copy()
    d["_full"] = 100.0
    base = alt.Chart(d).encode(
        y=alt.Y(f"{label}:N", sort=None, title=None, axis=alt.Axis(labelLimit=220)),
        tooltip=[label, value],
    )
    scale = alt.Scale(domain=[0, 100], nice=False)
    track = base.mark_bar(height=14, cornerRadius=4, color="#f0efec").encode(
        x=alt.X("_full:Q", title=None, scale=scale,
                axis=alt.Axis(grid=False, values=[0, 25, 50, 75, 100], format="d")),
    )
    bars = base.mark_bar(height=14, cornerRadiusEnd=4, color=SERIES[0]).encode(
        x=alt.X(f"{value}:Q", title=None, scale=scale),
    )
    labels = base.mark_text(align="left", dx=6, font=FONT, fontSize=10, color=INK_2).encode(
        x=alt.X(f"{value}:Q", title=None, scale=scale),
        text=alt.Text(f"{value}:Q", format=".0f"),
    )
    st.altair_chart(_base((track + bars + labels).properties(title=title), height),
                    width='stretch')


def dataframe(df: pd.DataFrame, height: int | None = None, **kwargs) -> None:
    if df.empty:
        st.caption("Nothing to show for this selection.")
        return
    st.dataframe(df, width='stretch', hide_index=True, height=height or "content", **kwargs)
