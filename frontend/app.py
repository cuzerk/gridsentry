"""
StormLines — Predictive Grid Reliability Dashboard
Streamlit + pydeck  |  Feb 2026 New England Nor'easter

Run:
    cd frontend
    streamlit run app.py
"""

from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

# ── PAGE CONFIG ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="StormLines — Predictive Grid Reliability",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CUSTOM CSS ────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
        .stApp { background-color: #0d1117; }
        [data-testid="stSidebar"] { background-color: #161b22; }
        .metric-card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 12px 16px;
            margin: 4px 0;
        }
        h1 { color: #e6edf3 !important; }
        .stMarkdown p { color: #8b949e; }
        div[data-testid="metric-container"] {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 12px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── DATA ──────────────────────────────────────────────────────────────────────
_DATA_PATH   = os.path.join(os.path.dirname(__file__), "data", "prediction_timeline.json")
_ROUTES_PATH = os.path.join(os.path.dirname(__file__), "data", "routes.json")

# lerpColor('#73BC84', '#E5EEC1', level / highestLevel) — matches busrouter visualization.js
_COLOR_A = (0x73, 0xBC, 0x84)  # #73BC84  forest green  (level 0)
_COLOR_B = (0xE5, 0xEE, 0xC1)  # #E5EEC1  light cream   (level max)
_HIGHEST_LEVEL = 5


def _lerp_color(level: int) -> list[int]:
    t = level / _HIGHEST_LEVEL
    return [int(_COLOR_A[i] + t * (_COLOR_B[i] - _COLOR_A[i])) for i in range(3)]


@st.cache_data
def load_routes() -> pd.DataFrame | None:
    if not os.path.exists(_ROUTES_PATH):
        return None
    with open(_ROUTES_PATH) as fh:
        routes = json.load(fh)
    for r in routes:
        r["color"] = _lerp_color(r["level"])
    return pd.DataFrame(routes)


@st.cache_data
def load_data() -> tuple[pd.DataFrame, dict]:
    if not os.path.exists(_DATA_PATH):
        st.error(
            "prediction_timeline.json not found. "
            "Run `cd backend && python pipeline.py` first."
        )
        st.stop()
    with open(_DATA_PATH) as fh:
        raw = json.load(fh)
    df = pd.DataFrame(raw["records"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["outage_pct_observed"] = pd.to_numeric(df["outage_pct_observed"], errors="coerce")
    return df, raw["metadata"]


df, meta   = load_data()
routes_df  = load_routes()
timestamps = sorted(df["timestamp"].unique())
n_frames   = len(timestamps)

# ── SESSION STATE ─────────────────────────────────────────────────────────────
if "frame"   not in st.session_state:
    st.session_state.frame   = 0
if "playing" not in st.session_state:
    st.session_state.playing = False

# ── SIDEBAR ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚡ StormLines")
    st.markdown("**Predictive Grid Maintenance**")
    st.markdown("*Southern New England — Feb 2026*")
    st.divider()

    col_a, col_b = st.columns(2)
    with col_a:
        play_label = "⏸ Pause" if st.session_state.playing else "▶ Play"
        if st.button(play_label, use_container_width=True):
            st.session_state.playing = not st.session_state.playing
    with col_b:
        if st.button("↩ Reset", use_container_width=True):
            st.session_state.frame   = 0
            st.session_state.playing = False

    _speed_map = {"0.25×": 0.25, "0.5×": 0.5, "1×": 1.0, "2×": 2.0, "4×": 4.0}
    speed = _speed_map[st.select_slider(
        "Animation Speed",
        options=list(_speed_map.keys()),
        value="1×",
    )]

    st.divider()

    view_pitch = st.slider("Map Pitch (°)", 0, 60, 45)
    col_height = st.slider("Column Height Scale", 1, 5, 3)

    st.divider()
    st.markdown("**Risk Legend**")
    st.markdown("🟢 &nbsp; 0–25 %   — Low")
    st.markdown("🟡 &nbsp; 25–50 % — Moderate")
    st.markdown("🟠 &nbsp; 50–75 % — High")
    st.markdown("🔴 &nbsp; 75–100% — Critical")

    st.divider()
    st.markdown("**Transmission Lines**")
    show_lines = st.checkbox("Show transmission lines", value=True)
    if show_lines:
        # Colors from lerpColor('#73BC84','#E5EEC1', level/5)
        st.markdown('<span style="color:#e5eec1">━━</span> 345 kV (level 5)', unsafe_allow_html=True)
        st.markdown('<span style="color:#cee4b5">━━</span> 230 kV (level 4)', unsafe_allow_html=True)
        st.markdown('<span style="color:#b7daa9">━━</span> 138 kV (level 3)', unsafe_allow_html=True)
        st.markdown('<span style="color:#a1d09c">━━</span> 115 kV (level 2)', unsafe_allow_html=True)
        st.markdown('<span style="color:#8ac690">━━</span> 69 kV  (level 1)', unsafe_allow_html=True)

    st.divider()
    st.markdown(f"**Locations:** {meta.get('locations', '—')}")
    st.markdown(f"**Records:** {meta.get('records', 0):,}")
    st.caption(f"Generated: {meta.get('generated_at','—')[:19]}")

# ── TITLE ROW ─────────────────────────────────────────────────────────────────
st.markdown("# ⚡ StormLines — February 2026 Nor'easter")
st.markdown(
    "AI-driven predictive maintenance · Random Forest · "
    "Southern New England Power Grid · 17 sensor locations"
)

# ── TIME SCRUBBER ─────────────────────────────────────────────────────────────
_ts_labels = [pd.Timestamp(t).strftime("%b %d  %H:%M") for t in timestamps]
selected_label = st.select_slider(
    "Timestamp",
    options=_ts_labels,
    value=_ts_labels[st.session_state.frame],
)
frame = _ts_labels.index(selected_label)
st.session_state.frame = frame

current_ts = timestamps[frame]
cur        = df[df["timestamp"] == current_ts].copy()

# ── COLOR + ELEVATION MAPPING ─────────────────────────────────────────────────
def prob_to_rgba(p: float) -> list[int]:
    """Smooth green→yellow→orange→red gradient with alpha ramping."""
    p = float(np.clip(p, 0, 1))
    if p < 0.25:
        t = p / 0.25
        r, g, b = int(t * 255), 210, 60
        a = 140 + int(t * 80)
    elif p < 0.5:
        t = (p - 0.25) / 0.25
        r, g, b = 255, int(210 - t * 110), 0
        a = 185 + int(t * 40)
    elif p < 0.75:
        t = (p - 0.5) / 0.25
        r, g, b = 255, int(100 - t * 80), 0
        a = 205 + int(t * 30)
    else:
        t = (p - 0.75) / 0.25
        r, g, b = 255, int(20 - t * 15), int(t * 40)
        a = 230
    return [r, g, b, a]


cur["color"]     = cur["incident_probability"].apply(prob_to_rgba)
cur["elevation"] = cur["incident_probability"] * 70_000 * col_height   # metres

# ── STORM PHASE LABEL ─────────────────────────────────────────────────────────
ts_obj = pd.Timestamp(current_ts)
if ts_obj.day == 22 and ts_obj.hour < 18:
    phase, phase_color = "Pre-Storm", "#3fb950"
elif ts_obj.day == 22 or (ts_obj.day == 23 and ts_obj.hour < 6):
    phase, phase_color = "Storm Building ⚠️", "#d29922"
elif ts_obj.day == 23:
    phase, phase_color = "STORM PEAK 🔴", "#f85149"
else:
    phase, phase_color = "Dissipating", "#58a6ff"

# ── METRICS ROW ───────────────────────────────────────────────────────────────
m1, m2, m3, m4, m5 = st.columns(5)
with m1:
    st.metric(
        "Storm Phase",
        phase,
    )
with m2:
    peak_prob = cur["incident_probability"].max()
    peak_loc  = cur.loc[cur["incident_probability"].idxmax(), "location_name"]
    st.metric("Peak Risk", f"{peak_prob:.1%}", delta=peak_loc, delta_color="off")
with m3:
    high_risk = int((cur["incident_probability"] > 0.50).sum())
    st.metric("High-Risk Zones", f"{high_risk} / {len(cur)}")
with m4:
    avg_wind = cur["wind_speed_mph"].mean()
    max_gust = cur["wind_gusts_mph"].max()
    st.metric("Avg Wind", f"{avg_wind:.0f} mph", delta=f"Gusts {max_gust:.0f} mph", delta_color="off")
with m5:
    total_snow = cur["snowfall_in"].sum()
    st.metric("Total Snow Rate", f"{total_snow:.1f} in/hr across all zones")

# ── DECK.GL LAYERS ────────────────────────────────────────────────────────────
routes_layer = pdk.Layer(
    "PathLayer",
    data=routes_df,
    get_path="path",           # [lon, lat, elevation] triplets — elevation = (level-1)*100 m
    get_color="color",         # lerpColor('#73BC84', '#E5EEC1', level/5)
    get_width=100,
    width_min_pixels=2,
    opacity=1,
    pickable=True,
    auto_highlight=True,
    highlight_color=[255, 255, 255, 255],
    parameters={"depthTest": False, "blend": True},
)

column_layer = pdk.Layer(
    "ColumnLayer",
    data=cur,
    get_position=["longitude", "latitude"],
    get_elevation="elevation",
    elevation_scale=1,
    radius=7_500,
    get_fill_color="color",
    pickable=True,
    auto_highlight=True,
    extruded=True,
    coverage=0.9,
)

view_state = pdk.ViewState(
    latitude=41.85,
    longitude=-71.0,
    zoom=7.4,
    pitch=view_pitch,
    bearing=-10,
)

tooltip = {
    "html": """
        <div style="
            background: rgba(13,17,23,0.92);
            border: 1px solid #30363d;
            padding: 14px 16px;
            border-radius: 10px;
            color: #e6edf3;
            font-family: 'SF Mono', 'Fira Code', monospace;
            font-size: 12px;
            min-width: 220px;
        ">
            <b style="font-size:14px; color:#58a6ff;">{location_name}</b>
            <hr style="border-color:#30363d; margin:8px 0;">
            ⚡ <b>Incident Prob:</b> &nbsp;{incident_probability}<br/>
            💨 <b>Wind:</b> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{wind_speed_mph} mph<br/>
            🌪 <b>Gusts:</b> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{wind_gusts_mph} mph<br/>
            🌨 <b>Snowfall:</b> &nbsp;&nbsp;&nbsp;{snowfall_in} in/hr<br/>
            📏 <b>Snow Depth:</b> &nbsp;{snow_depth_in} in<br/>
            🌡 <b>Temp:</b> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{temperature_f} °F<br/>
            📉 <b>Pressure:</b> &nbsp;&nbsp;&nbsp;{pressure_hpa} hPa<br/>
        </div>
    """,
    "style": {"backgroundColor": "transparent", "border": "none"},
}

_layers = []
if show_lines and routes_df is not None:
    _layers.append(routes_layer)
_layers.append(column_layer)

deck = pdk.Deck(
    layers=_layers,
    initial_view_state=view_state,
    tooltip=tooltip,
    map_style="mapbox://styles/jacksonkoehler11/cmqs965gr002s01qohn60d8lh",
    map_provider="mapbox",
    api_keys={"mapbox": os.environ.get("MAPBOX_TOKEN", "")},
)

st.pydeck_chart(deck, use_container_width=True, height=580)

# ── RISK TABLE ────────────────────────────────────────────────────────────────
with st.expander("📊 Location Risk Details (sorted by risk)", expanded=False):
    display = cur[[
        "location_name", "incident_probability",
        "wind_speed_mph", "wind_gusts_mph",
        "snowfall_in", "snow_depth_in", "temperature_f",
    ]].copy().sort_values("incident_probability", ascending=False).reset_index(drop=True)

    display.columns = [
        "Location", "Risk %", "Wind (mph)", "Gusts (mph)",
        "Snow Rate (in/hr)", "Snow Depth (in)", "Temp (°F)",
    ]
    display["Risk %"] = display["Risk %"].apply(lambda x: f"{x:.1%}")
    st.dataframe(display, use_container_width=True, hide_index=True)

# ── PROBABILITY SPARKLINES ────────────────────────────────────────────────────
with st.expander("📈 72-Hour Risk Profiles by Location", expanded=False):
    top5_locs = (
        df.groupby("location_name")["incident_probability"]
        .max()
        .nlargest(5)
        .index.tolist()
    )
    pivot = (
        df[df["location_name"].isin(top5_locs)]
        .pivot(index="timestamp", columns="location_name", values="incident_probability")
        .sort_index()
    )
    st.line_chart(pivot, height=250, use_container_width=True)
    st.caption("Top 5 highest-risk locations over the 72-hour event window.")

# ── AUTO-PLAY ENGINE ──────────────────────────────────────────────────────────
if st.session_state.playing:
    delay = max(0.05, 0.5 / speed)
    time.sleep(delay)
    next_frame = st.session_state.frame + 1
    if next_frame >= n_frames:
        st.session_state.playing = False
        st.session_state.frame   = 0
    else:
        st.session_state.frame = next_frame
    st.rerun()
