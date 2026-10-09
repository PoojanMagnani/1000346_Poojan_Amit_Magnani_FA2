"""
LogiSight Analytics — Last-Mile Delivery Dashboard (FA-2)
=========================================================
Built from the FA-1 design blueprint: FILTER > COMPARE > INVESTIGATE > ACT.

Data  : amazon_delivery.csv (43,739 last-mile orders, Delivery_Time in minutes)
Stack : pandas for cleaning, plotly for charts (2D + 3D), pydeck for the 3D map,
        Streamlit for the interface.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pydeck as pdk
import streamlit as st

# ---------------------------------------------------------------------------
# Page + palette (taken from the FA-1 infographic)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Last-Mile Delivery Dashboard · LogiSight",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

NAVY = "#1E1B4B"
INDIGO = "#4B32C3"
ORANGE = "#FF7A45"
YELLOW = "#FFE15A"
MAGENTA = "#EE4D86"
TEAL = "#0F8F82"
RED = "#E8483B"
PURPLE = "#8B7BE0"

CARD = {
    "lime": "#E6F6D3",
    "butter": "#FFE9A6",
    "lavender": "#E8E2FB",
    "salmon": "#FFDCCB",
    "mint": "#CBF1EC",
    "pink": "#FFD3E4",
}

GREEN_RAMP = ["#E4F3CF", "#B7DF96", "#79BE62", "#2F8A45", "#13552B"]
SALMON_RAMP = ["#FFF1E8", "#FFC8AE", "#FF9A73", "#F0613F", "#B8301E"]
WEATHER_COLORS = {
    "Sunny": "#F2B705",
    "Cloudy": "#8B7BE0",
    "Windy": TEAL,
    "Fog": "#7A88A8",
    "Stormy": NAVY,
    "Sandstorms": ORANGE,
}
AGE_COLORS = {"<25": TEAL, "25–40": INDIGO, "40+": MAGENTA}

TRAFFIC_ORDER = ["Low", "Medium", "High", "Jam"]
WEATHER_ORDER = ["Sunny", "Cloudy", "Windy", "Fog", "Stormy", "Sandstorms"]
AREA_ORDER = ["Urban", "Metropolitan", "Semi-Urban", "Other"]
AGE_ORDER = ["<25", "25–40", "40+"]

DATA_PATH = Path(__file__).parent / "amazon_delivery.csv"
FONT = "Archivo, 'Helvetica Neue', Arial, sans-serif"


# ---------------------------------------------------------------------------
# Stage 4 — LOAD + CLEAN
# ---------------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two coordinate arrays."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


@st.cache_data(show_spinner="Loading and cleaning delivery records…")
def load_and_clean(path: Path) -> tuple[pd.DataFrame, dict]:
    raw = pd.read_csv(path)
    log: dict = {"loaded": len(raw), "columns": raw.shape[1]}
    df = raw.copy()

    # 1. Tidy text labels: strip padding, turn literal "NaN" strings into real NaN,
    #    fix spelling and casing so groups merge correctly.
    text_cols = ["Weather", "Traffic", "Vehicle", "Area", "Category"]
    for c in text_cols:
        df[c] = df[c].astype("string").str.strip()
        df.loc[df[c].isin(["NaN", "nan", ""]), c] = pd.NA
    df["Area"] = df["Area"].replace({"Metropolitian": "Metropolitan"})
    df["Vehicle"] = df["Vehicle"].str.title()

    # 2. Duplicates
    log["duplicates"] = int(df.duplicated(subset="Order_ID").sum())
    df = df.drop_duplicates(subset="Order_ID")

    # 3. Rows missing the core filter dimensions cannot be placed in any view → drop.
    core_missing = df[["Weather", "Traffic"]].isna().any(axis=1)
    log["dropped_missing_core"] = int(core_missing.sum())
    df = df[~core_missing].copy()

    # 4. Numeric checks: ratings live on a 1–5 scale; anything above is invalid.
    df["Agent_Rating"] = pd.to_numeric(df["Agent_Rating"], errors="coerce")
    invalid_rating = df["Agent_Rating"] > 5
    log["invalid_ratings"] = int(invalid_rating.sum())
    df.loc[invalid_rating, "Agent_Rating"] = np.nan
    log["ratings_imputed"] = int(df["Agent_Rating"].isna().sum())
    df["Agent_Rating"] = df["Agent_Rating"].fillna(df["Agent_Rating"].median())

    df["Delivery_Time"] = pd.to_numeric(df["Delivery_Time"], errors="coerce")
    bad_time = df["Delivery_Time"].isna() | (df["Delivery_Time"] <= 0)
    log["dropped_bad_time"] = int(bad_time.sum())
    df = df[~bad_time].copy()

    # 5. Coordinates: some stores were recorded with a flipped sign; near-zero
    #    points are placeholders. Fix signs, flag placeholders (kept for charts,
    #    excluded from the map and distance metric).
    for c in ["Store_Latitude", "Store_Longitude", "Drop_Latitude", "Drop_Longitude"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    flipped = (df["Store_Latitude"] < 0) | (df["Store_Longitude"] < 0)
    log["coords_sign_fixed"] = int(flipped.sum())
    df["Store_Latitude"] = df["Store_Latitude"].abs()
    df["Store_Longitude"] = df["Store_Longitude"].abs()
    df["Geo_Valid"] = (df["Store_Latitude"] > 1) & (df["Drop_Latitude"] > 1)
    log["coords_placeholder"] = int((~df["Geo_Valid"]).sum())

    # 6. Dates and times
    df["Order_Date"] = pd.to_datetime(df["Order_Date"], errors="coerce")
    order_t = pd.to_datetime(df["Order_Time"], format="%H:%M:%S", errors="coerce")
    pick_t = pd.to_datetime(df["Pickup_Time"], format="%H:%M:%S", errors="coerce")
    df["Order_Hour"] = order_t.dt.hour
    wait = (pick_t - order_t).dt.total_seconds() / 60
    df["Pickup_Wait_Min"] = wait.where(wait >= 0, wait + 1440)
    df["Weekday"] = df["Order_Date"].dt.day_name()

    # 7. Derived metrics
    df["Distance_km"] = np.where(
        df["Geo_Valid"],
        haversine_km(df["Store_Latitude"], df["Store_Longitude"], df["Drop_Latitude"], df["Drop_Longitude"]),
        np.nan,
    )
    df["Age_Group"] = pd.cut(df["Agent_Age"], bins=[0, 24, 40, 200], labels=AGE_ORDER, right=True).astype(str)

    mean_t, std_t = df["Delivery_Time"].mean(), df["Delivery_Time"].std()
    log["mean"], log["std"] = float(mean_t), float(std_t)
    log["late_threshold"] = float(mean_t + std_t)
    log["final"] = len(df)

    for c, order in [("Traffic", TRAFFIC_ORDER), ("Weather", WEATHER_ORDER)]:
        df[c] = pd.Categorical(df[c], categories=order, ordered=True)
    return df.reset_index(drop=True), log


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------
def inject_css() -> None:
    st.markdown(
        f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap');

html, body, [class*="css"], .stApp, .stMarkdown, button, input, textarea, select {{
  font-family: {FONT};
}}
.stApp {{ background: #0A0A0C; }}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1380px; }}

/* rails like the infographic: dotted line with coloured stops */
.stApp::before, .stApp::after {{
  content: ""; position: fixed; top: 0; bottom: 0; width: 0;
  border-left: 2px dashed rgba(255,255,255,.18); z-index: 0; pointer-events: none;
}}
.stApp::after {{ right: 18px; }}

/* ---------- hero ---------- */
.hero {{
  background: {ORANGE}; border-radius: 26px; padding: 34px 40px 30px; color: {NAVY};
  display: grid; grid-template-columns: 1.35fr 1fr; gap: 28px; align-items: center;
  position: relative; overflow: hidden;
}}
.hero .eyebrow {{ display: flex; justify-content: space-between; grid-column: 1 / -1;
  font: 800 12.5px/1 'JetBrains Mono', monospace; letter-spacing: .08em; }}
.hero h1 {{ font: 900 clamp(38px, 4.6vw, 88px)/.88 {FONT}; letter-spacing: -.035em; margin: 6px 0 0; white-space: nowrap; color: {NAVY}; padding: 0; }}
.hero h2 {{ font: 800 clamp(22px, 3vw, 40px)/1 {FONT}; margin: 12px 0 0; color: {NAVY}; padding: 0; letter-spacing: -.01em; }}
.hero p.lede {{ font-size: 16px; line-height: 1.45; max-width: 520px; margin: 18px 0 0; font-weight: 500; }}
.hero .art {{ background: {YELLOW}; border-radius: 22px; padding: 18px 18px 14px; box-shadow: 0 10px 0 rgba(30,27,75,.14); }}
.hero .art small {{ font: 800 11px/1 'JetBrains Mono', monospace; letter-spacing: .06em; }}
.hero svg .dash {{ stroke-dasharray: 10 12; animation: run 1.6s linear infinite; }}
.hero svg .wheel {{ transform-origin: 300px 150px; animation: spin 3s linear infinite; }}
.hero svg .parcel {{ animation: bob 2.4s ease-in-out infinite; }}
@keyframes run {{ to {{ stroke-dashoffset: -44; }} }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
@keyframes bob {{ 50% {{ transform: translateY(-6px); }} }}
@media (max-width: 900px) {{ .hero {{ grid-template-columns: 1fr; padding: 26px 22px; }} }}

/* ---------- journey strip ---------- */
.journey {{ background: {YELLOW}; border-radius: 22px; padding: 18px 26px 20px; margin-top: 14px; color: {NAVY}; }}
.sec-label {{ font: 800 12px/1 'JetBrains Mono', monospace; letter-spacing: .08em; text-transform: uppercase; }}
.journey .steps {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 12px; }}
.journey .step b {{ font: 900 24px/1 {FONT}; letter-spacing: .01em; display: block; }}
.journey .step span {{ font-size: 13.5px; font-weight: 500; }}
.journey .step {{ position: relative; padding-right: 28px; }}
.journey .step:not(:last-child)::after {{ content: "⟶"; position: absolute; right: 8px; top: 0; font-size: 26px; font-weight: 300; }}
@media (max-width: 800px) {{ .journey .steps {{ grid-template-columns: 1fr 1fr; }} .journey .step::after {{ display:none; }} }}

.board-head {{ display: flex; justify-content: space-between; align-items: baseline; margin: 30px 2px 12px; color: #fff; }}
.board-head .hint {{ color: {YELLOW}; font: 800 12px/1 'JetBrains Mono', monospace; letter-spacing: .06em; }}

/* ---------- KPI tiles ---------- */
.kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }}
.kpi {{ border-radius: 18px; padding: 16px 18px 14px; color: {NAVY}; position: relative; overflow: hidden; }}
.kpi .k {{ font: 800 11.5px/1 'JetBrains Mono', monospace; letter-spacing: .07em; text-transform: uppercase; }}
.kpi .v {{ font: 900 40px/1 {FONT}; margin-top: 12px; letter-spacing: -.02em; }}
.kpi .v small {{ font-size: 17px; font-weight: 800; margin-left: 3px; }}
.kpi .s {{ font-size: 12.5px; margin-top: 7px; font-weight: 500; opacity: .85; }}
.kpi .delta {{ display: inline-block; font: 700 11px/1 'JetBrains Mono', monospace; padding: 4px 7px; border-radius: 99px; background: rgba(30,27,75,.1); margin-left: 6px; vertical-align: 1px; text-transform: none; letter-spacing: 0; }}
@media (max-width: 900px) {{ .kpis {{ grid-template-columns: 1fr 1fr; }} }}

/* ---------- chart cards (st.container key=card-*) ---------- */
[class*="st-key-card-"] {{
  border-radius: 22px; padding: 20px 22px 20px; color: {NAVY};
  box-shadow: 0 8px 0 rgba(255,255,255,.07);
}}
[class*="st-key-card-"] p, [class*="st-key-card-"] span, [class*="st-key-card-"] label,
[class*="st-key-card-"] div[data-testid="stMarkdownContainer"] {{ color: {NAVY}; }}
.st-key-card-lime {{ background: {CARD['lime']}; }}
.st-key-card-butter {{ background: {CARD['butter']}; }}
.st-key-card-lavender {{ background: {CARD['lavender']}; }}
.st-key-card-salmon {{ background: {CARD['salmon']}; }}
.st-key-card-mint {{ background: {CARD['mint']}; }}
.st-key-card-pink {{ background: {CARD['pink']}; }}
.st-key-card-white {{ background: #FFFDF7; }}
.st-key-card-yellow {{ background: {YELLOW}; }}

.ch {{ display: flex; align-items: center; gap: 11px; }}
.ch .num {{ width: 32px; height: 32px; border-radius: 50%; display: grid; place-items: center;
  color: #fff; font: 800 13px/1 'JetBrains Mono', monospace; flex: none; }}
.ch .t {{ font: 800 21px/1.1 {FONT}; color: {NAVY}; letter-spacing: -.01em; }}
.ch .tag {{ margin-left: auto; font: 700 10.5px/1 'JetBrains Mono', monospace; padding: 5px 8px; border-radius: 99px;
  background: rgba(30,27,75,.08); color: {NAVY}; letter-spacing: .05em; white-space: nowrap; }}
.q {{ margin: 6px 0 2px 43px; font-size: 14.5px; color: {NAVY}; opacity: .8; font-weight: 500; }}
.foot {{ font-size: 13.5px; color: {NAVY}; line-height: 1.45; margin-top: 2px; }}
.foot b {{ font-weight: 800; }}
.src {{ font: 600 11px/1.3 'JetBrains Mono', monospace; color: {NAVY}; opacity: .6; margin-top: 8px; letter-spacing: .02em; }}

/* tabs inside cards */
[class*="st-key-card-"] [data-baseweb="tab-list"] {{ gap: 18px; }}
[class*="st-key-card-"] button[role="tab"] {{ height: 34px; background: transparent; }}
[class*="st-key-card-"] [data-baseweb="tab"] p {{ font: 700 12.5px/1 {FONT}; color: {NAVY}; }}
[class*="st-key-card-"] button[role="tab"] p {{ opacity: .6; }}
[class*="st-key-card-"] button[role="tab"][aria-selected="true"] p {{ opacity: 1; font-weight: 900 !important; }}
[class*="st-key-card-"] [data-baseweb="tab-highlight"] {{ background: {NAVY}; height: 3px; border-radius: 3px; }}
[class*="st-key-card-"] [data-baseweb="tab-border"] {{ background: rgba(30,27,75,.12); }}

/* ---------- insight + pipeline ---------- */
.act {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 10px; }}
.act .it {{ background: #fff; border-radius: 16px; padding: 14px 16px; color: {NAVY}; border: 2px solid {NAVY}; }}
.act .it .h {{ font: 800 11px/1 'JetBrains Mono', monospace; letter-spacing: .06em; text-transform: uppercase; opacity: .7; }}
.act .it .b {{ font: 800 19px/1.15 {FONT}; margin-top: 8px; }}
.act .it .d {{ font-size: 13px; margin-top: 6px; line-height: 1.4; }}
@media (max-width: 900px) {{ .act {{ grid-template-columns: 1fr; }} }}

.pipe {{ background: #FFC9DE; border-radius: 26px; padding: 22px 26px 22px; color: {NAVY}; margin-top: 28px; }}
.pipe .row {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; margin-top: 14px; }}
.pipe .stage {{ border-radius: 14px; padding: 14px 15px; min-height: 140px; position: relative; }}
.pipe .stage:not(:last-child)::after {{ content: "›"; position: absolute; right: -12px; top: 42%; font: 900 20px/1 {FONT}; }}
.pipe .stage b {{ font: 900 15px/1 {FONT}; letter-spacing: .03em; display: block; margin-bottom: 9px; }}
.pipe .stage ul {{ margin: 0; padding-left: 16px; font-size: 12.5px; line-height: 1.45; }}
.pipe .stage .big {{ font: 900 26px/1 {FONT}; margin-bottom: 6px; }}
.pipe .note {{ margin-top: 16px; font-size: 13px; border-top: 1.5px solid rgba(30,27,75,.25); padding-top: 12px; }}
@media (max-width: 1000px) {{ .pipe .row {{ grid-template-columns: 1fr 1fr; }} .pipe .stage::after {{ display: none; }} }}

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] .side-title {{ color: {YELLOW}; font: 900 15px/1 {FONT}; letter-spacing: .08em; margin: 4px 0 4px; }}
[data-testid="stSidebar"] .side-note {{ font-size: 12.5px; color: rgba(255,255,255,.72); line-height: 1.45; }}
[data-testid="stSidebar"] .side-note b {{ color: #fff; }}
[data-testid="stSidebar"] label p {{ font-weight: 800 !important; font-size: 14px !important; }}
[data-testid="stSidebar"] .stButton button {{ background: {YELLOW}; color: {NAVY}; border: none; font-weight: 800; border-radius: 10px; width: 100%; }}
[data-testid="stSidebar"] .stButton button:hover {{ background: #fff; color: {NAVY}; }}
[data-testid="stSidebar"] [data-baseweb="tag"] {{ background: {ORANGE} !important; }}
.planned {{ font: 800 12px/1.35 'JetBrains Mono', monospace; color: #fff; margin-top: 18px; letter-spacing: .02em; }}

.empty {{ background: {YELLOW}; color: {NAVY}; border-radius: 18px; padding: 22px 24px; font-weight: 600; }}
.empty b {{ font: 900 22px/1.1 {FONT}; display: block; margin-bottom: 6px; }}
.footer {{ color: rgba(255,255,255,.7); font: 600 11.5px/1.5 'JetBrains Mono', monospace; margin-top: 18px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }}
[data-testid="stExpander"] {{ background: #FFFDF7; border-radius: 16px; border: none; }}
[data-testid="stExpander"] summary p, [data-testid="stExpander"] p {{ color: {NAVY}; font-weight: 700; }}
</style>
""",
        unsafe_allow_html=True,
    )


