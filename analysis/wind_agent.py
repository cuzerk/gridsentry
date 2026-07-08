#!/usr/bin/env python3
"""
wind_agent.py — Fetch, clean, store, and export wind data for StormLines.

Usage:
  python wind_agent.py \\
    --bbox=-75,40.5,-68,45.5 \\
    --start=2019-10-14 \\
    --end=2019-10-18 \\
    [--spacing=0.5] \\
    [--out=../frontend/static/data/wind] \\
    [--render=2019-10-16T15:00:00]

Tools (callable independently):
  fetch_wind_grid  — query Open-Meteo historical API over a lat/lon grid
  clean_wind       — validate, gap-fill, compute U/V components
  store_wind       — save to parquet
  export_geojson_frames — write per-hour GeoJSON + manifest.json for the frontend
  render_quiver    — matplotlib static wind quiver plot for a single timestamp
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
WIND_VARS = "wind_speed_10m,wind_direction_10m,wind_gusts_10m"


# ── Tool: fetch_wind_grid ─────────────────────────────────────────────────────

def _fetch_point(lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    resp = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude":        lat,
            "longitude":       lon,
            "start_date":      start,
            "end_date":        end,
            "hourly":          WIND_VARS,
            "wind_speed_unit": "ms",
            "timezone":        "UTC",
        },
        timeout=30,
    )
    resp.raise_for_status()
    h = resp.json()["hourly"]
    return pd.DataFrame({
        "time":      pd.to_datetime(h["time"]),
        "lat":       lat,
        "lon":       lon,
        "speed":     h["wind_speed_10m"],
        "direction": h["wind_direction_10m"],
        "gust":      h["wind_gusts_10m"],
    })


def fetch_wind_grid(
    bbox: tuple,
    start: str,
    end: str,
    spacing: float = 0.5,
) -> pd.DataFrame:
    """
    Fetch hourly 10 m wind data for a regular grid over bbox.
    bbox = (lon_min, lat_min, lon_max, lat_max)
    Returns a DataFrame with columns: time, lat, lon, speed, direction, gust.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    lats = np.arange(lat_min, lat_max + spacing / 2, spacing)
    lons = np.arange(lon_min, lon_max + spacing / 2, spacing)
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
            if done % 20 == 0 or done == len(points):
                print(f"  {done}/{len(points)} points fetched")

    return pd.concat(frames, ignore_index=True)


# ── Tool: clean_wind ──────────────────────────────────────────────────────────

def clean_wind(df: pd.DataFrame) -> pd.DataFrame:
    """
    Validate, forward-fill short gaps (≤2 h), and compute U/V vector components.
    U = eastward, V = northward (meteorological convention: direction = FROM).
    """
    df = df.copy()
    df = df[df["speed"].notna() & df["direction"].notna() & (df["speed"] >= 0)]
    df = df.sort_values(["lat", "lon", "time"])

    for col in ("speed", "direction", "gust"):
        df[col] = (
            df.groupby(["lat", "lon"])[col]
            .transform(lambda s: s.ffill(limit=2))
        )

    rad = np.deg2rad(df["direction"])
    df["u"] = -df["speed"] * np.sin(rad)   # eastward component
    df["v"] = -df["speed"] * np.cos(rad)   # northward component

    return df.dropna(subset=["speed"]).reset_index(drop=True)


# ── Tool: interpolate_grid ─────────────────────────────────────────────────────

