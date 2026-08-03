#!/usr/bin/env python3
"""
Fetch power line data for an arbitrary US bbox from the Overpass API and
export in deck.gl-ready format.

Large bboxes are auto-split into overlapping sub-tiles so each Overpass
query stays under the server's size/timeout limits (this replaces the old
hardcoded 4-region New England split with a generic version).

Callable as a library (fetch_lines_for_bbox, cached per area) or as a CLI
for local testing/regeneration of the original New England dataset.

Outputs (written to analysis/data/infrastructure/{area_hash}/):
  routes_from_api.json  — list of line objects with simplified geometry
  levels_from_api.json  — dict mapping "way/{id}" -> voltage level (1-5)
"""

import hashlib
import json
import math
import sys
import time
from pathlib import Path
import requests
import geopandas as gpd
from shapely.geometry import LineString

# The original New England coverage, kept only for the CLI regression check
# in main() — bbox is (west, south, east, north).
NEW_ENGLAND_BBOX = (-73.73, 40.95, -66.90, 47.50)

INFRA_CACHE_ROOT = Path(__file__).parent / "analysis" / "data" / "infrastructure"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

# Voltage (V) → (display level, z-height for deck.gl)
VOLTAGE_LEVELS = [
    (345_000, 5, 400),
    (230_000, 4, 300),
    (138_000, 3, 200),
    (115_000, 2, 100),
    ( 69_000, 1,   0),
]

SIMPLIFY_TOLERANCE = 10  # metres (EPSG:3857)

# Lines to exclude — matched against lowercased name or operator
EXCLUDE_NAME_KEYWORDS = ["sunrise wind", "south fork wind", "revolution wind"]
EXCLUDE_OPERATORS     = {"long island power authority"}


def build_query(bbox: str) -> str:
    return f"""
[out:json][timeout:180][maxsize:536870912];
(
  way["power"="line"]({bbox});
  way["power"="minor_line"]({bbox});
  way["power"="cable"]({bbox});
  way["power"="line"]["voltage"~"^(2|4|13|23|34|41)[0-9]{{3}}$"]({bbox});
);
out geom;
"""


def fetch_overpass(bbox: str, label: str) -> list:
    """Fetch elements for one bbox, trying each endpoint in turn."""
    query = build_query(bbox)
    headers = {"User-Agent": "stormlines/1.0 (power line data pipeline)"}
    for url in OVERPASS_ENDPOINTS:
        print(f"  [{label}] fetching from {url} …", flush=True)
        try:
            resp = requests.post(url, data={"data": query}, headers=headers, timeout=240)
            if resp.status_code == 200:
                elements = resp.json().get("elements", [])
                print(f"  [{label}] {len(elements)} elements received")
                return elements
            print(f"  [{label}] got {resp.status_code}, trying next endpoint …")
        except requests.exceptions.RequestException as e:
            print(f"  [{label}] {type(e).__name__}, trying next endpoint …")
    raise RuntimeError(f"All endpoints failed for region '{label}'.")


def fetch_all_regions(bboxes: dict) -> list:
    """Fetch all regions (label -> 'south,west,north,east' Overpass bbox string)
    and deduplicate by OSM element id."""
    seen: dict[int, dict] = {}
    for label, bbox in bboxes.items():
        elements = fetch_overpass(bbox, label)
        for el in elements:
            seen.setdefault(el["id"], el)
        # Brief pause between requests to be polite to the API
        time.sleep(2)
    print(f"\nTotal unique elements after dedup: {len(seen)}")
    return list(seen.values())


def area_hash(bbox: tuple) -> str:
    """Cache key for a bbox=(west, south, east, north), area-only (no dates) —
    infrastructure doesn't change per storm, only per region."""
    rounded = tuple(round(c, 1) for c in bbox)
    return hashlib.sha256(str(rounded).encode()).hexdigest()[:16]


def split_bbox(bbox: tuple, max_span_deg: float = 3.5) -> dict:
    """Split a (west, south, east, north) bbox into a grid of sub-tiles no
    larger than max_span_deg on a side, each padded ~0.1° so lines crossing
    tile boundaries aren't clipped. Returns {label: 'south,west,north,east'}
    ready for the Overpass query string."""
    west, south, east, north = bbox
    overlap = 0.1

    def edges(lo: float, hi: float) -> list:
        n = max(1, math.ceil((hi - lo) / max_span_deg))
        step = (hi - lo) / n
        return [lo + i * step for i in range(n + 1)]

    lon_edges = edges(west, east)
    lat_edges = edges(south, north)

    tiles = {}
    for i in range(len(lon_edges) - 1):
        for j in range(len(lat_edges) - 1):
            w = lon_edges[i]     - (overlap if i > 0 else 0)
            e = lon_edges[i + 1] + (overlap if i < len(lon_edges) - 2 else 0)
            s = lat_edges[j]     - (overlap if j > 0 else 0)
            n_ = lat_edges[j + 1] + (overlap if j < len(lat_edges) - 2 else 0)
            tiles[f"tile_{i}_{j}"] = f"{s},{w},{n_},{e}"
    return tiles