def style_fig(fig: go.Figure, height: int = 360, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=64, r=16, t=40, b=56),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=NAVY, size=13),
        hoverlabel=dict(bgcolor=NAVY, font=dict(color="#fff", family=FONT), bordercolor=NAVY),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, title_text="", bgcolor="rgba(0,0,0,0)") if legend else None,
        showlegend=legend,
    )
    fig.update_xaxes(automargin=True, gridcolor="rgba(30,27,75,.10)", zerolinecolor="rgba(30,27,75,.2)", linecolor="rgba(30,27,75,.35)", title_font=dict(size=12.5), tickfont=dict(size=12))
    fig.update_yaxes(automargin=True, gridcolor="rgba(30,27,75,.10)", zerolinecolor="rgba(30,27,75,.2)", linecolor="rgba(30,27,75,.35)", title_font=dict(size=12.5), tickfont=dict(size=12))
    return fig


PLOT_CFG = {"displayModeBar": False, "responsive": True}
PLOT_CFG_3D = {"displayModeBar": True, "displaylogo": False, "responsive": True,
               "modeBarButtonsToRemove": ["toImage", "resetCameraLastSave3d"]}


def show(fig, three_d: bool = False):
    st.plotly_chart(fig, width="stretch", theme=None, config=PLOT_CFG_3D if three_d else PLOT_CFG)