def interpolate_grid(df: pd.DataFrame, factor: int = 4) -> pd.DataFrame:
    """
    Bilinearly upsample the wind grid by `factor` so the heatmap shows wind
    strength continuously, not just at the sparse API query points.

    Interpolates U/V vector components (not raw speed/direction — direction
    wraps at 360° and can't be linearly blended) then recomputes speed and
    direction from the interpolated vectors.

    Tags each output row with kind='obs' (an original API grid point, exactly
    preserved) or kind='interp' (a synthesized in-between point), so the
    frontend can render arrows only at 'obs' points while the heatmap uses all.
    """
    from scipy.interpolate import RegularGridInterpolator

    if factor <= 1:
        df = df.copy()
        df["kind"] = "obs"
        return df

    lats = np.sort(df["lat"].unique())
    lons = np.sort(df["lon"].unique())
    fine_lats = np.linspace(lats[0], lats[-1], (len(lats) - 1) * factor + 1)
    fine_lons = np.linspace(lons[0], lons[-1], (len(lons) - 1) * factor + 1)
    obs_lat_idx = set(range(0, len(fine_lats), factor))
    obs_lon_idx = set(range(0, len(fine_lons), factor))

    mesh_lat, mesh_lon = np.meshgrid(fine_lats, fine_lons, indexing="ij")
    kind_grid = np.array([
        ["obs" if (i in obs_lat_idx and j in obs_lon_idx) else "interp"
         for j in range(len(fine_lons))]
        for i in range(len(fine_lats))
    ])

    rows = []
    for ts, slice_ in df.groupby("time"):
        u_grid    = slice_.pivot(index="lat", columns="lon", values="u").reindex(index=lats, columns=lons).values
        v_grid    = slice_.pivot(index="lat", columns="lon", values="v").reindex(index=lats, columns=lons).values
        gust_grid = slice_.pivot(index="lat", columns="lon", values="gust").reindex(index=lats, columns=lons).values

        u_interp    = RegularGridInterpolator((lats, lons), u_grid, method="linear")
        v_interp    = RegularGridInterpolator((lats, lons), v_grid, method="linear")
        gust_interp = RegularGridInterpolator((lats, lons), gust_grid, method="linear")

        pts = np.column_stack([mesh_lat.ravel(), mesh_lon.ravel()])
        u_fine    = u_interp(pts).reshape(mesh_lat.shape)
        v_fine    = v_interp(pts).reshape(mesh_lat.shape)
        gust_fine = gust_interp(pts).reshape(mesh_lat.shape)

        speed_fine = np.hypot(u_fine, v_fine)
        dir_fine   = np.degrees(np.arctan2(-u_fine, -v_fine)) % 360

        rows.append(pd.DataFrame({
            "time":      ts,
            "lat":       mesh_lat.ravel(),
            "lon":       mesh_lon.ravel(),
            "speed":     speed_fine.ravel(),
            "direction": dir_fine.ravel(),
            "gust":      gust_fine.ravel(),
            "u":         u_fine.ravel(),
            "v":         v_fine.ravel(),
            "kind":      kind_grid.ravel(),
        }))

    out = pd.concat(rows, ignore_index=True)
    print(f"Interpolated {len(lats)}×{len(lons)} grid → {len(fine_lats)}×{len(fine_lons)} "
          f"(factor {factor}x, {len(out):,} rows)")
    return out


# ── Tool: store_wind ──────────────────────────────────────────────────────────