def fetch_lines_for_bbox(
    bbox: tuple,
    cache_root: Path = INFRA_CACHE_ROOT,
    max_span_deg: float = 3.5,
    force: bool = False,
) -> tuple[list, dict]:
    """Fetch (or load from area cache) transmission line routes/levels for
    bbox=(west, south, east, north). Returns (routes, levels)."""
    out_dir = cache_root / area_hash(bbox)
    routes_path = out_dir / "routes_from_api.json"
    levels_path = out_dir / "levels_from_api.json"

    if not force and routes_path.exists() and levels_path.exists():
        with open(routes_path) as f:
            routes = json.load(f)
        with open(levels_path) as f:
            levels = json.load(f)
        return routes, levels

    tiles = split_bbox(bbox, max_span_deg)
    elements = fetch_all_regions(tiles)
    features = build_features(elements)
    simplified = simplify_geometries(features)
    routes, levels = build_outputs(simplified)

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(routes_path, "w") as f:
        json.dump(routes, f, separators=(",", ":"))
    with open(levels_path, "w") as f:
        json.dump(levels, f, separators=(",", ":"))

    return routes, levels


def parse_voltage(raw) -> int | None:
    """Return the highest numeric voltage from a tag like '345000;115000'."""
    if not raw or not isinstance(raw, str):
        return None
    nums = []
    for p in raw.replace(";", " ").replace(",", " ").split():
        try:
            nums.append(int(p))
        except ValueError:
            pass
    return max(nums) if nums else None


def voltage_to_level(volts: int | None, power_type: str) -> tuple[int, int]:
    """
    Return (level, z) for a way.
    Minor lines and cables without a voltage tag default to level 1.
    Transmission lines (power=line) without voltage are skipped (returns None).
    """
    if volts is not None:
        for threshold, level, z in VOLTAGE_LEVELS:
            if volts >= threshold:
                return level, z
        return 1, 0  # below 69 kV → lowest tier

    # No voltage tag
    if power_type in ("minor_line", "cable"):
        return 1, 0
    return None  # power=line with no voltage — skip


def build_features(elements: list) -> list[dict]:
    features = []
    for el in elements:
        if "geometry" not in el:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
        if len(coords) < 2:
            continue
        tags = el.get("tags", {})
        features.append(
            {
                "id": el["id"],
                "geometry": LineString(coords),
                "voltage_raw": tags.get("voltage"),
                "number": tags.get("ref") or tags.get("ref:line") or "",
                "name": tags.get("name") or "",
                "operator": tags.get("operator") or "",
                "power": tags.get("power", "line"),
            }
        )
    return features


def simplify_geometries(features: list[dict]) -> list[dict]:
    gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
    gdf_m = gdf.to_crs(epsg=3857)
    gdf_m["geometry"] = gdf_m["geometry"].simplify(
        tolerance=SIMPLIFY_TOLERANCE, preserve_topology=True
    )
    return gdf_m.to_crs(epsg=4326).to_dict("records")


def build_outputs(simplified: list[dict]) -> tuple[list, dict]:
    routes = []
    levels = {}

    for row in simplified:
        osm_id = f"way/{row['id']}"
        volts = parse_voltage(row["voltage_raw"])
        lv_z = voltage_to_level(volts, row["power"])
        if lv_z is None:
            continue

        level, z = lv_z
        geom = row["geometry"]
        if geom is None or geom.is_empty:
            continue

        path = [[round(x, 4), round(y, 4), z] for x, y in geom.coords]
        routes.append(
            {
                "id": osm_id,
                "level": level,
                "number": str(row["number"]),
                "name": str(row["name"]),
                "voltage": str(volts) if volts else "",
                "operator": str(row["operator"]),
                "path": path,
            }
        )
        levels[osm_id] = level

    return routes, levels


def main() -> None:
    """CLI regeneration of the original New England dataset, plus the
    top-level routes_from_api.json/levels_from_api.json the current frontend
    still reads directly (kept in sync with the area cache)."""
    routes, levels = fetch_lines_for_bbox(NEW_ENGLAND_BBOX, force="--force" in sys.argv)
    print(f"Lines written: {len(routes)}")

    top_level_dir = Path(__file__).parent / "analysis" / "data" / "infrastructure"
    with open(top_level_dir / "routes_from_api.json", "w") as f:
        json.dump(routes, f, separators=(",", ":"))
    with open(top_level_dir / "levels_from_api.json", "w") as f:
        json.dump(levels, f, separators=(",", ":"))
    print(f"Wrote {top_level_dir / 'routes_from_api.json'}")
    print(f"Wrote {top_level_dir / 'levels_from_api.json'}")


if __name__ == "__main__":
    sys.exit(main())