def card_header(num: str, title: str, question: str, color: str, tag: str = "") -> None:
    tag_html = f'<span class="tag">{tag}</span>' if tag else ""
    st.markdown(
        f'<div class="ch"><span class="num" style="background:{color}">{num}</span>'
        f'<span class="t">{title}</span>{tag_html}</div><div class="q">{question}</div>',
        unsafe_allow_html=True,
    )


def card_footer(text: str, source: str) -> None:
    st.markdown(f'<div class="foot">{text}</div><div class="src">{source}</div>', unsafe_allow_html=True)


def fmt_min(x: float) -> str:
    return f"{x:,.0f} min" if pd.notna(x) else "–"


# ---------------------------------------------------------------------------
# Sidebar — REFINE THE VIEW
# ---------------------------------------------------------------------------
FILTER_KEYS = ["f_weather", "f_traffic", "f_vehicle", "f_area", "f_category"]


def reset_filters() -> None:
    for k in FILTER_KEYS:
        st.session_state[k] = []
    st.session_state["late_mode"] = "Mean + 1 SD (brief rule)"


def sidebar(df: pd.DataFrame, log: dict) -> dict:
    sb = st.sidebar
    sb.markdown('<div class="side-title">REFINE THE VIEW</div>', unsafe_allow_html=True)
    sb.markdown('<div class="side-note">Leave a filter empty to include <b>All</b>.</div>', unsafe_allow_html=True)

    def opts(col, order=None):
        present = set(df[col].dropna().astype(str).unique())
        if order:
            return [o for o in order if o in present] + sorted(present - set(order))
        return sorted(present)

    sel = {
        "Weather": sb.multiselect("Weather", opts("Weather", WEATHER_ORDER), key="f_weather", placeholder="All"),
        "Traffic": sb.multiselect("Traffic", opts("Traffic", TRAFFIC_ORDER), key="f_traffic", placeholder="All"),
        "Vehicle": sb.multiselect("Vehicle", opts("Vehicle", ["Motorcycle", "Scooter", "Van", "Bicycle"]), key="f_vehicle", placeholder="All"),
        "Area": sb.multiselect("Area", opts("Area", AREA_ORDER), key="f_area", placeholder="All"),
        "Category": sb.multiselect("Category", opts("Category"), key="f_category", placeholder="All"),
    }

    sb.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    mode = sb.radio("Late delivery rule", ["Mean + 1 SD (brief rule)", "Custom target"], key="late_mode")
    if mode == "Custom target":
        threshold = sb.slider("Target (minutes)", 30, 240, int(round(log["late_threshold"] / 5) * 5), 5, key="late_target")
    else:
        threshold = log["late_threshold"]
        sb.markdown(
            f'<div class="side-note">Late = Delivery_Time &gt; mean ({log["mean"]:.0f}) + 1 SD ({log["std"]:.0f}) '
            f'= <b>{threshold:.0f} min</b>, fixed on the full cleaned dataset so the target never moves with filters.</div>',
            unsafe_allow_html=True,
        )

    sb.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    sb.button("Reset filters", on_click=reset_filters, width="stretch")
    sb.markdown(
        '<div class="planned">One selection.<br>Every view updates.</div>'
        '<div class="side-note" style="margin-top:10px">Compare like-for-like conditions before making decisions.</div>'
        '<div class="planned" style="margin-top:26px;color:#FFE15A">PLANNED IN FA-1<br>'
        '<span style="color:#fff">BUILT WITH PYTHON + STREAMLIT IN FA-2.</span></div>',
        unsafe_allow_html=True,
    )
    return {"selections": sel, "threshold": float(threshold), "mode": mode}