def store_wind(df: pd.DataFrame, path: str) -> str:
    """Save cleaned wind DataFrame to parquet."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(str(out), index=False)
    size_kb = out.stat().st_size // 1024
    print(f"Stored {len(df):,} rows → {out} ({size_kb} KB)")
    return str(out)


# ── Tool: export_geojson_frames ───────────────────────────────────────────────

def export_geojson_frames(df: pd.DataFrame, out_dir: str) -> dict:
    """
    Write one GeoJSON FeatureCollection per hourly timestamp plus a manifest.json.
    Each feature is a grid point with properties: speed, direction, gust, u, v.
    Returns the manifest dict.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    timestamps = sorted(df["time"].unique())
    frames = []

    for ts in timestamps:
        slice_ = df[df["time"] == ts]
        iso = pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
        label = pd.Timestamp(ts).strftime("%-b %-d %H:%M UTC")
        fname = "wind_" + iso.replace(":", "-") + ".geojson"

        features = [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(float(r.lon), 4), round(float(r.lat), 4)],
                },
                "properties": {
                    "speed":     round(float(r.speed), 2),
                    "direction": round(float(r.direction), 1),
                    "gust":      round(float(r.gust), 2) if pd.notna(r.gust) else None,
                    "u":         round(float(r.u), 3),
                    "v":         round(float(r.v), 3),
                    "kind":      getattr(r, "kind", "obs"),
                },
            }
            for r in slice_.itertuples()
        ]

        fc = {
            "type": "FeatureCollection",
            "properties": {"timestamp": iso, "label": label},
            "features": features,
        }
        (out / fname).write_text(json.dumps(fc, separators=(",", ":")))
        frames.append({"file": fname, "timestamp": iso, "label": label})

    manifest = {
        "frames":    frames,
        "generated": datetime.now(timezone.utc).isoformat(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"Exported {len(frames)} frames → {out}/")
    print(f"Manifest → {out}/manifest.json")
    return manifest


# ── Tool: render_quiver ───────────────────────────────────────────────────────

def render_quiver(df: pd.DataFrame, timestamp: str, out_path: str) -> str:
    """
    Render a matplotlib wind quiver plot for a single timestamp.
    Arrows colored by speed; background heatmap shows speed magnitude.
    """
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    ts = pd.Timestamp(timestamp)
    slice_ = df[df["time"] == ts]
    if slice_.empty:
        raise ValueError(f"No data for {timestamp}. "
                         f"Available: {sorted(df['time'].unique())[:5]} …")

    pivot_u = slice_.pivot(index="lat", columns="lon", values="u")
    pivot_v = slice_.pivot(index="lat", columns="lon", values="v")
    pivot_s = slice_.pivot(index="lat", columns="lon", values="speed")

    lons = pivot_u.columns.values
    lats = pivot_u.index.values
    U, V, S = pivot_u.values, pivot_v.values, pivot_s.values

    vmax = max(float(S.max()), 5.0)
    cmap = plt.cm.cool
    norm = mcolors.Normalize(0, vmax)

    fig, ax = plt.subplots(figsize=(13, 8), facecolor="#0d1b2a")
    ax.set_facecolor("#0d1b2a")

    # Speed background
    pcm = ax.pcolormesh(
        lons, lats, S,
        cmap=cmap, norm=norm,
        shading="nearest", alpha=0.35,
    )

    # Wind arrows (quiver)
    q = ax.quiver(
        lons, lats, U, V, S.flatten(),
        cmap=cmap, norm=norm,
        scale=None, scale_units="inches",
        units="width", width=0.004,
        headwidth=4, headlength=5,
        pivot="tail",
    )

    cb = fig.colorbar(q, ax=ax, fraction=0.025, pad=0.01)
    cb.set_label("Wind speed (m/s)", color="#94a3b8")
    cb.ax.yaxis.set_tick_params(color="#94a3b8")
    plt.setp(cb.ax.yaxis.get_ticklabels(), color="#94a3b8")

    ax.set_title(
        ts.strftime("10 m Wind — %b %-d, %Y  %H:%M UTC"),
        color="#f8fafc", fontsize=14, pad=12,
    )
    ax.tick_params(colors="#64748b")
    for spine in ax.spines.values():
        spine.set_color("#334155")
    ax.set_xlabel("Longitude", color="#94a3b8")
    ax.set_ylabel("Latitude", color="#94a3b8")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"Quiver plot → {out_path}")
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="StormLines wind data agent")
    p.add_argument(
        "--bbox", required=True,
        help="lon_min,lat_min,lon_max,lat_max  e.g. -75,40.5,-68,45.5",
    )
    p.add_argument("--start",   required=True, help="Start date YYYY-MM-DD")
    p.add_argument("--end",     required=True, help="End date YYYY-MM-DD")
    p.add_argument("--spacing", type=float, default=0.5, help="Grid spacing °  (default 0.5)")
    p.add_argument(
        "--out",
        default=str(Path(__file__).parent.parent / "frontend" / "static" / "data" / "wind"),
        help="Output dir for GeoJSON frames (default: ../frontend/static/data/wind)",
    )
    p.add_argument(
        "--parquet",
        default=str(Path(__file__).parent / "data" / "wind" / "wind_data.parquet"),
        help="Path to save cleaned parquet (default: analysis/data/wind/wind_data.parquet)",
    )
    p.add_argument(
        "--render", default=None,
        help="Also save a quiver PNG for this ISO timestamp, e.g. 2019-10-16T15:00:00",
    )
    p.add_argument(
        "--upsample", type=int, default=4,
        help="Bilinear upsampling factor for heatmap coverage between grid "
             "points (default 4; use 1 to disable)",
    )
    args = p.parse_args()

    bbox = tuple(float(x) for x in args.bbox.split(","))
    if len(bbox) != 4:
        p.error("--bbox must have exactly 4 comma-separated values")

    # 1 — Fetch
    raw = fetch_wind_grid(bbox, args.start, args.end, args.spacing)

    # 2 — Clean
    print("\nCleaning…")
    df = clean_wind(raw)
    print(f"  {len(df):,} valid observations across {df['time'].nunique()} hours")

    # 3 — Store (original-resolution data, not the upsampled visualization grid)
    print("\nStoring…")
    store_wind(df, args.parquet)

    # 4 — Upsample for continuous heatmap coverage, then export GeoJSON frames
    print("\nInterpolating…")
    dense = interpolate_grid(df, args.upsample)

    print("\nExporting frames…")
    export_geojson_frames(dense, args.out)

    # 5 — Optional static quiver
    if args.render:
        print(f"\nRendering quiver for {args.render}…")
        png = str(Path(args.parquet).parent / f"quiver_{args.render[:10]}.png")
        render_quiver(df, args.render, png)

    print("\nDone.")


if __name__ == "__main__":
    main()
