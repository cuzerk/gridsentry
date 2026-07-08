#!/usr/bin/env python3
"""
precip_agent.py — Fetch and export hourly precipitation data for StormLines.

Uses the same Open-Meteo historical archive as wind_agent.py so both datasets
share the same grid, bbox, and time range and can be animated together.

Usage:
  python analysis/precip_agent.py \\
    --bbox=-80,36,-68,46 \\
    --start=2021-10-26 \\
    --end=2021-10-29 \\
    [--spacing=0.25] \\
    [--out=frontend/static/data/precip]
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
PRECIP_VARS    = "precipitation,rain,snowfall"


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _fetch_point(lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    resp = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude":   lat,
            "longitude":  lon,
            "start_date": start,
            "end_date":   end,
            "hourly":     PRECIP_VARS,
            "timezone":   "UTC",
        },
        timeout=30,
    )
    resp.raise_for_status()
    h = resp.json()["hourly"]
    return pd.DataFrame({
        "time":          pd.to_datetime(h["time"]),
        "lat":           lat,
        "lon":           lon,
        "precipitation": h["precipitation"],
        "rain":          h["rain"],
        "snowfall":      h["snowfall"],
    })


def fetch_precip_grid(bbox: tuple, start: str, end: str, spacing: float = 0.25) -> pd.DataFrame:
    """Fetch hourly precipitation for a regular lat/lon grid."""
    lon_min, lat_min, lon_max, lat_max = bbox
    lats   = np.arange(lat_min, lat_max + spacing / 2, spacing)
    lons   = np.arange(lon_min, lon_max + spacing / 2, spacing)
    points = [(lat, lon) for lat in lats for lon in lons]

    print(f"Fetching {len(points)} grid points "
          f"({len(lats)} lat × {len(lons)} lon), {start} → {end}")

    frames, done = [], 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(_fetch_point, lat, lon, start, end): (lat, lon)
            for lat, lon in points
        }
        for fut in as_completed(futures):
            lat, lon = futures[fut]
            try:
                frames.append(fut.result())
            except Exception as exc:
                print(f"  WARNING ({lat:.2f}, {lon:.2f}): {exc}")
            done += 1
            if done % 50 == 0 or done == len(points):
                print(f"  {done}/{len(points)} points fetched")

    df = pd.concat(frames, ignore_index=True).fillna(0)
    print(f"  {len(df):,} rows, {df['time'].nunique()} hours")
    return df


# ── Export ────────────────────────────────────────────────────────────────────

def export_precip_frames(df: pd.DataFrame, out_dir: str) -> dict:
    """Write one GeoJSON FeatureCollection per hourly timestamp + manifest.json."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    timestamps = sorted(df["time"].unique())
    frames = []

    for ts in timestamps:
        slice_ = df[df["time"] == ts]
        iso    = pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
        label  = pd.Timestamp(ts).strftime("%-b %-d %H:%M UTC")
        fname  = "precip_" + iso.replace(":", "-") + ".geojson"

        features = [
            {
                "type": "Feature",
                "geometry": {
                    "type":        "Point",
                    "coordinates": [round(float(r.lon), 4), round(float(r.lat), 4)],
                },
                "properties": {
                    "precipitation": round(float(r.precipitation), 2),
                    "rain":          round(float(r.rain), 2),
                    "snowfall":      round(float(r.snowfall), 2),
                },
            }
            for r in slice_.itertuples()
        ]

        fc = {
            "type":       "FeatureCollection",
            "properties": {"timestamp": iso, "label": label},
            "features":   features,
        }
        (out / fname).write_text(json.dumps(fc, separators=(",", ":")))
        frames.append({"file": fname, "timestamp": iso, "label": label})

    manifest = {
        "frames":    frames,
        "generated": datetime.now(timezone.utc).isoformat(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Exported {len(frames)} frames → {out}/")
    return manifest


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="StormLines precipitation data agent")
    p.add_argument("--bbox",    required=True,
                   help="lon_min,lat_min,lon_max,lat_max  e.g. -80,36,-68,46")
    p.add_argument("--start",   required=True, help="Start date YYYY-MM-DD")
    p.add_argument("--end",     required=True, help="End date YYYY-MM-DD")
    p.add_argument("--spacing", type=float, default=0.25, help="Grid spacing ° (default 0.25)")
    p.add_argument(
        "--out",
        default=str(Path(__file__).parent.parent / "frontend" / "static" / "data" / "precip"),
        help="Output dir for GeoJSON frames",
    )
    p.add_argument(
        "--parquet",
        default=str(Path(__file__).parent / "data" / "precip" / "precip_oct2021.parquet"),
        help="Path to save parquet",
    )
    args = p.parse_args()

    bbox = tuple(float(x) for x in args.bbox.split(","))
    if len(bbox) != 4:
        p.error("--bbox must have exactly 4 comma-separated values")

    df = fetch_precip_grid(bbox, args.start, args.end, args.spacing)

    print("Storing parquet…")
    Path(args.parquet).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.parquet, index=False)
    size_kb = Path(args.parquet).stat().st_size // 1024
    print(f"  → {args.parquet} ({size_kb} KB)")

    print("Exporting GeoJSON frames…")
    export_precip_frames(df, args.out)
    print("Done.")


if __name__ == "__main__":
    main()