def apply_filters(df: pd.DataFrame, selections: dict) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for col, chosen in selections.items():
        if chosen:
            mask &= df[col].astype(str).isin(chosen)
    return df[mask]


# ---------------------------------------------------------------------------
# Static blocks
# ---------------------------------------------------------------------------
HERO_SVG = f"""
<svg viewBox="0 0 380 210" width="100%" role="img" aria-label="Route from store to door">
  <path d="M40 170 V70 Q40 40 70 40 H270 Q300 40 300 70 V150" fill="none" stroke="{NAVY}" stroke-width="9" stroke-linecap="round"/>
  <path class="dash" d="M40 170 V70 Q40 40 70 40 H270 Q300 40 300 70 V150" fill="none" stroke="{YELLOW}" stroke-width="3" stroke-linecap="round"/>
  <circle cx="40" cy="172" r="16" fill="{NAVY}"/><circle cx="40" cy="172" r="6" fill="{YELLOW}"/>
  <g class="parcel"><rect x="132" y="92" width="92" height="56" rx="6" fill="{ORANGE}"/>
    <path d="M178 92 V148 M160 116 H196" stroke="{NAVY}" stroke-width="3"/></g>
  <g class="wheel"><circle cx="300" cy="150" r="26" fill="{MAGENTA}"/><circle cx="300" cy="150" r="9" fill="{YELLOW}"/>
    <rect x="297" y="126" width="6" height="10" fill="{YELLOW}"/></g>
</svg>"""


