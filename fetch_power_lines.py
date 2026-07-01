#!/usr/bin/env python3
"""
Fetch New England power line data from Overpass API and export in the
same format as routes.json and levels.json.

Outputs:
  routes_from_api.json  — list of line objects with simplified geometry
  levels_from_api.json  — dict mapping "way/{id}" -> voltage level (1-5)
"""

import json
import sys
from pathlib import Path
import requests
import geopandas as gpd
from shapely.geometry import LineString

# Rhode Island, Massachusetts, and Connecticut bounding box
# (south_lat, west_lon, north_lat, east_lon)
BBOX = "40.95,-73.73,42.89,-69.86"

# Primary endpoint + mirror fallbacks
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

# Voltage (V) to display level and z-height used by deck.gl
VOLTAGE_LEVELS = [
    (345_000, 5, 400),
    (230_000, 4, 300),
    (138_000, 3, 200),
    (115_000, 2, 100),
    (  69_000, 1,   0),
]

# Simplification tolerance in meters (Web Mercator / EPSG:3857).
# 10 m keeps near-lossless detail; raise to 25-50 for smaller files.
SIMPLIFY_TOLERANCE = 10


def fetch_overpass(bbox: str) -> dict:
    query = f"""
[out:json][timeout:180][maxsize:536870912];
(
  way["power"="line"]({bbox});
  way["power"="minor_line"]({bbox});
);
out geom;
"""
    headers = {"User-Agent": "gridsentry/1.0 (power line data pipeline)"}
    for url in OVERPASS_ENDPOINTS:
        print(f"Fetching from {url} …", flush=True)
        try:
            resp = requests.post(url, data={"data": query}, headers=headers, timeout=240)
            if resp.status_code == 200:
                return resp.json()
            print(f"  Got {resp.status_code}, trying next endpoint …")
        except requests.exceptions.RequestException as e:
            print(f"  {type(e).__name__}, trying next endpoint …")
    raise RuntimeError("All Overpass endpoints failed or timed out.")


def parse_voltage(raw) -> int | None:
    """Return the highest numeric voltage from a tag like '345000;115000'."""
    if not raw or not isinstance(raw, str):
        return None
    parts = raw.replace(";", " ").replace(",", " ").split()
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            pass
    return max(nums) if nums else None


def voltage_to_level(volts: int | None) -> tuple[int, int] | None:
    """Return (level, z) for a voltage value, or None if unrecognised."""
    if volts is None:
        return None
    for threshold, level, z in VOLTAGE_LEVELS:
        if volts >= threshold:
            return level, z
    # Anything below 69 kV — treat as level 1
    return 1, 0


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
                "power": tags.get("power", ""),
            }
        )
    return features


def simplify_geometries(features: list[dict]) -> list[dict]:
    gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
    gdf_m = gdf.to_crs(epsg=3857)
    gdf_m["geometry"] = gdf_m["geometry"].simplify(
        tolerance=SIMPLIFY_TOLERANCE, preserve_topology=True
    )
    gdf_wgs84 = gdf_m.to_crs(epsg=4326)
    return gdf_wgs84.to_dict("records")


def build_outputs(simplified: list[dict]) -> tuple[list, dict]:
    routes = []
    levels = {}

    for row in simplified:
        osm_id = f"way/{row['id']}"
        volts = parse_voltage(row["voltage_raw"])
        lv_z = voltage_to_level(volts)
        if lv_z is None:
            continue  # skip lines with no recognisable voltage

        level, z = lv_z

        # Extract coordinate list; round to 4 decimal places (~11 m precision)
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
                "voltage": str(volts),
                "operator": str(row["operator"]),
                "path": path,
            }
        )
        levels[osm_id] = level

    return routes, levels


def main() -> None:
    data = fetch_overpass(BBOX)
    elements = data.get("elements", [])
    print(f"Elements received: {len(elements)}")

    features = build_features(elements)
    print(f"Valid line features: {len(features)}")

    simplified = simplify_geometries(features)

    routes, levels = build_outputs(simplified)
    print(f"Lines with recognised voltage: {len(routes)}")

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
