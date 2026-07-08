"""
Preprocess EagleI outage data for the Oct 27-29 2021 Nor'easter.
Filters to CT, MA, RI, NH, VT, ME counties and builds a time-indexed
outage timeline plus a county→transmission-line spatial index.

Output: frontend/static/data/outages/storm_oct2021.json
"""

import csv, json, os, math
from datetime import datetime, timedelta
from collections import defaultdict

# ── County bounding boxes (west, south, east, north) ─────────────────────────
# FIPS → {name, state, bbox: [W, S, E, N]}
COUNTY_BOUNDS = {
    # Connecticut
    9001: {"name": "Fairfield",   "state": "Connecticut",    "bbox": [-73.73, 40.95, -73.10, 41.47]},
    9003: {"name": "Hartford",    "state": "Connecticut",    "bbox": [-73.02, 41.67, -72.05, 42.05]},
    9005: {"name": "Litchfield",  "state": "Connecticut",    "bbox": [-73.55, 41.75, -72.94, 42.05]},
    9007: {"name": "Middlesex",   "state": "Connecticut",    "bbox": [-72.78, 41.33, -72.30, 41.68]},
    9009: {"name": "New Haven",   "state": "Connecticut",    "bbox": [-73.30, 41.12, -72.72, 41.67]},
    9011: {"name": "New London",  "state": "Connecticut",    "bbox": [-72.32, 41.22, -71.79, 41.75]},
    9013: {"name": "Tolland",     "state": "Connecticut",    "bbox": [-72.44, 41.85, -72.01, 42.05]},
    9015: {"name": "Windham",     "state": "Connecticut",    "bbox": [-72.17, 41.69, -71.79, 42.05]},
    # Rhode Island
    44001: {"name": "Bristol",    "state": "Rhode Island",   "bbox": [-71.40, 41.60, -71.19, 41.82]},
    44003: {"name": "Kent",       "state": "Rhode Island",   "bbox": [-71.80, 41.53, -71.39, 41.82]},
    44005: {"name": "Newport",    "state": "Rhode Island",   "bbox": [-71.50, 41.31, -71.17, 41.65]},
    44007: {"name": "Providence", "state": "Rhode Island",   "bbox": [-71.93, 41.72, -71.12, 42.02]},
    44009: {"name": "Washington", "state": "Rhode Island",   "bbox": [-71.93, 41.29, -71.35, 41.70]},
    # Massachusetts
    25001: {"name": "Barnstable", "state": "Massachusetts",  "bbox": [-70.65, 41.52, -69.93, 42.08]},
    25003: {"name": "Berkshire",  "state": "Massachusetts",  "bbox": [-73.52, 42.02, -72.85, 42.75]},
    25005: {"name": "Bristol",    "state": "Massachusetts",  "bbox": [-71.30, 41.61, -70.81, 42.04]},
    25007: {"name": "Dukes",      "state": "Massachusetts",  "bbox": [-70.83, 41.23, -70.61, 41.47]},
    25009: {"name": "Essex",      "state": "Massachusetts",  "bbox": [-71.35, 42.48, -70.64, 42.90]},
    25011: {"name": "Franklin",   "state": "Massachusetts",  "bbox": [-73.10, 42.47, -72.28, 42.88]},
    25013: {"name": "Hampden",    "state": "Massachusetts",  "bbox": [-73.01, 42.01, -72.02, 42.36]},
    25015: {"name": "Hampshire",  "state": "Massachusetts",  "bbox": [-72.83, 42.08, -72.14, 42.47]},
    25017: {"name": "Middlesex",  "state": "Massachusetts",  "bbox": [-71.89, 42.22, -71.04, 42.73]},
    25019: {"name": "Nantucket",  "state": "Massachusetts",  "bbox": [-70.24, 41.21, -69.96, 41.41]},
    25021: {"name": "Norfolk",    "state": "Massachusetts",  "bbox": [-71.30, 42.00, -70.77, 42.27]},
    25023: {"name": "Plymouth",   "state": "Massachusetts",  "bbox": [-71.23, 41.76, -70.53, 42.16]},
    25025: {"name": "Suffolk",    "state": "Massachusetts",  "bbox": [-71.19, 42.24, -70.99, 42.40]},
    25027: {"name": "Worcester",  "state": "Massachusetts",  "bbox": [-72.32, 41.97, -71.56, 42.73]},
    # New Hampshire
    33001: {"name": "Belknap",      "state": "New Hampshire",  "bbox": [-71.73, 43.36, -71.22, 43.77]},
    33003: {"name": "Carroll",      "state": "New Hampshire",  "bbox": [-71.58, 43.54, -70.97, 44.30]},
    33005: {"name": "Cheshire",     "state": "New Hampshire",  "bbox": [-72.56, 42.70, -71.93, 43.22]},
    33007: {"name": "Coos",         "state": "New Hampshire",  "bbox": [-71.62, 44.30, -70.96, 45.31]},
    33009: {"name": "Grafton",      "state": "New Hampshire",  "bbox": [-72.05, 43.54, -71.57, 44.40]},
    33011: {"name": "Hillsborough", "state": "New Hampshire",  "bbox": [-72.03, 42.70, -71.36, 43.22]},
    33013: {"name": "Merrimack",    "state": "New Hampshire",  "bbox": [-71.93, 43.03, -71.36, 43.60]},
    33015: {"name": "Rockingham",   "state": "New Hampshire",  "bbox": [-71.46, 42.70, -70.70, 43.25]},
    33017: {"name": "Strafford",    "state": "New Hampshire",  "bbox": [-71.27, 43.10, -70.82, 43.36]},
    33019: {"name": "Sullivan",     "state": "New Hampshire",  "bbox": [-72.44, 43.17, -71.94, 43.56]},
    # Vermont
    50001: {"name": "Addison",      "state": "Vermont",        "bbox": [-73.44, 43.76, -72.50, 44.31]},
    50003: {"name": "Bennington",   "state": "Vermont",        "bbox": [-73.26, 42.73, -72.73, 43.22]},
    50005: {"name": "Caledonia",    "state": "Vermont",        "bbox": [-72.43, 44.12, -71.50, 44.56]},
    50007: {"name": "Chittenden",   "state": "Vermont",        "bbox": [-73.25, 44.31, -72.76, 44.72]},
    50009: {"name": "Essex",        "state": "Vermont",        "bbox": [-71.76, 44.56, -71.46, 45.02]},
    50011: {"name": "Franklin",     "state": "Vermont",        "bbox": [-73.19, 44.72, -72.65, 45.02]},
    50013: {"name": "Grand Isle",   "state": "Vermont",        "bbox": [-73.37, 44.69, -73.17, 45.02]},
    50015: {"name": "Lamoille",     "state": "Vermont",        "bbox": [-72.84, 44.37, -72.28, 44.73]},
    50017: {"name": "Orange",       "state": "Vermont",        "bbox": [-72.53, 43.77, -72.01, 44.37]},
    50019: {"name": "Orleans",      "state": "Vermont",        "bbox": [-72.64, 44.55, -71.86, 45.02]},
    50021: {"name": "Rutland",      "state": "Vermont",        "bbox": [-73.44, 43.21, -72.70, 43.77]},
    50023: {"name": "Washington",   "state": "Vermont",        "bbox": [-72.79, 44.07, -72.28, 44.56]},
    50025: {"name": "Windham",      "state": "Vermont",        "bbox": [-72.84, 42.73, -72.37, 43.25]},
    50027: {"name": "Windsor",      "state": "Vermont",        "bbox": [-72.84, 43.22, -72.25, 43.77]},
    # Maine
    23001: {"name": "Androscoggin", "state": "Maine",          "bbox": [-70.50, 44.00, -70.00, 44.40]},
    23003: {"name": "Aroostook",    "state": "Maine",          "bbox": [-70.83, 46.00, -67.43, 47.46]},
    23005: {"name": "Cumberland",   "state": "Maine",          "bbox": [-70.80, 43.56, -70.06, 44.11]},
    23007: {"name": "Franklin",     "state": "Maine",          "bbox": [-70.83, 44.40, -70.16, 45.15]},
    23009: {"name": "Hancock",      "state": "Maine",          "bbox": [-68.94, 44.30, -67.93, 44.90]},
    23011: {"name": "Kennebec",     "state": "Maine",          "bbox": [-70.13, 44.13, -69.56, 44.70]},
    23013: {"name": "Knox",         "state": "Maine",          "bbox": [-69.55, 43.90, -68.90, 44.30]},
    23015: {"name": "Lincoln",      "state": "Maine",          "bbox": [-69.80, 43.86, -69.46, 44.22]},
    23017: {"name": "Oxford",       "state": "Maine",          "bbox": [-71.08, 43.90, -70.18, 44.80]},
    23019: {"name": "Penobscot",    "state": "Maine",          "bbox": [-69.56, 44.60, -68.00, 45.90]},
    23021: {"name": "Piscataquis",  "state": "Maine",          "bbox": [-70.16, 45.15, -68.48, 46.40]},
    23023: {"name": "Sagadahoc",    "state": "Maine",          "bbox": [-70.08, 43.80, -69.67, 44.10]},
    23025: {"name": "Somerset",     "state": "Maine",          "bbox": [-70.83, 44.64, -69.50, 45.60]},
    23027: {"name": "Waldo",        "state": "Maine",          "bbox": [-69.43, 44.30, -68.80, 44.75]},
    23029: {"name": "Washington",   "state": "Maine",          "bbox": [-67.93, 44.73, -66.93, 45.80]},
    23031: {"name": "York",         "state": "Maine",          "bbox": [-71.08, 43.08, -70.57, 43.60]},
}

