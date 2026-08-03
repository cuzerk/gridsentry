"""
Build a time-indexed county-level outage timeline for an arbitrary US bbox
and date range, from the EagleI outage dataset (analysis/data/Outage_Dataset/
eaglei_outages_with_events_{2014..2023}.csv — real county-level outage data
covering the whole US).

Callable as a library (build_outage_timeline, estimate_coverage) or as a CLI
for local testing/regeneration.

Output: {out_dir}/outages.json
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, box, shape

BASE = Path(__file__).parent
BOUNDARIES_PATH = BASE / "analysis" / "data" / "boundaries" / "us_counties.json"
OUTAGE_DIR = BASE / "analysis" / "data" / "Outage_Dataset"
ROUTES_PATH = BASE / "analysis" / "data" / "infrastructure" / "routes_from_api.json"

CSV_TIME_FMT = "%Y-%m-%d %H:%M:%S"
HISTORICAL_YEARS = range(2014, 2024)  # inclusive 2014-2023, matches CSVs on disk


def load_county_boundaries() -> dict:
    with open(BOUNDARIES_PATH) as f:
        return json.load(f)


def counties_in_bbox(bbox: tuple, boundaries: dict) -> dict:
    """Return {fips: county_info} for counties whose geometry intersects bbox (W, S, E, N)."""
    w, s, e, n = bbox
    qbox = box(w, s, e, n)
    result = {}
    for fips, info in boundaries.items():
        cw, cs, ce, cn = info["bbox"]
        if ce < w or cw > e or cn < s or cs > n:
            continue  # cheap bbox-bbox reject before the precise geometry check
        if shape(info["geometry"]).intersects(qbox):
            result[fips] = info
    return result


def build_county_lines(routes: list, counties: dict) -> dict:
    """Spatial join: which counties (restricted to `counties`) each route passes through."""
    if not routes or not counties:
        return {}

    fips_list = list(counties.keys())
    county_gdf = gpd.GeoDataFrame(
        {"fips": fips_list},
        geometry=[shape(counties[f]["geometry"]) for f in fips_list],
        crs="EPSG:4326",
    )

    points, route_idx = [], []
    for ridx, route in enumerate(routes):
        path = route["path"]
        step = max(1, len(path) // 12)
        indices = set(range(0, len(path), step))
        indices.add(len(path) - 1)
        for i in indices:
            lon, lat = path[i][0], path[i][1]
            points.append(Point(lon, lat))
            route_idx.append(ridx)

    points_gdf = gpd.GeoDataFrame({"route_idx": route_idx}, geometry=points, crs="EPSG:4326")
    joined = gpd.sjoin(points_gdf, county_gdf, predicate="within", how="inner")

    county_lines = defaultdict(list)
    seen = set()
    for ridx, fips in zip(joined["route_idx"], joined["fips"]):
        key = (ridx, fips)
        if key in seen:
            continue
        seen.add(key)
        route = routes[ridx]
        county_lines[fips].append({"id": route["id"], "level": route["level"]})
    return county_lines


def _years_available(start_dt: datetime, end_dt: datetime) -> list[int]:
    return [
        y for y in range(start_dt.year, end_dt.year + 1)
        if y in HISTORICAL_YEARS and (OUTAGE_DIR / f"eaglei_outages_with_events_{y}.csv").exists()
    ]


def load_outage_events(start_dt: datetime, end_dt: datetime, fips_set: set) -> list:
    """Load and filter outage intervals overlapping [start_dt, end_dt] for the given FIPS codes."""
    outages = []
    for year in _years_available(start_dt, end_dt):
        csv_path = OUTAGE_DIR / f"eaglei_outages_with_events_{year}.csv"
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                if not row["start_time"] or not row["end_time"]:
                    continue
                fips = int(row["fips"])
                if fips not in fips_set:
                    continue
                row_start = datetime.strptime(row["start_time"], CSV_TIME_FMT)
                row_end = datetime.strptime(row["end_time"], CSV_TIME_FMT)
                if row_end < start_dt or row_start > end_dt:
                    continue
                outages.append({
                    "fips": fips,
                    "state": row["state"],
                    "county": row["county"],
                    "start": row["start_time"],
                    "end": row["end_time"],
                    "max_customers": float(row["max_customers"]) if row["max_customers"] else 0,
                })
    return outages


def build_frames(outages: list, start_dt: datetime, end_dt: datetime, interval_minutes: int = 15) -> list:
    """Build 15-min-interval frames of active outages per county."""
    frames = []
    t = start_dt
    delta = timedelta(minutes=interval_minutes)
    while t <= end_dt:
        t_str = t.strftime(CSV_TIME_FMT)
        active = {}
        for o in outages:
            if o["start"] <= t_str <= o["end"]:
                fips, mc = o["fips"], o["max_customers"]
                if fips not in active or active[fips] < mc:
                    active[fips] = mc
        frames.append({
            "timestamp": t.isoformat(),
            "label": t.strftime("%b %d %H:%M UTC"),
            "counties": [{"fips": fips, "customers": int(c)} for fips, c in active.items()],
        })
        t += delta
    return frames


def _load_relevant(bbox: tuple, start_dt: datetime, end_dt: datetime) -> tuple:
    counties = counties_in_bbox(bbox, load_county_boundaries())
    fips_set = {int(f) for f in counties}
    outages = load_outage_events(start_dt, end_dt, fips_set) if fips_set else []
    return counties, outages


def estimate_coverage(bbox: tuple, start_dt: datetime, end_dt: datetime) -> dict:
    """Cheap pre-check used by the orchestrator to decide ground-truth vs. fallback."""
    counties, outages = _load_relevant(bbox, start_dt, end_dt)
    years_requested = list(range(start_dt.year, end_dt.year + 1))
    years_covered = _years_available(start_dt, end_dt)
    return {
        "has_data": bool(years_covered) and len(outages) > 0,
        "county_count": len(counties),
        "event_count": len(outages),
        "years_requested": years_requested,
        "years_covered": years_covered,
    }


def build_outage_timeline(
    bbox: tuple,
    start_dt: datetime,
    end_dt: datetime,
    routes: list,
    out_dir: Path,
    storm_label: str = "",
    interval_minutes: int = 15,
) -> dict:
    """Build outages.json for one bbox+date-range request. Returns the output dict."""
    counties, outages = _load_relevant(bbox, start_dt, end_dt)

    county_lines = build_county_lines(routes, counties)
    for lines in county_lines.values():
        lines.sort(key=lambda x: x["level"])

    frames = build_frames(outages, start_dt, end_dt, interval_minutes)
    max_customers = max((o["max_customers"] for o in outages), default=1.0)

    output = {
        "storm": storm_label or f"{start_dt.date()} to {end_dt.date()}",
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "interval_minutes": interval_minutes,
        "max_customers": max_customers,
        "frames": frames,
        "county_lines": {
            str(fips): [entry["id"] for entry in entries]
            for fips, entries in county_lines.items()
        },
        "county_info": {
            str(fips): {"name": info["name"], "state": info.get("state", "")}
            for fips, info in counties.items()
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "outages.json"
    with open(out_path, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    return output


def main() -> None:
    """CLI regression check: rebuild the original Oct 2021 New England storm."""
    with open(ROUTES_PATH) as f:
        routes = json.load(f)

    start_dt = datetime(2021, 10, 26, 18, 0)
    end_dt = datetime(2021, 10, 31, 6, 0)
    # Original hardcoded bbox covering CT/RI/MA/NH/VT/ME
    bbox = (-73.73, 40.95, -66.90, 47.50)

    out_dir = BASE / "frontend" / "static" / "data" / "outages"
    output = build_outage_timeline(bbox, start_dt, end_dt, routes, out_dir, storm_label="Oct 2021 Nor'easter")

    print(f"{len(output['frames'])} frames, {len(output['county_lines'])} counties with lines")
    print(f"Wrote {out_dir / 'outages.json'}")


if __name__ == "__main__":
    main()
