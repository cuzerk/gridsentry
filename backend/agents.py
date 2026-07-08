"""
StormLines — Predictive Grid Maintenance Pipeline
LangGraph multi-agent system for the Feb 23, 2026 New England Nor'easter.

Three agents run as a deterministic StateGraph (no LLM calls required):
  1. WeatherAggregator  — Open-Meteo archive API with realistic fallback
  2. GridOpsArchivist   — Physics-informed outage simulation per location
  3. PredictiveAnalyst  — Random Forest trained on 10 synthetic historical storms
"""

from __future__ import annotations

import json
import os
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from operator import add
from typing import Annotated, Optional, TypedDict

from langgraph.graph import END, StateGraph
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# ── STUDY AREA ────────────────────────────────────────────────────────────────
# Southern New England locations with infrastructure vulnerability encoding.
# coast_factor (0–1): 1 = fully exposed coast/island, 0 = inland/sheltered.
LOCATIONS: list[dict] = [
    {"id": "orleans_ma",      "name": "Orleans, MA",       "lat": 41.7897, "lon": -69.9893, "coast_factor": 0.95},
    {"id": "chatham_ma",      "name": "Chatham, MA",       "lat": 41.6798, "lon": -69.9593, "coast_factor": 0.97},
    {"id": "oak_bluffs_ma",   "name": "Oak Bluffs, MA",    "lat": 41.4554, "lon": -70.5571, "coast_factor": 0.98},
    {"id": "barnstable_ma",   "name": "Barnstable, MA",    "lat": 41.7003, "lon": -70.3002, "coast_factor": 0.88},
    {"id": "yarmouth_ma",     "name": "Yarmouth, MA",      "lat": 41.7068, "lon": -70.2286, "coast_factor": 0.87},
    {"id": "falmouth_ma",     "name": "Falmouth, MA",      "lat": 41.5512, "lon": -70.6176, "coast_factor": 0.90},
    {"id": "plymouth_ma",     "name": "Plymouth, MA",      "lat": 41.9584, "lon": -70.6673, "coast_factor": 0.75},
    {"id": "new_bedford_ma",  "name": "New Bedford, MA",   "lat": 41.6362, "lon": -70.9342, "coast_factor": 0.80},
    {"id": "newport_ri",      "name": "Newport, RI",       "lat": 41.4901, "lon": -71.3128, "coast_factor": 0.92},
    {"id": "fall_river_ma",   "name": "Fall River, MA",    "lat": 41.7015, "lon": -71.1550, "coast_factor": 0.60},
    {"id": "boston_ma",       "name": "Boston, MA",        "lat": 42.3601, "lon": -71.0589, "coast_factor": 0.55},
    {"id": "brockton_ma",     "name": "Brockton, MA",      "lat": 42.0834, "lon": -71.0184, "coast_factor": 0.35},
    {"id": "new_haven_ct",    "name": "New Haven, CT",     "lat": 41.3083, "lon": -72.9279, "coast_factor": 0.70},
    {"id": "providence_ri",   "name": "Providence, RI",    "lat": 41.8240, "lon": -71.4128, "coast_factor": 0.40},
    {"id": "worcester_ma",    "name": "Worcester, MA",     "lat": 42.2626, "lon": -71.8023, "coast_factor": 0.20},
    {"id": "springfield_ma",  "name": "Springfield, MA",   "lat": 42.1015, "lon": -72.5898, "coast_factor": 0.10},
    {"id": "hartford_ct",     "name": "Hartford, CT",      "lat": 41.7658, "lon": -72.6851, "coast_factor": 0.15},
]

EVENT_START = "2026-02-22"
EVENT_END   = "2026-02-24"

FEATURE_COLS = [
    "wind_speed_10m",
    "wind_gusts_10m",
    "snowfall",
    "snow_depth",
    "temperature_2m",
    "surface_pressure",
    "precipitation",
    "relative_humidity_2m",
    "coast_factor",
]


