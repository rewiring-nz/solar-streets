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

# Total ICPs (not just solar) by network reporting region -- lets us show
# "% of connections with solar", not just raw counts. The file is
# date-stamped and moves monthly, so we scrape the current link off the
# page rather than hardcoding a URL.
ICP_TOTALS_PAGE = "https://www.emi.ea.govt.nz/Retail/Datasets/MarketStructure/ICPandMeteringDetails"
ICP_TOTALS_LINK_RE = re.compile(r'href="([^"]*\d{8}_MarketShareByMEPandTrader\.csv)"')

# EA's own network-region boundaries -- real polygons, same 39 regions,
# used only to answer "is this region in the current map view". Static
# reference data (distributor footprints barely change), so it's cached
# like the SA2 boundaries and fetched once.
NETWORK_BOUNDARIES_ZIP = ("https://www.emi.ea.govt.nz/Wholesale/Datasets/"
                           "MappingsAndGeospatial/NetworkRegionShapefiles/"
                           "WGS84/GeoJSON/WGS84_GeoJSON_NRR.zip")
NETWORK_BOUNDS_CACHE = "network_bounds.json"

# The boundary file uses slightly older/unmacronned names for a few
# regions -- these are the same real networks, just spelled differently.
NETWORK_BOUNDARY_ALIASES = {
    "Otago (OtagoNet JV)": "Otago (OtagoNet)",
    "Kapiti and Horowhenua (Electra)": "Kāpiti and Horowhenua (Electra)",
    "Manawatu (Powerco)": "Manawatū (Powerco)",
    "Taupo (Unison Networks)": "Taupō (Unison Networks)",
    "Wanganui (Powerco)": "Whanganui (Powerco)",
    "Whangarei and Kaipara (Northpower)": "Whangārei and Kaipara (Northpower)",
    "Eastland (Eastland Network)": "Tairāwhiti and Wairoa (Firstlight Network)",
}

# EMI's 39 "network reporting regions" (real distributor footprints, not
# fabricated) grouped under their NZ regional council. A handful of
# networks straddle a council boundary -- those are marked, and the
# grouping is a display choice only; every number shown is still the
# real, unmodified EMI/network figure.
NETWORK_TO_COUNCIL = {
    "Bay of Islands (Top Energy)": "Northland",
    "Whangārei and Kaipara (Northpower)": "Northland",
    "Waitemata (Vector)": "Auckland",
    "Auckland (Vector)": "Auckland",
    "Counties (Counties Power)": "Auckland",          # spans into Waikato district
    "Thames Valley (Powerco)": "Waikato",
    "Waikato (WEL Networks)": "Waikato",
    "Waipa (Waipa Networks)": "Waikato",
    "King Country (The Lines Company)": "Waikato",    # spans into Manawatū-Whanganui
    "Taupō (Unison Networks)": "Waikato",
    "Tauranga (Powerco)": "Bay of Plenty",
    "Eastern Bay of Plenty (Horizon Energy)": "Bay of Plenty",
    "Rotorua (Unison Networks)": "Bay of Plenty",
    "Tairāwhiti and Wairoa (Firstlight Network)": "Gisborne",  # Wairoa is technically Hawke's Bay
    "Hawke's Bay (Unison Networks)": "Hawke's Bay",
    "Central Hawke's Bay (Centralines)": "Hawke's Bay",
    "Southern Hawke's Bay (Scanpower)": "Manawatū-Whanganui",  # Tararua district
    "Taranaki (Powerco)": "Taranaki",
    "Manawatū (Powerco)": "Manawatū-Whanganui",
    "Whanganui (Powerco)": "Manawatū-Whanganui",
    "Kāpiti and Horowhenua (Electra)": "Wellington",  # Horowhenua is Manawatū-Whanganui
    "Wairarapa (Powerco)": "Wellington",
    "Wellington (Wellington Electricity)": "Wellington",
    "Tasman (Network Tasman)": "Tasman",
    "Nelson (Nelson Electricity)": "Nelson",
    "Marlborough (Marlborough Lines)": "Marlborough",
    "Buller (Buller Electricity)": "West Coast",
    "West Coast (Westpower)": "West Coast",
    "North Canterbury (MainPower NZ)": "Canterbury",
    "Central Canterbury (Orion New Zealand)": "Canterbury",
    "Ashburton (Electricity Ashburton)": "Canterbury",
    "South Canterbury (Alpine Energy)": "Canterbury",
    "Waitaki (Network Waitaki)": "Canterbury",
    "Central Otago (Aurora Energy)": "Otago",
    "Queenstown (Aurora Energy)": "Otago",
    "Frankton (Lakelands)": "Otago",
    "Dunedin (Aurora Energy)": "Otago",
    "Otago (OtagoNet)": "Otago",
    "Southland (The Power Company)": "Southland",
    "Invercargill (Electricity Invercargill)": "Southland",
}

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
    """National and per-network-region solar totals for the dashboard."""
    import csv
    import io

    totals, networks = {}, {}
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
        elif row.get("RegionType") == "NWK_REPORTING_REGION":
            networks[row.get("Region")] = {"icps": n, "kW": round(kw, 1)}
    return totals, networks


