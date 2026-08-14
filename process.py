#!/usr/bin/env python3
"""
Turn EMI's monthly solar CSVs into a map-ready GeoJSON.

Runs weekly in GitHub Actions. The expensive part -- working out where
each street physically is -- is cached in road_cache.json and committed
back to the repo, so only genuinely new streets cost anything after the
first run. Weekly updates take minutes; the first run takes an hour or so.

    EMI street CSV ─┐
    EMI region CSV ─┼─> join on (SA2 area + street name) ─> docs/streets.geojson
    OSM road data ──┘

Outputs docs/streets.geojson and docs/meta.json, which GitHub Pages
serves to the map.
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

EMI_BASE = ("https://emidatasets.blob.core.windows.net/publicdata/Datasets/"
            "Retail/SolarInstallations/")
STREET_CSV = EMI_BASE + "SolarInstallationsByStreet.csv"
REGION_CSV = EMI_BASE + "SolarInstallationsByRegion.csv"

SA2_SERVICE = (
    "https://services2.arcgis.com/vKb0s8tBIA3bdocZ/ArcGIS/rest/services/"
    "2023_Census_totals_by_topic_for_families_and_extended_families_by_SA2/"
    "FeatureServer/0/query"
)

OVERPASS_MIRRORS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

ROAD_CACHE = "road_cache.json"      # {sa2: {street: [lng, lat]}} -- committed
SA2_CACHE = "sa2_areas.json"        # {sa2: [name, s, w, n, e]}   -- committed
OUT_GEOJSON = "docs/streets.geojson"
OUT_META = "docs/meta.json"

WORKERS = 3          # parallel Overpass queries -- polite, not greedy
PAUSE = 0.9          # seconds each worker waits between queries
SUPPRESSED = 2       # EMI's "3 or less" counted as this many

session = requests.Session()
session.headers["User-Agent"] = "nz-solar-map/1.0 (github actions; weekly build)"


# ----------------------------------------------------------------------
# Street name matching
# ----------------------------------------------------------------------

ABBREV = {
    "RD": "ROAD", "ST": "STREET", "AVE": "AVENUE", "AV": "AVENUE",
    "DR": "DRIVE", "CRES": "CRESCENT", "PL": "PLACE", "TCE": "TERRACE",
    "TER": "TERRACE", "LN": "LANE", "CT": "COURT", "HWY": "HIGHWAY",
    "GRV": "GROVE", "PDE": "PARADE", "CL": "CLOSE", "BLVD": "BOULEVARD",
    "MT": "MOUNT", "SH": "STATE HIGHWAY",
}


def normalise(name):
    """EMI writes 'QUEEN ST', OSM writes 'Queen Street'.

    Only the final word is expanded, so 'St Andrews Road' keeps its
    saint instead of becoming 'Street Andrews Road'.
    """
    if not name:
        return ""
    words = re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()
    return " ".join(
        ABBREV[w] if (i == len(words) - 1 and w in ABBREV) else w
        for i, w in enumerate(words)
    )


def load(path, default):
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return default


def save(path, obj, indent=None):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=indent)
    os.replace(tmp, path)


# ----------------------------------------------------------------------
# Step 1 - SA2 areas (cached; boundaries only change at census time)
# ----------------------------------------------------------------------

def get_areas():
    cached = load(SA2_CACHE, None)
    if cached:
        print(f"Areas: {len(cached)} (cached)")
        return cached

    print("Fetching SA2 boundaries from Stats NZ...")
    areas, offset = {}, 0
    while True:
        r = session.get(SA2_SERVICE, timeout=180, params={
            "where": "1=1",
            "outFields": "SA22023_V1_00,SA22023_V1_00_NAME",
            "returnGeometry": "true", "maxAllowableOffset": 500,
            "geometryPrecision": 5, "f": "geojson",
            "resultOffset": offset, "resultRecordCount": 2000,
        })
        r.raise_for_status()
        feats = r.json().get("features", [])
        for f in feats:
            if not f.get("geometry"):
                continue
            xs, ys = [], []

            def scan(c):
                if isinstance(c[0], (int, float)):
                    xs.append(c[0]); ys.append(c[1])
                else:
                    for s in c:
                        scan(s)

            scan(f["geometry"]["coordinates"])
            if not xs:
                continue
            areas[str(f["properties"]["SA22023_V1_00"])] = [
                f["properties"]["SA22023_V1_00_NAME"],
                min(ys), min(xs), max(ys), max(xs),
            ]
        if len(feats) < 2000:
            break
        offset += 2000

    save(SA2_CACHE, areas)
    print(f"Areas: {len(areas)}")
    return areas


# ----------------------------------------------------------------------
# Step 2 - EMI data
# ----------------------------------------------------------------------

def icp_value(raw):
    if isinstance(raw, str) and "less" in raw.lower():
        return SUPPRESSED, True
    try:
        return int(float(raw)), False
    except (TypeError, ValueError):
        return 0, False


def fetch_csv(url):
    print(f"Downloading {url.rsplit('/', 1)[-1]}...")
    r = session.get(url, timeout=300)
    r.raise_for_status()
    return r.content.decode("utf-8-sig", errors="replace")


def read_streets(text):
    """One record per (area, street), with the three segments merged."""
    import csv
    import io

    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        code = (row.get("StatisticalArea2Code") or "").strip()
        street = (row.get("PhysicalAddressStreet") or "").strip()
        if not code or not street:
            continue
        key = (code, normalise(street))
        rec = out.setdefault(key, {
            "street": street.title(),
            "area": (row.get("StatisticalArea2Name") or "").strip(),
            "code": code, "icps": 0, "res": 0, "bus": 0,
            "kW": 0.0, "est": False,
        })
        n, suppressed = icp_value(row.get("ICPs"))
        seg = row.get("MarketSegment")
        if seg == "All":
            rec["icps"] = n
            rec["est"] = suppressed
            try:
                rec["kW"] = float(row.get("GenerationCapacityKilowattsSum") or 0)
            except ValueError:
                pass
        elif seg == "Res":
            rec["res"] = n
        elif seg == "Bus":
            rec["bus"] = n
    return out


def read_regions(text):
    """National and regional totals -- handy context for the map header."""
    import csv
    import io

    totals, regions = {}, {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("MarketSegment") != "All":
            continue
        n, _ = icp_value(row.get("ICPs"))
        try:
            kw = float(row.get("GenerationCapacityKilowattsSum") or 0)
        except ValueError:
            kw = 0.0
        if row.get("RegionType") == "NZ":
            totals = {"icps": n, "kW": round(kw, 1)}
        elif row.get("RegionType") == "REG_COUNCIL":
            regions[row.get("Region")] = {"icps": n, "kW": round(kw, 1)}
    return totals, regions


# ----------------------------------------------------------------------
# Step 3 - Geocode the streets we don't already know
# ----------------------------------------------------------------------

def overpass(query):
    """Try each mirror until one answers."""
    last = ""
    for url in OVERPASS_MIRRORS:
        try:
            r = session.post(url, data={"data": query}, timeout=180)
            if r.status_code in (429, 504, 406):
                last = f"{r.status_code}"
                time.sleep(5)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last = str(exc)
    raise RuntimeError(f"all Overpass mirrors failed ({last})")


def roads_in_area(bounds):
    s, w, n, e = bounds
    data = overpass(
        f'[out:json][timeout:120];way["highway"]["name"]({s},{w},{n},{e});'
        "out center tags;"
    )
    sums = {}
    for el in data.get("elements", []):
        name = el.get("tags", {}).get("name")
        centre = el.get("center")
        if not name or not centre:
            continue
        acc = sums.setdefault(normalise(name), [0.0, 0.0, 0])
        acc[0] += centre["lon"]; acc[1] += centre["lat"]; acc[2] += 1
    return {k: [round(v[0] / v[2], 5), round(v[1] / v[2], 5)]
            for k, v in sums.items()}


def geocode(records, areas):
    cache = load(ROAD_CACHE, {})

    # Only visit areas that contain streets we can't already place.
    todo = sorted({
        code for (code, norm) in records
        if code in areas and norm not in cache.get(code, {})
    })
    if not todo:
        print("Geocoding: nothing new")
        return cache

    print(f"Geocoding {len(todo)} areas via OpenStreetMap "
          f"({WORKERS} workers, ~{len(todo) * PAUSE / WORKERS / 60:.0f} min)")

    done = [0]
    failures = [0]

    def work(code):
        if failures[0] > 25:            # something is systemically wrong
            return
        try:
            cache[code] = roads_in_area(areas[code][1:])
        except Exception as exc:        # noqa: BLE001 - keep going, log it
            failures[0] += 1
            print(f"  ! {areas[code][0]}: {exc}")
        done[0] += 1
        if done[0] % 50 == 0:
            print(f"  {done[0]}/{len(todo)} areas")
            save(ROAD_CACHE, cache)
        time.sleep(PAUSE)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(work, todo))

    save(ROAD_CACHE, cache)
    if failures[0]:
        print(f"  {failures[0]} areas failed -- they'll retry next run")
    return cache


# ----------------------------------------------------------------------
# Step 4 - Build the GeoJSON
# ----------------------------------------------------------------------

def build(records, cache, areas, previous):
    features, missing = [], 0

    for (code, norm), rec in records.items():
        coord = cache.get(code, {}).get(norm)
        if not coord:
            missing += 1
            continue
        lng, lat = coord
        # The road must sit inside the area EMI assigned it to. This is
        # what keeps Auckland's Queen Street out of Invercargill.
        if code in areas:
            _, s, w, n, e = areas[code]
            if not (w <= lng <= e and s <= lat <= n):
                missing += 1
                continue

        known = rec["res"] + rec["bus"]
        props = {
            "street": rec["street"],
            "area": rec["area"],
            "icps": rec["icps"],
            "res": rec["res"],
            "bus": rec["bus"],
            "kW": round(rec["kW"], 1),
            "busShare": round(rec["bus"] / known, 2) if known else 0,
        }
        if rec["est"]:
            props["est"] = 1
        # Growth since the last build, so the map can show what's moving.
        was = previous.get(f"{code}|{norm}")
        if was is not None and rec["icps"] != was:
            props["change"] = rec["icps"] - was
        if rec["kW"] and rec["icps"]:
            props["kWper"] = round(rec["kW"] / rec["icps"], 1)

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": props,
        })

    return features, missing


def main():
    areas = get_areas()

    records = read_streets(fetch_csv(STREET_CSV))
    print(f"EMI: {len(records):,} streets")

    try:
        totals, regions = read_regions(fetch_csv(REGION_CSV))
    except Exception as exc:                       # noqa: BLE001
        print(f"Region file unavailable ({exc}) -- continuing without it")
        totals, regions = {}, {}

    cache = geocode(records, areas)

    previous = load("previous_counts.json", {})
    features, missing = build(records, cache, areas, previous)

    save(OUT_GEOJSON, {"type": "FeatureCollection", "features": features})
    save("previous_counts.json",
         {f"{c}|{n}": r["icps"] for (c, n), r in records.items()})

    matched = len(features)
    total = len(records)
    save(OUT_META, {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "streets": matched,
        "streetsTotal": total,
        "matchRate": round(matched / total * 100, 1) if total else 0,
        "national": totals,
        "regions": regions,
    }, indent=1)

    size = os.path.getsize(OUT_GEOJSON) / 1024 / 1024
    print(f"\nPlaced {matched:,} of {total:,} streets "
          f"({matched / total * 100:.1f}%), {missing:,} unmatched")
    print(f"Wrote {OUT_GEOJSON} ({size:.1f} MB)")

    if matched < total * 0.5:
        print("Match rate below 50% -- failing so the bad build isn't published")
        sys.exit(1)


if __name__ == "__main__":
    main()