def hero(log: dict) -> None:
    st.markdown(
        f"""
<div class="hero">
  <div class="eyebrow"><span>LOGISIGHT ANALYTICS</span><span>FA-2 / MATHEMATICS FOR AI-II</span></div>
  <div>
    <h1>LAST&#8209;MILE<br>DELIVERY</h1>
    <h2>DASHBOARD</h2>
    <p class="lede">{log['final']:,} real orders, six weather states, four traffic levels.
    Find where — and why — the last mile slows down.</p>
  </div>
  <div class="art">{HERO_SVG}<small>DELIVERY OPERATIONS / LIVE BUILD</small></div>
</div>
<div class="journey">
  <div class="sec-label">01 / The manager's journey</div>
  <div class="steps">
    <div class="step"><b>FILTER</b><span>Choose a delivery context</span></div>
    <div class="step"><b>COMPARE</b><span>Read five connected views</span></div>
    <div class="step"><b>INVESTIGATE</b><span>Review groups and outliers</span></div>
    <div class="step"><b>ACT</b><span>Plan the next intervention</span></div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def kpi_row(f: pd.DataFrame, full: pd.DataFrame, threshold: float) -> None:
    n = len(f)
    mean_f = f["Delivery_Time"].mean()
    late_f = (f["Delivery_Time"] > threshold).mean() * 100
    late_all = (full["Delivery_Time"] > threshold).mean() * 100
    d_mean = mean_f - full["Delivery_Time"].mean()
    d_late = late_f - late_all
    med = f["Delivery_Time"].median()
    dist = f["Distance_km"].median()

    def delta(v, unit):
        if len(f) == len(full):
            return ""
        sign = "+" if v > 0 else ""
        return f'<span class="delta">{sign}{v:.1f}{unit} vs all</span>'

    st.markdown(
        f"""
<div class="kpis">
  <div class="kpi" style="background:{YELLOW}"><div class="k">Delivery records</div>
    <div class="v">{n:,}</div><div class="s">{n / len(full) * 100:.1f}% of {len(full):,} cleaned orders</div></div>
  <div class="kpi" style="background:#B9F0C9"><div class="k">Mean delivery time {delta(d_mean, ' min')}</div>
    <div class="v">{mean_f:.1f}<small>min</small></div><div class="s">Median {med:.0f} min · minutes from order to drop</div></div>
  <div class="kpi" style="background:#FFC1DC"><div class="k">Late delivery rate {delta(d_late, ' pts')}</div>
    <div class="v">{late_f:.1f}<small>%</small></div><div class="s">Share of orders over {threshold:.0f} min</div></div>
  <div class="kpi" style="background:#FFFDF7"><div class="k">Median route length</div>
    <div class="v">{dist:.1f}<small>km</small></div><div class="s">Store → drop, straight-line (haversine)</div></div>
</div>""",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Stage 5 — the five compulsory views (+ 3D variants)
# ---------------------------------------------------------------------------
def delay_analyzer(f: pd.DataFrame) -> None:
    with st.container(key="card-lime"):
        card_header("01", "Delay analyzer", "When do deliveries take longer?", "#1E7A3E", "WEATHER × TRAFFIC")
        g = (f.groupby(["Weather", "Traffic"], observed=True)["Delivery_Time"]
               .agg(mean="mean", n="size").reset_index())
        t1, t2, t3 = st.tabs(["Bar chart", "Heat grid", "3D surface"])
        with t1:
            fig = px.bar(
                g, x="Traffic", y="mean", color="Weather", barmode="group",
                category_orders={"Traffic": TRAFFIC_ORDER, "Weather": WEATHER_ORDER},
                color_discrete_map=WEATHER_COLORS, custom_data=["Weather", "n"],
                labels={"mean": "Avg delivery time (min)", "Traffic": "Traffic intensity →"},
            )
            fig.update_traces(marker_line_width=0,
                              hovertemplate="<b>%{customdata[0]}</b> · %{x} traffic<br>%{y:.1f} min avg<br>n = %{customdata[1]:,}<extra></extra>")
            fig.update_layout(bargap=0.18, bargroupgap=0.06)
            show(style_fig(fig, 380))
        piv = g.pivot(index="Weather", columns="Traffic", values="mean")
        cnt = g.pivot(index="Weather", columns="Traffic", values="n")
        with t2:
            text = [[f"{v:.0f}" if pd.notna(v) else "" for v in row] for row in piv.values]
            fig = go.Figure(go.Heatmap(
                z=piv.values, x=[str(c) for c in piv.columns], y=[str(i) for i in piv.index],
                colorscale=GREEN_RAMP, text=text, texttemplate="%{text}", textfont=dict(size=14, family=FONT),
                customdata=cnt.values, xgap=5, ygap=5,
                hovertemplate="%{y} · %{x}<br>%{z:.1f} min avg<br>n = %{customdata:,}<extra></extra>",
                colorbar=dict(title="min", thickness=10, outlinewidth=0),
            ))
            fig.update_yaxes(autorange="reversed", showgrid=False)
            fig.update_xaxes(showgrid=False, side="top", title="Traffic intensity →")
            show(style_fig(fig, 380, legend=False))
        with t3:
            if piv.shape[0] >= 2 and piv.shape[1] >= 2:
                fig = go.Figure(go.Surface(
                    z=piv.values, x=list(range(piv.shape[1])), y=list(range(piv.shape[0])),
                    colorscale=GREEN_RAMP, showscale=False, opacity=0.97,
                    contours=dict(z=dict(show=True, usecolormap=True, project_z=True, width=2)),
                    hovertemplate="%{z:.1f} min<extra></extra>",
                ))
                fig.update_layout(scene=dict(
                    xaxis=dict(title="Traffic", tickvals=list(range(piv.shape[1])), ticktext=[str(c) for c in piv.columns], backgroundcolor="rgba(0,0,0,0)"),
                    yaxis=dict(title="", tickvals=list(range(piv.shape[0])), ticktext=[str(i) for i in piv.index], backgroundcolor="rgba(0,0,0,0)"),
                    zaxis=dict(title="Avg min", backgroundcolor="rgba(0,0,0,0)"),
                    camera=dict(eye=dict(x=1.6, y=-1.55, z=0.9)), aspectratio=dict(x=1.2, y=1.2, z=0.7),
                ))
                show(style_fig(fig, 430, legend=False), three_d=True)
                st.caption("Drag to rotate · scroll to zoom. Valleys are the calm conditions; ridges are where dispatch buffers belong.")
            else:
                st.info("Select at least two weather states and two traffic levels to build the surface.")
        if len(g):
            worst = g.sort_values("mean").iloc[-1]
            best = g.sort_values("mean").iloc[0]
            card_footer(
                f"Slowest: <b>{worst.Weather} + {worst.Traffic}</b> at {worst['mean']:.0f} min · "
                f"fastest: <b>{best.Weather} + {best.Traffic}</b> at {best['mean']:.0f} min. "
                "Use the gap to size dispatch buffers.",
                "TRAFFIC + WEATHER + DELIVERY_TIME",
            )


def vehicle_comparison(f: pd.DataFrame) -> None:
    with st.container(key="card-butter"):
        card_header("02", "Vehicle comparison", "Which vehicles deliver faster?", "#B5580F", "FLEET")
        g = (f.groupby("Vehicle")["Delivery_Time"].agg(mean="mean", median="median", std="std", n="size")
               .reset_index().sort_values("mean"))
        t1, t2 = st.tabs(["Average time", "Spread"])
        with t1:
            colors = [TEAL if i == 0 else ("#E8A33A" if i < len(g) - 1 else RED) for i in range(len(g))]
            fig = go.Figure(go.Bar(
                x=g["mean"], y=g["Vehicle"], orientation="h", marker_color=colors,
                error_x=dict(type="data", array=g["std"].fillna(0), color="rgba(30,27,75,.45)", thickness=1.5, width=6),
                text=[f"{m:.0f} min · n={n:,}" for m, n in zip(g["mean"], g["n"])],
                textposition="inside", insidetextanchor="start", textfont=dict(color="#fff", size=12.5, family=FONT),
                customdata=g[["median", "std", "n"]].values,
                hovertemplate="<b>%{y}</b><br>mean %{x:.1f} min<br>median %{customdata[0]:.0f} · SD %{customdata[1]:.1f}<br>n = %{customdata[2]:,}<extra></extra>",
            ))
            fig.update_xaxes(title="Avg delivery time (min) · whiskers = ±1 SD")
            fig.update_yaxes(title="", showgrid=False)
            show(style_fig(fig, 330, legend=False))
        with t2:
            order = g["Vehicle"].tolist()
            fig = px.box(f, x="Delivery_Time", y="Vehicle", category_orders={"Vehicle": order},
                         color_discrete_sequence=[TEAL], points=False,
                         labels={"Delivery_Time": "Delivery time (min)", "Vehicle": ""})
            fig.update_traces(fillcolor="rgba(15,143,130,.75)", line=dict(color=NAVY, width=1.6))
            show(style_fig(fig, 330, legend=False))
        if len(g):
            card_footer(
                f"<b>{g.iloc[0].Vehicle}</b> is fastest on average ({g.iloc[0]['mean']:.0f} min); "
                f"<b>{g.iloc[-1].Vehicle}</b> slowest ({g.iloc[-1]['mean']:.0f} min). "
                "Median + spread show consistency — compare under similar conditions before reallocating fleet.",
                "VEHICLE + DELIVERY_TIME",
            )


def agent_insights(f: pd.DataFrame) -> None:
    with st.container(key="card-lavender"):
        card_header("03", "Agent insights", "How do ratings and age relate to time?", INDIGO, "PEOPLE")
        sample = f.sample(min(len(f), 4500), random_state=7)
        t1, t2, t3 = st.tabs(["Rating × time", "Age × time", "3D cloud"])
        with t1:
            jitter = sample.assign(Rating_j=sample["Agent_Rating"] + np.random.default_rng(1).uniform(-0.04, 0.04, len(sample)))
            fig = px.scatter(
                jitter, x="Rating_j", y="Delivery_Time", color="Age_Group",
                category_orders={"Age_Group": AGE_ORDER}, color_discrete_map=AGE_COLORS, opacity=0.5,
                labels={"Rating_j": "Agent rating", "Delivery_Time": "Delivery time (min)", "Age_Group": "Agent age"},
                custom_data=["Agent_Rating", "Agent_Age", "Vehicle"],
            )
            fig.update_traces(marker=dict(size=6, line=dict(width=0)),
                              hovertemplate="Rating %{customdata[0]} · age %{customdata[1]}<br>%{y} min · %{customdata[2]}<extra></extra>")
            trend = f.assign(rb=f["Agent_Rating"].round(1)).groupby("rb")["Delivery_Time"].agg(["mean", "size"]).reset_index()
            trend = trend[trend["size"] >= 20]
            fig.add_trace(go.Scatter(x=trend["rb"], y=trend["mean"], mode="lines+markers", name="Mean per rating",
                                     line=dict(color=NAVY, width=3), marker=dict(size=7, color=YELLOW, line=dict(color=NAVY, width=2)),
                                     hovertemplate="Rating %{x}: %{y:.1f} min avg<extra></extra>"))
            show(style_fig(fig, 390))
        with t2:
            ag = f.groupby(["Agent_Age", "Age_Group"])["Delivery_Time"].agg(mean="mean", n="size").reset_index()
            fig = px.scatter(ag, x="Agent_Age", y="mean", size="n", color="Age_Group", size_max=34,
                             category_orders={"Age_Group": AGE_ORDER}, color_discrete_map=AGE_COLORS,
                             labels={"Agent_Age": "Agent age", "mean": "Avg delivery time (min)", "Age_Group": "Agent age"},
                             custom_data=["n"])
            fig.update_traces(marker=dict(line=dict(color=NAVY, width=1)),
                              hovertemplate="Age %{x}<br>%{y:.1f} min avg<br>n = %{customdata[0]:,}<extra></extra>")
            show(style_fig(fig, 390))
        with t3:
            s3 = sample.sample(min(len(sample), 2500), random_state=3)
            fig = px.scatter_3d(s3, x="Agent_Rating", y="Agent_Age", z="Delivery_Time", color="Age_Group",
                                category_orders={"Age_Group": AGE_ORDER}, color_discrete_map=AGE_COLORS, opacity=0.75,
                                labels={"Agent_Rating": "Rating", "Agent_Age": "Age", "Delivery_Time": "Minutes", "Age_Group": "Agent age"})
            fig.update_traces(marker=dict(size=3.2, line=dict(width=0)))
            fig.update_layout(scene=dict(camera=dict(eye=dict(x=1.7, y=1.4, z=0.75)),
                                         xaxis=dict(backgroundcolor="rgba(0,0,0,0)"), yaxis=dict(backgroundcolor="rgba(0,0,0,0)"),
                                         zaxis=dict(backgroundcolor="rgba(0,0,0,0)")))
            show(style_fig(fig, 430), three_d=True)
        corr = f[["Agent_Rating", "Delivery_Time"]].corr().iloc[0, 1] if len(f) > 2 else np.nan
        card_footer(
            f"Rating vs time correlation: <b>r = {corr:.2f}</b>. Spot outliers to guide review and training. "
            f"Points sampled ({len(sample):,} of {len(f):,}) for speed; trend line uses all rows. "
            "No agents over 40 remain after cleaning. <i>Association is not causation; age is not tenure.</i>",
            "AGENT_RATING / AGENT_AGE + DELIVERY_TIME",
        )


def regional_bottlenecks(f: pd.DataFrame) -> None:
    with st.container(key="card-salmon"):
        card_header("04", "Regional bottlenecks", "Which areas need closer attention?", RED, "AREA")
        t1, t2, t3 = st.tabs(["Area heatmap", "Ranking", "3D map"])
        with t1:
            g = f.groupby(["Area", "Traffic"], observed=True)["Delivery_Time"].agg(mean="mean", n="size").reset_index()
            order = f.groupby("Area")["Delivery_Time"].mean().sort_values(ascending=False).index.tolist()
            piv = g.pivot(index="Area", columns="Traffic", values="mean").reindex(order)
            cnt = g.pivot(index="Area", columns="Traffic", values="n").reindex(order)
            overall = f.groupby("Area")["Delivery_Time"].mean().reindex(order)
            piv["All traffic"] = overall
            cnt["All traffic"] = f.groupby("Area").size().reindex(order)
            text = [[f"{v:.0f}" if pd.notna(v) else "" for v in row] for row in piv.values]
            fig = go.Figure(go.Heatmap(
                z=piv.values, x=[str(c) for c in piv.columns], y=piv.index.tolist(), colorscale=SALMON_RAMP,
                text=text, texttemplate="%{text}", textfont=dict(size=14, family=FONT), customdata=cnt.values,
                xgap=5, ygap=5, colorbar=dict(title="min", thickness=10, outlinewidth=0),
                hovertemplate="%{y} · %{x}<br>%{z:.1f} min avg<br>n = %{customdata:,}<extra></extra>",
            ))
            fig.update_xaxes(side="top", showgrid=False)
            fig.update_yaxes(autorange="reversed", showgrid=False)
            show(style_fig(fig, 330, legend=False))
        with t2:
            r = f.groupby("Area")["Delivery_Time"].agg(mean="mean", n="size").reset_index().sort_values("mean")
            shades = ["#F7A487", "#F48A68", "#EF6A4A", RED][-len(r):] if len(r) <= 4 else None
            fig = go.Figure(go.Bar(
                x=r["mean"], y=r["Area"], orientation="h", marker_color=shades or RED,
                text=[f"{m:.0f} min · n={n:,}" for m, n in zip(r["mean"], r["n"])], textposition="inside",
                insidetextanchor="start", textfont=dict(color="#fff", size=12.5),
                hovertemplate="%{y}: %{x:.1f} min<extra></extra>",
            ))
            fig.update_xaxes(title="Avg delivery time (min)")
            fig.update_yaxes(showgrid=False, title="")
            show(style_fig(fig, 330, legend=False))
        with t3:
            geo = f[f["Geo_Valid"]][["Drop_Latitude", "Drop_Longitude", "Delivery_Time"]]
            # Pre-aggregate drops into ~0.15° cells (≈15 km) so every column is an exact group mean.
            cells = (geo.assign(lat=(geo["Drop_Latitude"] / 0.15).round() * 0.15,
                                lon=(geo["Drop_Longitude"] / 0.15).round() * 0.15)
                        .groupby(["lat", "lon"])["Delivery_Time"].agg(mean="mean", n="size").reset_index())
            cells = cells[cells["n"] >= 5]
            if len(cells):
                lo, hi = cells["mean"].min(), cells["mean"].max()
                ramp = np.array([[255, 225, 90], [255, 154, 115], [240, 97, 63], [184, 48, 30], [110, 20, 60]])
                pos = ((cells["mean"] - lo) / max(hi - lo, 1e-9) * (len(ramp) - 1)).to_numpy()
                i0 = np.floor(pos).astype(int).clip(0, len(ramp) - 2)
                frac = (pos - i0)[:, None]
                rgb = (ramp[i0] * (1 - frac) + ramp[i0 + 1] * frac).round().astype(int)
                cells["color"] = [list(map(int, c)) + [235] for c in rgb]
                cells["elev"] = (cells["mean"] - lo + 10) * 900
                cells["mean_txt"] = cells["mean"].round(0).astype(int)
                layer = pdk.Layer(
                    "ColumnLayer", data=cells[["lat", "lon", "elev", "color", "mean_txt", "n"]],
                    get_position=["lon", "lat"], get_elevation="elev", elevation_scale=1, radius=7000,
                    get_fill_color="color", extruded=True, pickable=True, auto_highlight=True, disk_resolution=6,
                )
                view = pdk.ViewState(latitude=20.5, longitude=78.5, zoom=3.7, pitch=55, bearing=-15)
                st.pydeck_chart(pdk.Deck(
                    layers=[layer], initial_view_state=view, map_provider="carto", map_style=pdk.map_styles.CARTO_LIGHT,
                    tooltip={"html": "<b>{mean_txt} min</b> avg delivery<br>{n} orders in this cell",
                             "style": {"backgroundColor": NAVY, "color": "white", "fontFamily": "Archivo, sans-serif"}},
                ), height=440)
                st.caption(f"{len(cells):,} delivery cells (≥5 orders each) · column height & colour = mean delivery time "
                           f"({lo:.0f}–{hi:.0f} min) · ctrl/right-drag to tilt.")
            else:
                st.info("No valid coordinates in this selection.")
        r_all = f.groupby("Area")["Delivery_Time"].mean().sort_values()
        if len(r_all):
            card_footer(
                f"<b>{r_all.index[-1]}</b> is the slowest area ({r_all.iloc[-1]:.0f} min) vs <b>{r_all.index[0]}</b> "
                f"({r_all.iloc[0]:.0f} min). Rank mean time, show group counts, prioritise slower areas for investigation.",
                "AREA + DELIVERY_TIME",
            )


def category_explorer(f: pd.DataFrame, threshold: float) -> None:
    with st.container(key="card-mint"):
        card_header("05", "Category explorer", "Do some product categories take longer?", TEAL, "PRODUCT")
        stats = (f.groupby("Category")["Delivery_Time"]
                   .agg(median="median", mean="mean", n="size", late=lambda s: (s > threshold).mean() * 100)
                   .sort_values("median"))
        order = stats.index.tolist()
        fig = go.Figure()
        for i, cat in enumerate(order):
            vals = f.loc[f["Category"] == cat, "Delivery_Time"]
            col = TEAL if i % 2 == 0 else PURPLE
            fig.add_trace(go.Box(y=vals, name=cat, marker_color=col, line=dict(color=NAVY, width=1.4),
                                 fillcolor=col, boxpoints=False, showlegend=False,
                                 hovertemplate=f"<b>{cat}</b><br>%{{y}} min<extra></extra>"))
        fig.add_trace(go.Scatter(x=order, y=stats["mean"], mode="markers", name="Mean",
                                 marker=dict(symbol="diamond", size=9, color=YELLOW, line=dict(color=NAVY, width=1.5)),
                                 customdata=stats[["n", "late"]].values,
                                 hovertemplate="%{x}: mean %{y:.1f} min<br>n = %{customdata[0]:,} · late %{customdata[1]:.1f}%<extra></extra>"))
        fig.add_hline(y=threshold, line=dict(color=RED, width=1.5, dash="dot"),
                      annotation_text=f"late target {threshold:.0f} min", annotation_position="top left",
                      annotation_font=dict(color=RED, size=11))
        fig.update_yaxes(title="Delivery time (min)")
        fig.update_xaxes(title="", tickangle=-30)
        show(style_fig(fig, 410))
        if len(stats):
            card_footer(
                f"Typical time ranges from <b>{stats['median'].iloc[0]:.0f} min ({order[0]})</b> to "
                f"<b>{stats['median'].iloc[-1]:.0f} min ({order[-1]})</b>. Boxes sorted by median; ◆ = mean. "
                "Review handling needs and delivery planning for the slow end.",
                "CATEGORY + DELIVERY_TIME",
            )


# ---------------------------------------------------------------------------
# Optional views
# ---------------------------------------------------------------------------
def trends(f: pd.DataFrame, threshold: float) -> None:
    with st.container(key="card-white"):
        card_header("06", "Time trends", "Is performance drifting over days or hours?", NAVY, "OPTIONAL")
        t1, t2 = st.tabs(["By day", "By hour of order"])
        with t1:
            d = (f.dropna(subset=["Order_Date"]).groupby("Order_Date")["Delivery_Time"]
                   .agg(mean="mean", n="size").reset_index())
            d = d[d["n"] >= 30]
            if len(d):
                full_range = pd.date_range(d["Order_Date"].min(), d["Order_Date"].max(), freq="D")
                d = d.set_index("Order_Date").reindex(full_range).rename_axis("Order_Date").reset_index()
                d["parity"] = np.where(d["Order_Date"].dt.day % 2 == 1, "Odd date", "Even date")
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=d["Order_Date"], y=d["mean"], mode="lines", line=dict(color="rgba(30,27,75,.35)", width=1.5),
                                     showlegend=False, hoverinfo="skip", connectgaps=False))
            for name, col in [("Odd date", ORANGE), ("Even date", INDIGO)]:
                dd = d[d["parity"] == name] if len(d) else d
                fig.add_trace(go.Scatter(x=dd["Order_Date"], y=dd["mean"], mode="markers", name=name,
                                         marker=dict(size=8, color=col, line=dict(color=NAVY, width=1)),
                                         customdata=dd["n"], hovertemplate="%{x|%d %b}: %{y:.1f} min · n=%{customdata:,}<extra></extra>"))
            fig.update_yaxes(title="Avg delivery time (min)")
            show(style_fig(fig, 320))
        with t2:
            h = f.dropna(subset=["Order_Hour"]).groupby("Order_Hour")["Delivery_Time"].agg(mean="mean", n="size").reset_index()
            fig = go.Figure(go.Bar(x=h["Order_Hour"], y=h["mean"], marker=dict(color=h["mean"], colorscale=[[0, "#C9C0F5"], [1, INDIGO]]),
                                   customdata=h["n"], hovertemplate="%{x}:00 · %{y:.1f} min · n=%{customdata:,}<extra></extra>"))
            fig.update_xaxes(title="Hour order placed", dtick=2)
            fig.update_yaxes(title="Avg delivery time (min)")
            show(style_fig(fig, 320, legend=False))
        card_footer("Odd and even dates alternate by ~30 min — too regular to be operational. <b>Flag to the data owner</b> before reading trends. Days under 30 orders hidden.",
                    "ORDER_DATE / ORDER_TIME + DELIVERY_TIME")


def distribution(f: pd.DataFrame, threshold: float) -> None:
    with st.container(key="card-pink"):
        card_header("07", "Time distribution", "How are delivery times spread?", MAGENTA, "OPTIONAL")
        fig = go.Figure()
        on_time = f.loc[f["Delivery_Time"] <= threshold, "Delivery_Time"]
        late = f.loc[f["Delivery_Time"] > threshold, "Delivery_Time"]
        bins = dict(start=0, end=280, size=10)
        fig.add_trace(go.Histogram(x=on_time, xbins=bins, name="On time", marker_color=INDIGO, hovertemplate="%{x} min: %{y:,}<extra></extra>"))
        fig.add_trace(go.Histogram(x=late, xbins=bins, name="Late", marker_color=MAGENTA, hovertemplate="%{x} min: %{y:,}<extra></extra>"))
        fig.add_vline(x=threshold, line=dict(color=NAVY, width=2, dash="dash"),
                      annotation_text=f"{threshold:.0f} min", annotation_position="top right")
        fig.update_layout(barmode="stack", bargap=0.06)
        fig.update_xaxes(title="Delivery time (min)")
        fig.update_yaxes(title="Orders")
        show(style_fig(fig, 320))
        card_footer(f"<b>{len(late):,}</b> of {len(f):,} orders sit right of the target line.", "DELIVERY_TIME")


def late_by_condition(f: pd.DataFrame, threshold: float) -> None:
    with st.container(key="card-yellow"):
        card_header("08", "Late rate by condition", "Where does the late share spike?", NAVY, "OPTIONAL")
        g = (f.assign(late=f["Delivery_Time"] > threshold)
               .groupby(["Traffic", "Weather"], observed=True)["late"].mean().mul(100).reset_index())
        fig = px.bar(g, x="Weather", y="late", color="Traffic", barmode="group",
                     category_orders={"Traffic": TRAFFIC_ORDER, "Weather": WEATHER_ORDER},
                     color_discrete_map={"Low": "#9FD9C9", "Medium": "#4FB3A2", "High": TEAL, "Jam": NAVY},
                     labels={"late": "% late", "Weather": ""})
        fig.update_traces(hovertemplate="%{x} · %{fullData.name}: %{y:.1f}% late<extra></extra>", marker_line_width=0)
        show(style_fig(fig, 320))
        card_footer("Late share = orders above the target ÷ all orders in the group.", "TRAFFIC + WEATHER + DELIVERY_TIME")


# ---------------------------------------------------------------------------
# ACT + pipeline
# ---------------------------------------------------------------------------
def act_panel(f: pd.DataFrame, threshold: float) -> None:
    def slowest(col):
        s = f.groupby(col, observed=True)["Delivery_Time"].agg(["mean", "size"])
        s = s[s["size"] >= 30]
        return (s["mean"].idxmax(), s["mean"].max(), s["mean"].idxmin(), s["mean"].min()) if len(s) else None

    items = []
    for col, label, verb in [("Traffic", "Traffic", "Add buffer for"), ("Weather", "Weather", "Pre-alert customers in"),
                             ("Vehicle", "Fleet", "Shift volume toward"), ("Area", "Zone", "Investigate"),
                             ("Category", "Category", "Review handling for")]:
        r = slowest(col)
        if r is None:
            continue
        hi, hv, lo, lv = r
        if col == "Vehicle":
            items.append((label, f"{verb} {lo}", f"{lo} averages {lv:.0f} min vs {hi} at {hv:.0f} min ({hv - lv:+.0f} min gap)."))
        else:
            items.append((label, f"{verb} {hi}", f"{hi} averages {hv:.0f} min — {hv - lv:.0f} min slower than {lo}."))
    late_rate = (f["Delivery_Time"] > threshold).mean() * 100
    items.append(("Target", f"{late_rate:.1f}% late", f"Orders over {threshold:.0f} min in this selection. Track it weekly against the same rule."))

    cards = "".join(f'<div class="it"><div class="h">{h}</div><div class="b">{b}</div><div class="d">{d}</div></div>' for h, b, d in items[:6])
    if True:
        st.markdown(
            f'<div style="background:{YELLOW};border-radius:22px;padding:20px 22px 22px;color:{NAVY}">'
            f'<div class="sec-label">03 / Act — what this selection suggests</div>'
            f'<div class="act">{cards}</div>'
            f'<div class="foot" style="margin-top:12px;opacity:.75">Groups under 30 orders are skipped. '
            f'These are prompts for investigation, not causal findings.</div></div>',
            unsafe_allow_html=True,
        )


def pipeline(log: dict, f: pd.DataFrame) -> None:
    st.markdown(
        f"""
<div class="pipe">
  <div class="sec-label">04 / From raw data to decisions</div>
  <div class="row">
    <div class="stage" style="background:{YELLOW}"><b>LOAD</b><div class="big">{log['loaded']:,}</div>
      <ul><li>rows × {log['columns']} columns read</li><li>required columns checked</li></ul></div>
    <div class="stage" style="background:#FFA98A"><b>CLEAN</b>
      <ul><li>{log['dropped_missing_core']} rows dropped (no weather / traffic / time)</li>
      <li>{log['invalid_ratings']} ratings &gt; 5 marked invalid; {log['ratings_imputed']} filled with median</li>
      <li>{log['coords_sign_fixed']:,} flipped coordinates fixed</li>
      <li>{log['coords_placeholder']:,} placeholder points kept off the map</li>
      <li>labels trimmed; "Metropolitian" → Metropolitan; {log['duplicates']} duplicates</li></ul></div>
    <div class="stage" style="background:#BDEBCB"><b>FILTER</b><div class="big">{len(f):,}</div>
      <ul><li>rows match the sidebar selection</li><li>of {log['final']:,} cleaned rows</li></ul></div>
    <div class="stage" style="background:#D6CCF7"><b>COMPUTE</b>
      <ul><li>group → count → mean / median / SD</li><li>Late flag (mean + 1 SD = {log['late_threshold']:.0f} min)</li>
      <li>Age group, distance (km), order hour, pickup wait</li></ul></div>
    <div class="stage" style="background:{NAVY};color:#fff"><b style="color:{YELLOW}">REFRESH</b>
      <ul><li>KPIs + every chart redraw</li><li>empty selections flagged</li><li>sample sizes shown on hover</li></ul></div>
  </div>
  <div class="note"><b>EVERY FILTER CHANGE REPEATS: FILTER › COMPUTE › REFRESH.</b><br>
  Delay rate = 100 × deliveries above the target ÷ all valid deliveries. Delivery_Time is in minutes.</div>
</div>
""",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    inject_css()
    df, log = load_and_clean(DATA_PATH)
    ctl = sidebar(df, log)
    f = apply_filters(df, ctl["selections"])
    threshold = ctl["threshold"]

    hero(log)

    active = [f"{k}: {', '.join(v)}" for k, v in ctl["selections"].items() if v]
    st.markdown(
        f'<div class="board-head"><span class="sec-label">02 / The dashboard storyboard</span>'
        f'<span class="hint">{" · ".join(active).upper() if active else "ALL RECORDS — USE THE SIDEBAR TO DRILL DOWN"}</span></div>',
        unsafe_allow_html=True,
    )

    if f.empty:
        st.markdown('<div class="empty"><b>No deliveries match this selection.</b>'
                    'Loosen a filter or hit “Reset filters” in the sidebar.</div>', unsafe_allow_html=True)
        pipeline(log, f)
        return

    kpi_row(f, df, threshold)
    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    c1, c2 = st.columns(2, gap="medium")
    with c1:
        delay_analyzer(f)
    with c2:
        vehicle_comparison(f)

    c3, c4 = st.columns(2, gap="medium")
    with c3:
        agent_insights(f)
    with c4:
        regional_bottlenecks(f)

    category_explorer(f, threshold)

    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    c6, c7, c8 = st.columns(3, gap="medium")
    with c6:
        trends(f, threshold)
    with c7:
        distribution(f, threshold)
    with c8:
        late_by_condition(f, threshold)

    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    act_panel(f, threshold)
    pipeline(log, f)

    with st.expander(f"Inspect the filtered records ({len(f):,} rows)"):
        cols = ["Order_ID", "Order_Date", "Weather", "Traffic", "Vehicle", "Area", "Category",
                "Agent_Age", "Agent_Rating", "Distance_km", "Delivery_Time"]
        st.dataframe(f[cols].head(500), width="stretch", hide_index=True)
        st.download_button("Download filtered CSV", f[cols].to_csv(index=False).encode(), "filtered_deliveries.csv", "text/csv")

    st.markdown(
        '<div class="footer"><span>SOURCE / Amazon last-mile delivery dataset (43,739 orders, Feb–Apr 2022)</span>'
        '<span>LOGISIGHT ANALYTICS · FA-2 BUILD / 02</span></div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