def fetch_total_icps():
    """Total (not just solar) ICPs per network reporting region.

    The source file is a monthly, date-stamped CSV -- we scrape today's
    link off the EMI page rather than hardcoding a filename that expires.
    """
    page = session.get(ICP_TOTALS_PAGE, timeout=60).text
    m = ICP_TOTALS_LINK_RE.search(page)
    if not m:
        print("  ! Couldn't find the ICP totals CSV link -- skipping % of ICPs")
        return {}

    url = m.group(1)
    if url.startswith("/"):
        url = "https://www.emi.ea.govt.nz" + url

    import csv
    import io

    text = fetch_csv(url)
    totals = {}
    for row in csv.DictReader(io.StringIO(text)):
        name = row.get("Network reporting region")
        n, _ = icp_value(row.get("ICPs (Total)"))
        totals[name] = totals.get(name, 0) + n
    return totals


def fetch_network_bounds():
    """Real EA-published boundaries for the 39 network regions, as
    [south, west, north, east] boxes. Cached -- distributor footprints
    essentially never change, so there's no reason to refetch weekly.
    """
    cached = load(NETWORK_BOUNDS_CACHE, None)
    if cached:
        return cached

    import io
    import zipfile

    print("Fetching network region boundaries from EA...")
    r = session.get(NETWORK_BOUNDARIES_ZIP, timeout=120)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith((".json", ".geojson")))
        geo = json.loads(z.read(name))

    def bbox_of(geom):
        xs, ys = [], []

        def scan(c):
            if isinstance(c[0], (int, float)):
                xs.append(c[0]); ys.append(c[1])
            else:
                for s in c:
                    scan(s)

        scan(geom["coordinates"])
        return [min(ys), min(xs), max(ys), max(xs)]

    bounds = {}
    for f in geo["features"]:
        name = f["properties"].get("Region", "")
        name = NETWORK_BOUNDARY_ALIASES.get(name, name)
        bounds[name] = bbox_of(f["geometry"])

    save(NETWORK_BOUNDS_CACHE, bounds)
    return bounds


def _union_bbox(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def build_region_tree(networks, total_icps, bounds):
    """Nest the 39 network regions under their regional council.

    Every number here is a real, unmodified EMI figure -- the council
    grouping is a display choice (see NETWORK_TO_COUNCIL), never a
    fabricated one. Ranked by % of ICPs with solar, which is what the
    dashboard leads with.
    """
    councils = {}
    # Union with total_icps: a network can have real connections but zero
    # solar rows in the source (e.g. Nelson) -- it should still show up,
    # honestly, as 0% rather than silently vanishing from the leaderboard.
    for name in set(networks) | set(total_icps):
        council = NETWORK_TO_COUNCIL.get(name)
        if not council:
            print(f"  ! No council mapping for network region: {name}")
            continue
        s = networks.get(name, {"icps": 0, "kW": 0.0})
        total = total_icps.get(name, 0)
        pct = round(s["icps"] / total * 100, 2) if total else 0
        councils.setdefault(council, []).append({
            "name": name.split(" (")[0],
            "icps": s["icps"], "kW": s["kW"],
            "totalIcps": total, "pct": pct,
            "bbox": bounds.get(name),   # filled in below if missing
            "_fullName": name,
        })

    tree = []
    for council, children in councils.items():
        # A network missing its own boundary (e.g. the tiny Frankton
        # embedded network) is treated as visible whenever its council is,
        # rather than never showing up in the map-view filter.
        council_bbox = _union_bbox([c["bbox"] for c in children])
        for c in children:
            if not c["bbox"]:
                c["bbox"] = council_bbox
            del c["_fullName"]
        children.sort(key=lambda c: c["pct"], reverse=True)

        icps = sum(c["icps"] for c in children)
        total = sum(c["totalIcps"] for c in children)
        tree.append({
            "name": council,
            "icps": icps,
            "kW": round(sum(c["kW"] for c in children), 1),
            "totalIcps": total,
            "pct": round(icps / total * 100, 2) if total else 0,
            "bbox": council_bbox,
            "children": children,
        })
    tree.sort(key=lambda r: r["pct"], reverse=True)
    return tree


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
        totals, networks = read_regions(fetch_csv(REGION_CSV))
    except Exception as exc:                       # noqa: BLE001
        print(f"Region file unavailable ({exc}) -- continuing without it")
        totals, networks = {}, {}

    try:
        total_icps = fetch_total_icps()
    except Exception as exc:                       # noqa: BLE001
        print(f"ICP totals unavailable ({exc}) -- % of ICPs will be omitted")
        total_icps = {}

    try:
        bounds = fetch_network_bounds()
    except Exception as exc:                       # noqa: BLE001
        print(f"Network boundaries unavailable ({exc}) -- map-view filter will be omitted")
        bounds = {}

    region_tree = build_region_tree(networks, total_icps, bounds) if networks else []
    national_total = sum(total_icps.values())
    if totals and national_total:
        totals["totalIcps"] = national_total
        totals["pct"] = round(totals["icps"] / national_total * 100, 2)

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
        "regions": region_tree,
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