MAX_CUSTOMERS = 351_202  # MA peak; used as normalization ceiling by frontend


def point_in_bbox(lon, lat, bbox):
    w, s, e, n = bbox
    return w <= lon <= e and s <= lat <= n


def build_county_lines(routes):
    """Spatial join: find which county each route segment passes through."""
    county_lines = defaultdict(list)  # fips → [(route_id, level)]
    for route in routes:
        path = route["path"]
        step = max(1, len(path) // 12)
        fips_hit = set()
        for i in range(0, len(path), step):
            lon, lat = path[i][0], path[i][1]
            for fips, info in COUNTY_BOUNDS.items():
                if fips not in fips_hit and point_in_bbox(lon, lat, info["bbox"]):
                    county_lines[fips].append({"id": route["id"], "level": route["level"]})
                    fips_hit.add(fips)
        # also check the last point
        lon, lat = path[-1][0], path[-1][1]
        for fips, info in COUNTY_BOUNDS.items():
            if fips not in fips_hit and point_in_bbox(lon, lat, info["bbox"]):
                county_lines[fips].append({"id": route["id"], "level": route["level"]})
                fips_hit.add(fips)
    return county_lines


def build_frames(outages, start_dt, end_dt, interval_minutes=15):
    """Build time-indexed frames of active outages."""
    frames = []
    t = start_dt
    delta = timedelta(minutes=interval_minutes)
    fmt = "%Y-%m-%d %H:%M:%S"
    while t <= end_dt:
        t_str = t.strftime(fmt)
        # aggregate max_customers per county across all concurrent events
        active = {}
        for o in outages:
            if not o["start"] or not o["end"]:
                continue
            if o["start"] <= t_str <= o["end"]:
                fips = o["fips"]
                mc = o["max_customers"]
                if fips not in active or active[fips] < mc:
                    active[fips] = mc
        frames.append({
            "timestamp": t.isoformat(),
            "label": t.strftime("%b %d %H:%M UTC"),
            "counties": [
                {"fips": fips, "customers": int(customers)}
                for fips, customers in active.items()
            ],
        })
        t += delta
    return frames


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(base, "analysis/data/Outage_Dataset/eaglei_outages_with_events_2021.csv")
    routes_path = os.path.join(base, "analysis/data/infrastructure/routes_from_api.json")
    out_dir = os.path.join(base, "frontend/static/data/outages")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "storm_oct2021.json")

    print("Loading routes…")
    with open(routes_path) as f:
        routes = json.load(f)
    print(f"  {len(routes):,} routes loaded")

    print("Building county→line spatial index…")
    county_lines = build_county_lines(routes)
    for fips, lines in county_lines.items():
        # sort by voltage level ascending (lower = less resilient, goes first)
        lines.sort(key=lambda x: x["level"])
    print(f"  {sum(len(v) for v in county_lines.values())} county-line assignments across {len(county_lines)} counties")

    print("Loading outage CSV…")
    target_events = {
        "Connecticut-5",
        "Rhode Island-4",
        "Massachusetts-5",
        "New Hampshire-3",
        "Vermont-4", "Vermont-5",
        "Maine-7",
    }
    outages = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["event_id"] in target_events:
                outages.append({
                    "fips": int(row["fips"]),
                    "state": row["state"],
                    "county": row["county"],
                    "start": row["start_time"],
                    "end": row["end_time"],
                    "max_customers": float(row["max_customers"]) if row["max_customers"] else 0,
                })
    print(f"  {len(outages)} outage intervals across {len(set(o['fips'] for o in outages))} counties")

    # Storm window: Oct 26 18:00 → Oct 31 06:00 UTC
    # Extended to capture VT tail events that run past Oct 30 00:00
    start_dt = datetime(2021, 10, 26, 18, 0)
    end_dt   = datetime(2021, 10, 31,  6, 0)

    print("Generating 15-min frames…")
    frames = build_frames(outages, start_dt, end_dt)
    print(f"  {len(frames)} frames")

    # Check max simultaneous customers across all frames
    max_sim = max(
        (sum(c["customers"] for c in f["counties"]) for f in frames),
        default=0,
    )
    print(f"  Peak simultaneous customers (sum across counties): {max_sim:,}")

    output = {
        "storm": "Oct 2021 Nor'easter",
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "interval_minutes": 15,
        "max_customers": MAX_CUSTOMERS,
        "frames": frames,
        "county_lines": {
            str(fips): [entry["id"] for entry in entries]
            for fips, entries in county_lines.items()
        },
        "county_info": {
            str(fips): {"name": info["name"], "state": info["state"]}
            for fips, info in COUNTY_BOUNDS.items()
        },
    }

    with open(out_path, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    size_kb = os.path.getsize(out_path) // 1024
    print(f"Wrote {out_path} ({size_kb} KB)")


if __name__ == "__main__":
    main()