# ── PIPELINE STATE ─────────────────────────────────────────────────────────────
class StormLinesState(TypedDict):
    weather_data: dict          # {location_id: [hourly_row, ...]}
    outage_data:  dict          # {location_id: [hourly_row, ...]}
    predictions:  list          # flat list of prediction dicts for deck.gl
    output_path:  str
    log: Annotated[list[str], add]  # appended across nodes


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 1 — WEATHER SENSOR AGGREGATOR
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_open_meteo(loc: dict) -> list[dict]:
    """Pull hourly weather from the Open-Meteo archive API (free, no key)."""
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude":    loc["lat"],
        "longitude":   loc["lon"],
        "start_date":  EVENT_START,
        "end_date":    EVENT_END,
        "hourly": (
            "wind_speed_10m,wind_gusts_10m,snowfall,snow_depth,"
            "temperature_2m,surface_pressure,precipitation,relative_humidity_2m"
        ),
        "wind_speed_unit": "mph",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": "America/New_York",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    h = r.json()["hourly"]
    rows = []
    for i, ts in enumerate(h["time"]):
        rows.append({
            "timestamp":            ts,
            "wind_speed_10m":       float(h["wind_speed_10m"][i]       or 0),
            "wind_gusts_10m":       float(h["wind_gusts_10m"][i]       or 0),
            "snowfall":             float(h["snowfall"][i]              or 0),
            "snow_depth":           float(h["snow_depth"][i]            or 0),
            "temperature_2m":       float(h["temperature_2m"][i]       or 32),
            "surface_pressure":     float(h["surface_pressure"][i]     or 1013),
            "precipitation":        float(h["precipitation"][i]         or 0),
            "relative_humidity_2m": float(h["relative_humidity_2m"][i] or 75),
        })
    return rows


def _mock_weather(loc: dict) -> list[dict]:
    """
    Realistic fallback for the Feb 22-24 2026 Nor'easter.
    Storm profile: gradual build (h 0-20) → explosive deepening (h 20-44) → decay (h 44-72).
    Cape Cod / coastal locations receive amplified winds via coast_factor.
    Temperatures stay in the 26-34°F wet-snow zone during the peak — maximum icing risk.
    """
    rng = np.random.default_rng(seed=int(abs(hash(loc["id"]))) % (2**31))
    cf   = loc["coast_factor"]
    rows = []
    cumulative_snow = 0.0

    for h in range(72):
        dt = datetime(2026, 2, 22, 0, 0) + timedelta(hours=h)

        if h < 20:
            # Pre-storm: slow wind-up, temps dropping
            phase       = h / 20
            base_wind   = 8  + phase * 18 * cf + rng.normal(0, 2)
            base_gust   = base_wind * 1.30
            temp        = 40 - h * 0.6 + rng.normal(0, 1)
            pressure    = 1015 - h * 0.8
            snowfall    = max(0.0, rng.normal(0.05, 0.03)) if h > 14 else 0.0
            precip      = snowfall * 0.09
            humidity    = 65 + phase * 20 + rng.normal(0, 3)

        elif h < 44:
            # Storm peak — Nor'easter explosively deepens overnight Feb 22→23
            storm_h     = h - 20
            phase       = np.sin(storm_h / 24 * np.pi)            # 0→1→0
            base_wind   = 30 + phase * 40 * cf + rng.normal(0, 5)
            base_gust   = base_wind * (1.40 + 0.15 * cf)
            temp        = 30 + rng.normal(0, 2)                    # wet-snow zone
            pressure    = 995 - phase * 20 + rng.normal(0, 1.5)
            snowfall    = max(0.0, rng.normal(1.8 * cf, 0.45))
            precip      = snowfall * 0.09
            humidity    = min(100, 85 + phase * 10 + rng.normal(0, 2))

        else:
            # Decay: winds ease, clearing skies, temps drop further
            decay_h     = h - 44
            decay_exp   = np.exp(-decay_h / 14)
            base_wind   = max(5, 55 * cf * decay_exp + rng.normal(0, 3))
            base_gust   = base_wind * 1.25
            temp        = 25 + decay_h * 0.35 + rng.normal(0, 1)
            pressure    = 1000 + decay_h * 0.5
            snowfall    = max(0.0, rng.normal(0.2, 0.15) * decay_exp)
            precip      = snowfall * 0.08
            humidity    = max(50, 90 - decay_h * 1.5 + rng.normal(0, 3))

        # Cumulative snow depth (10% hourly settling/compaction)
        cumulative_snow = cumulative_snow * 0.90 + snowfall
        snow_depth_val  = round(cumulative_snow, 3)

        rows.append({
            "timestamp":            dt.strftime("%Y-%m-%dT%H:%M"),
            "wind_speed_10m":       round(max(0, base_wind),    2),
            "wind_gusts_10m":       round(max(0, base_gust),    2),
            "snowfall":             round(max(0, snowfall),      3),
            "snow_depth":           snow_depth_val,
            "temperature_2m":       round(temp,                  1),
            "surface_pressure":     round(pressure,              1),
            "precipitation":        round(max(0, precip),        3),
            "relative_humidity_2m": round(min(100, humidity),    1),
        })

    return rows


