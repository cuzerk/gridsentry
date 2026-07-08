#!/usr/bin/env python3
"""
Fetch New England power line data from Overpass API and export in the
same format as routes.json and levels.json.

Coverage: CT, RI, MA, NH, VT, ME — split into 4 bounding boxes so each
query stays under Overpass server limits.

Outputs (written to analysis/data/infrastructure/):
  routes_from_api.json  — list of line objects with simplified geometry
  levels_from_api.json  — dict mapping "way/{id}" -> voltage level (1-5)
"""

import json
import sys
import time
from pathlib import Path
import requests
import geopandas as gpd
from shapely.geometry import LineString

# Four overlapping sub-regions that together cover all of New England.
# Overlap of ~0.1° catches lines that cross region boundaries.
BBOXES = {
    "CT + RI":    "40.95,-73.73,42.10,-71.08",
    "MA":         "41.20,-73.55,42.95,-69.80",
    "VT + NH":    "42.65,-73.50,45.35,-70.55",
    "ME":         "43.00,-71.15,47.50,-66.90",
}

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


def fetch_all_regions() -> list:
    """Fetch all regions and deduplicate by OSM element id."""
    seen: dict[int, dict] = {}
    for label, bbox in BBOXES.items():
        elements = fetch_overpass(bbox, label)
        for el in elements:
            seen.setdefault(el["id"], el)
        # Brief pause between requests to be polite to the API
        time.sleep(2)
    print(f"\nTotal unique elements after dedup: {len(seen)}")
    return list(seen.values())


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
    elements = fetch_all_regions()

    features = build_features(elements)
    print(f"Valid line features: {len(features)}")

    simplified = simplify_geometries(features)

    routes, levels = build_outputs(simplified)
    print(f"Lines written: {len(routes)}")

    out_dir = Path(__file__).parent / "analysis" / "data" / "infrastructure"
    out_dir.mkdir(parents=True, exist_ok=True)

    routes_path = out_dir / "routes_from_api.json"
    levels_path = out_dir / "levels_from_api.json"

    with open(routes_path, "w") as f:
        json.dump(routes, f, separators=(",", ":"))
    print(f"Wrote {routes_path}")

    with open(levels_path, "w") as f:
        json.dump(levels, f, separators=(",", ":"))
    print(f"Wrote {levels_path}")


if __name__ == "__main__":
    sys.exit(main())
