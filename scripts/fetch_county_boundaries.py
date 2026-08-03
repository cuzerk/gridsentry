#!/usr/bin/env python3
"""
One-time setup script: download US Census cartographic county boundaries
and simplify them into a small FIPS-keyed geometry file used by
process_outages.py to resolve which counties intersect an arbitrary bbox.

Usage:
    python scripts/fetch_county_boundaries.py

Output:
    analysis/data/boundaries/us_counties.json
"""

import io
import json
import zipfile
from pathlib import Path

import geopandas as gpd
import requests

CENSUS_URL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_county_20m.zip"
# 2020 vintage, not 2022: Connecticut's 2022 boundary release replaced its 8
# legacy counties with 9 new "planning region" FIPS codes (09110-09190), but
# the EagleI outage CSVs (and every other county FIPS reference in this repo)
# still use the pre-2022 county codes (09001, 09003, ...). The 2020 file keeps
# those.
SIMPLIFY_TOLERANCE = 0.005  # degrees (~500 m) — plenty for bbox-intersection lookups

OUT_PATH = Path(__file__).parent.parent / "analysis" / "data" / "boundaries" / "us_counties.json"


def main() -> None:
    print(f"Downloading {CENSUS_URL} …")
    resp = requests.get(CENSUS_URL, timeout=60)
    resp.raise_for_status()

    print("Reading shapefile …")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        shp_name = next(n for n in zf.namelist() if n.endswith(".shp"))
        zf.extractall("/tmp/us_counties_shp")
        gdf = gpd.read_file(f"/tmp/us_counties_shp/{shp_name}")

    print(f"  {len(gdf)} counties loaded")

    gdf["geometry"] = gdf["geometry"].simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)

    counties = {}
    for _, row in gdf.iterrows():
        fips = int(row["GEOID"])
        minx, miny, maxx, maxy = row["geometry"].bounds
        counties[str(fips)] = {
            "name": row["NAME"],
            "state_fips": row["STATEFP"],
            "bbox": [round(minx, 4), round(miny, 4), round(maxx, 4), round(maxy, 4)],
            "geometry": json.loads(gpd.GeoSeries([row["geometry"]]).to_json())["features"][0]["geometry"],
        }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as fh:
        json.dump(counties, fh, separators=(",", ":"))

    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUT_PATH} ({len(counties)} counties, {size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