def weather_aggregator_node(state: StormLinesState) -> dict:
    """Agent 1: Fetch hourly meteorological data for all 17 study-area locations."""
    weather_data: dict = {}
    source_note: str   = "Open-Meteo archive API"

    for loc in LOCATIONS:
        try:
            rows = _fetch_open_meteo(loc)
            weather_data[loc["id"]] = rows
        except Exception as exc:
            rows = _mock_weather(loc)
            weather_data[loc["id"]] = rows
            source_note = f"mock fallback (API error: {exc})"

    return {
        "weather_data": weather_data,
        "log": [
            f"[Agent 1 — WeatherAggregator] Fetched {len(LOCATIONS)} locations "
            f"× ~72 h via {source_note}."
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 2 — GRID OPS ARCHIVIST
# ═══════════════════════════════════════════════════════════════════════════════

def _simulate_outage(loc: dict, weather_rows: list[dict]) -> list[dict]:
    """
    Physics-informed outage accumulation model.

    Damage mechanisms:
      - Wind: sustained >35 mph → minor tree contact; gusts >45 mph → branch failures;
              gusts >60 mph → line snaps, pole failures (exponential scaling).
      - Icing: wet heavy snow at 26-34°F is the worst case; dry cold snow does
               far less line damage.  Peak icing at exactly 30°F.
      - Coastal amplification: salt spray weakens hardware; higher sustained winds.
      - Infrastructure vulnerability: Cape Cod towns carry 40+ yr old feeder lines.

    Restoration:
      - Crews cannot safely work above 45 mph sustained; rate drops to near-zero.
      - After storm, outage decays exponentially at 6-10 % per hour.
    """
    rng       = np.random.default_rng(seed=int(abs(hash(loc["id"] + "outage"))) % (2**31))
    infra_vuln = 0.28 + loc["coast_factor"] * 0.52  # 0.28 (inland) → 0.80 (coast)

    outage_rows   = []
    running_outage = 0.0

    for row in weather_rows:
        wind  = row["wind_speed_10m"]
        gust  = row["wind_gusts_10m"]
        snow  = row["snowfall"]
        depth = row["snow_depth"]
        temp  = row["temperature_2m"]

        # Wind damage contribution (per hour)
        if gust > 60:
            wind_damage = 0.20 + (gust - 60) / 50 * 0.45
        elif gust > 45:
            wind_damage = 0.06 + (gust - 45) / 15 * 0.14
        elif gust > 35:
            wind_damage = (gust - 35) / 10 * 0.06
        else:
            wind_damage = 0.0

        # Ice/wet-snow damage (worst at 30°F; negligible below 22°F or above 36°F)
        if 22 <= temp <= 36:
            icing_factor = max(0, 1.0 - abs(temp - 30) / 8)
            snow_damage  = snow * icing_factor * 0.12 + depth * icing_factor * 0.004
        else:
            snow_damage  = snow * 0.01

        # Combined instantaneous damage scaled by infrastructure vulnerability
        instant_damage = (wind_damage * 0.65 + snow_damage * 0.35) * infra_vuln
        instant_damage = max(0, instant_damage + rng.normal(0, 0.015))

        # Restoration rate (near-zero in high winds)
        if wind < 20:
            restore_rate = 0.10
        elif wind < 35:
            restore_rate = 0.04
        elif wind < 50:
            restore_rate = 0.01
        else:
            restore_rate = 0.003

        running_outage = min(0.95, running_outage * (1 - restore_rate) + instant_damage)
        running_outage = max(0.0, running_outage)

        outage_rows.append({
            "timestamp":  row["timestamp"],
            "outage_pct": round(running_outage + rng.normal(0, 0.008), 4),
            "wind_damage": round(wind_damage,  4),
            "snow_damage": round(snow_damage,  4),
        })

    return outage_rows


def grid_archivist_node(state: StormLinesState) -> dict:
    """Agent 2: Generate realistic outage accumulation curves per location."""
    outage_data: dict  = {}
    location_map       = {loc["id"]: loc for loc in LOCATIONS}

    for loc_id, weather_rows in state["weather_data"].items():
        loc = location_map[loc_id]
        outage_data[loc_id] = _simulate_outage(loc, weather_rows)

    peak_loc = max(outage_data, key=lambda k: max(r["outage_pct"] for r in outage_data[k]))
    peak_val = max(r["outage_pct"] for r in outage_data[peak_loc])

    return {
        "outage_data": outage_data,
        "log": [
            f"[Agent 2 — GridOpsArchivist] Simulated outage curves for {len(outage_data)} locations. "
            f"Peak observed: {peak_val:.1%} at {location_map[peak_loc]['name']}."
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 3 — PREDICTIVE ANALYTICS ENGINEER
# ═══════════════════════════════════════════════════════════════════════════════

_HISTORICAL_STORMS = [
    {"name": "Grayson 2018",       "peak_wind": 70, "snowfall_peak": 1.8, "temp_base": 14, "duration_h": 36},
    {"name": "Harper 2019",        "peak_wind": 55, "snowfall_peak": 1.4, "temp_base": 22, "duration_h": 42},
    {"name": "Thanksgiving 2019",  "peak_wind": 45, "snowfall_peak": 0.9, "temp_base": 30, "duration_h": 30},
    {"name": "Orlena 2021",        "peak_wind": 40, "snowfall_peak": 2.1, "temp_base": 28, "duration_h": 48},
    {"name": "Kenan 2022",         "peak_wind": 60, "snowfall_peak": 1.0, "temp_base": 18, "duration_h": 40},
    {"name": "Elliott 2022",       "peak_wind": 65, "snowfall_peak": 0.4, "temp_base": 5,  "duration_h": 36},
    {"name": "Harold 2023",        "peak_wind": 50, "snowfall_peak": 1.5, "temp_base": 26, "duration_h": 44},
    {"name": "Jocelyn 2024",       "peak_wind": 55, "snowfall_peak": 1.2, "temp_base": 29, "duration_h": 38},
    {"name": "Kiona 2024",         "peak_wind": 48, "snowfall_peak": 0.7, "temp_base": 32, "duration_h": 32},
    {"name": "Maren 2025",         "peak_wind": 52, "snowfall_peak": 1.7, "temp_base": 27, "duration_h": 46},
]


def _build_training_data() -> pd.DataFrame:
    """
    Synthesize labeled hourly records for 10 historical NE winter storms.
    Ground truth outage_pct is computed via the same physics model used in Agent 2,
    so the RF learns causal weather→outage relationships rather than noise.
    """
    rng  = np.random.default_rng(seed=42)
    rows = []

    for storm in _HISTORICAL_STORMS:
        for loc in LOCATIONS:
            cf   = loc["coast_factor"]
            dur  = storm["duration_h"]
            running_outage = 0.0
            cumulative_snow = 0.0

            for h in range(dur):
                phase = np.sin(h / dur * np.pi)

                wind  = max(3, storm["peak_wind"] * phase * cf + rng.normal(0, 5))
                gust  = wind * (1.38 + rng.normal(0, 0.05))
                snow  = max(0, storm["snowfall_peak"] * phase * cf + rng.normal(0, 0.15))
                temp  = storm["temp_base"] + rng.normal(0, 3)
                pres  = 1013 - phase * 28 + rng.normal(0, 2)
                cumulative_snow = cumulative_snow * 0.9 + snow
                depth = cumulative_snow
                prec  = snow * 0.09
                hum   = min(100, 75 + phase * 18 + rng.normal(0, 3))

                # Same physics as _simulate_outage for consistent ground truth
                infra = 0.28 + cf * 0.52
                if gust > 60:
                    wd = 0.20 + (gust - 60) / 50 * 0.45
                elif gust > 45:
                    wd = 0.06 + (gust - 45) / 15 * 0.14
                elif gust > 35:
                    wd = (gust - 35) / 10 * 0.06
                else:
                    wd = 0.0
                if 22 <= temp <= 36:
                    ic = max(0, 1.0 - abs(temp - 30) / 8)
                    sd = snow * ic * 0.12 + depth * ic * 0.004
                else:
                    sd = snow * 0.01
                damage = (wd * 0.65 + sd * 0.35) * infra + rng.normal(0, 0.01)

                restore = 0.10 if wind < 20 else (0.04 if wind < 35 else (0.01 if wind < 50 else 0.003))
                running_outage = min(0.95, max(0, running_outage * (1 - restore) + damage))

                rows.append({
                    "wind_speed_10m":       round(wind, 2),
                    "wind_gusts_10m":       round(gust, 2),
                    "snowfall":             round(snow, 3),
                    "snow_depth":           round(depth, 3),
                    "temperature_2m":       round(temp, 1),
                    "surface_pressure":     round(pres, 1),
                    "precipitation":        round(prec, 4),
                    "relative_humidity_2m": round(hum,  1),
                    "coast_factor":         cf,
                    "outage_pct":           round(running_outage, 4),
                })

    return pd.DataFrame(rows)


def predictive_analyst_node(state: StormLinesState) -> dict:
    """Agent 3: Train Random Forest on historical storms, predict Feb 2026 probabilities."""

    # 3a — Training
    hist_df = _build_training_data()
    X_train = hist_df[FEATURE_COLS]
    y_train = hist_df["outage_pct"]

    model = Pipeline([
        ("scaler", StandardScaler()),
        ("rf", RandomForestRegressor(
            n_estimators=300,
            max_depth=14,
            min_samples_leaf=3,
            max_features="sqrt",
            random_state=42,
            n_jobs=-1,
        )),
    ])
    model.fit(X_train, y_train)
    train_rmse = mean_squared_error(y_train, model.predict(X_train)) ** 0.5

    # Feature importances for the log
    fi   = model.named_steps["rf"].feature_importances_
    top2 = sorted(zip(FEATURE_COLS, fi), key=lambda x: -x[1])[:2]
    fi_str = ", ".join(f"{n}={v:.3f}" for n, v in top2)

    # 3b — Inference over Feb 2026 event
    location_map = {loc["id"]: loc for loc in LOCATIONS}
    predictions: list[dict] = []

    for loc_id, weather_rows in state["weather_data"].items():
        loc         = location_map[loc_id]
        outage_rows = state["outage_data"].get(loc_id, [])
        outage_map  = {r["timestamp"]: max(0.0, r["outage_pct"]) for r in outage_rows}

        for row in weather_rows:
            feat_row = {col: row.get(col, 0.0) for col in FEATURE_COLS if col != "coast_factor"}
            feat_row["coast_factor"] = loc["coast_factor"]
            feat_df = pd.DataFrame([feat_row])[FEATURE_COLS]

            prob = float(model.predict(feat_df)[0])
            prob = round(max(0.0, min(1.0, prob)), 4)

            # Normalise timestamp to ISO-8601
            ts = row["timestamp"]
            if "T" not in ts:
                ts = ts.replace(" ", "T")
            if len(ts) == 16:          # "YYYY-MM-DDTHH:MM"
                ts += ":00"

            predictions.append({
                "timestamp":             ts,
                "location_id":           loc_id,
                "location_name":         loc["name"],
                "longitude":             loc["lon"],
                "latitude":              loc["lat"],
                "incident_probability":  prob,
                "wind_speed_mph":        round(row.get("wind_speed_10m", 0), 1),
                "wind_gusts_mph":        round(row.get("wind_gusts_10m", 0), 1),
                "snowfall_in":           round(row.get("snowfall",        0), 2),
                "snow_depth_in":         round(row.get("snow_depth",      0), 2),
                "temperature_f":         round(row.get("temperature_2m",  32), 1),
                "pressure_hpa":          round(row.get("surface_pressure", 1013), 1),
                "outage_pct_observed":   outage_map.get(row["timestamp"]),
            })

    return {
        "predictions": predictions,
        "log": [
            f"[Agent 3 — PredictiveAnalyst] Trained RF on {len(hist_df):,} samples "
            f"({len(_HISTORICAL_STORMS)} storms × {len(LOCATIONS)} locations). "
            f"Train RMSE: {train_rmse:.4f}. Top features: {fi_str}.",
            f"[Agent 3 — PredictiveAnalyst] Generated {len(predictions):,} predictions "
            f"({len(LOCATIONS)} locations × {len(predictions)//len(LOCATIONS)} hours).",
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# DATA EXPORTER
# ═══════════════════════════════════════════════════════════════════════════════

def data_exporter_node(state: StormLinesState) -> dict:
    """Write prediction_timeline.json in deck.gl-ready format."""
    output_path = state["output_path"]
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    output = {
        "metadata": {
            "event":        "February 2026 New England Nor'easter",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "locations":    len(LOCATIONS),
            "records":      len(state["predictions"]),
            "timerange":    {"start": EVENT_START + "T00:00:00", "end": EVENT_END + "T23:00:00"},
            "model":        "RandomForestRegressor (300 trees, depth 14)",
            "features":     FEATURE_COLS,
        },
        "records": state["predictions"],
    }

    with open(output_path, "w") as fh:
        json.dump(output, fh, indent=2)

    size_kb = os.path.getsize(output_path) / 1024
    return {
        "log": [
            f"[Exporter] Wrote {len(state['predictions']):,} records "
            f"→ {output_path} ({size_kb:.1f} KB)."
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════════════

def build_pipeline():
    """Compile the four-node LangGraph pipeline."""
    g = StateGraph(StormLinesState)

    g.add_node("weather_aggregator",  weather_aggregator_node)
    g.add_node("grid_archivist",      grid_archivist_node)
    g.add_node("predictive_analyst",  predictive_analyst_node)
    g.add_node("data_exporter",       data_exporter_node)

    g.set_entry_point("weather_aggregator")
    g.add_edge("weather_aggregator", "grid_archivist")
    g.add_edge("grid_archivist",     "predictive_analyst")
    g.add_edge("predictive_analyst", "data_exporter")
    g.add_edge("data_exporter",      END)

    return g.compile()
