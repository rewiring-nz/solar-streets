#!/usr/bin/env python3
"""
Turn EMI's monthly solar CSVs into a map-ready GeoJSON.

Runs weekly in GitHub Actions. Road positions come from OpenStreetMap via
Overpass, queried once per regional council (~16 requests) rather than
once per statistical area (~2,100) -- the whole run takes a few minutes.

    EMI street CSV ─┐
    EMI region CSV ─┼─> join on (SA2 area + street name) ─> docs/streets.geojson
    OSM road data ──┘

Outputs docs/streets.geojson and docs/meta.json, which GitHub Pages
serves to the map.
"""

import json
import math
import os
import re
import sys
import time
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

# Real, official regional council boundaries (Eagle Technology, sourced
# from Stats NZ, CC-BY-4.0) -- used to decide which council a SA2/town
# falls inside, and to power the "regions within map view" filter.
#
# Earlier this was approximated by unioning EA's network-operator
# boundaries per council, which is wrong: a network operator's footprint
# doesn't follow council lines. Concretely, "Nelson (Nelson Electricity)"
# is a tiny legacy embedded network covering a few blocks, while most of
# Nelson city is actually served by "Tasman (Network Tasman)" -- so the
# old approach had almost all of Nelson's real data geographically
# misattributed to Tasman. Real council polygons don't have that problem.
REGC_SERVICE = (
    "https://services.arcgis.com/XTtANUDT8Va4DLwI/arcgis/rest/services/"
    "nz_regional_councils/FeatureServer/0/query"
)
REGC_BOUNDS_CACHE = "regc_bounds.json"

# LINZ's Suburbs and Localities (CC-BY-4.0, mirrored publicly on ArcGIS
# Online -- no LINZ account/API key needed). Each row is a named locality
# (e.g. "Albert Town") tagged with the larger town it belongs to via
# major_name (e.g. "Wānaka") -- exactly the "one row per real town"
# granularity for the dashboard, without needing SA2's finer split
# ("Wanaka North"/"Wanaka West") or a district-level ("Queenstown-Lakes")
# grouping that would merge Queenstown and Wānaka together.
TOWN_ANCHORS_SERVICE = (
    "https://services.arcgis.com/xdsHIIxuCWByZiCB/arcgis/rest/services/"
    "LINZ_NZ_Suburbs_and_Localities/FeatureServer/0/query"
)
TOWN_ANCHORS_CACHE = "town_anchors.json"

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

# The archive layer (Eagle Technology, sourced from Stats NZ, CC-BY-4.0),
# not just current-year boundaries -- see get_areas() for why.
SA2_SERVICE = (
    "https://services.arcgis.com/XTtANUDT8Va4DLwI/arcgis/rest/services/"
    "nz_statistical_areas_2_archive/FeatureServer/0/query"
)

# Verified-working mirrors first (checked 2026-08-15): private.coffee and
# kumi.systems currently don't respond at all, and a 180s timeout per dead
# mirror meant every single lookup paid up to 6 minutes before reaching a
# working one -- ~2,100 areas at that rate is measured in days, not hours.
OVERPASS_MIRRORS = [
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_TIMEOUT = 90   # live testing showed 3-44s depending on server load
                        # and area size; this comfortably covers that while
                        # still capping a dead mirror far below the old 180s

ROAD_CACHE = "road_cache.json"      # {sa2: {street: [lng, lat]}} -- committed
SA2_CACHE = "sa2_areas.json"        # {sa2: [name, s, w, n, e]}   -- committed
OUT_GEOJSON = "docs/streets.geojson"
OUT_META = "docs/meta.json"

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

    # The archive (not just the current-year layer) matters: EMI's street
    # data references a mix of SA2 vintages, including codes retired in
    # the 2023 boundary revision. Querying every year and keeping the
    # newest boundary per code covers current *and* legacy codes.
    print("Fetching SA2 boundaries from Stats NZ (all vintages)...")
    areas, offset = {}, 0
    while True:
        r = session.get(SA2_SERVICE, timeout=180, params={
            "where": "1=1", "orderByFields": "dataset_year ASC",
            "outFields": "SA2_code,SA2_name,dataset_year",
            "returnGeometry": "true", "maxAllowableOffset": 500,
            "geometryPrecision": 5, "f": "geojson",
            "resultOffset": offset, "resultRecordCount": 1000,
        })
        r.raise_for_status()
        feats = r.json().get("features", [])
        if not feats:
            break
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
            # Later years overwrite earlier ones (ordered ASC), so each
            # code ends up with its most recent known boundary.
            areas[str(f["properties"]["SA2_code"])] = [
                f["properties"]["SA2_name"],
                min(ys), min(xs), max(ys), max(xs),
            ]
        offset += len(feats)

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


def _point_in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(x, y, coords, is_multi):
    """coords is GeoJSON Polygon or MultiPolygon coordinates: a polygon's
    first ring is its outer boundary, any further rings are holes."""
    for poly in (coords if is_multi else [coords]):
        if poly and _point_in_ring(x, y, poly[0]):
            if not any(_point_in_ring(x, y, ring) for ring in poly[1:]):
                return True
    return False


def council_of_point(lat, lng, councils):
    """Which council a point falls inside, checked properly (polygon,
    not bbox). NZ's regions are long and irregular, so adjacent
    councils' *bounding boxes* overlap heavily even though the councils
    themselves don't -- Otago's and Southland's boxes overlap enough
    that Invercargill's coordinates sit inside both. Bbox is used only
    to shortlist candidates before the precise check.
    """
    candidates = [name for name, c in councils.items()
                  if c["bbox"][0] <= lat <= c["bbox"][2] and c["bbox"][1] <= lng <= c["bbox"][3]]
    for name in candidates:
        c = councils[name]
        if _point_in_polygon(lng, lat, c["coords"], c["multi"]):
            return name
    return candidates[0] if candidates else None


def fetch_regional_councils():
    """Real regional-council polygons, keyed by the same council names
    used in NETWORK_TO_COUNCIL. Cached -- council boundaries essentially
    never change.

    Used to batch the geocoding queries (~16 requests instead of
    ~2,100, one per council), to assign each town/SA2 to its council for
    the dashboard, and (bbox only) to power the map-view filter.
    """
    cached = load(REGC_BOUNDS_CACHE, None)
    if cached:
        return cached

    print("Fetching regional council boundaries from Stats NZ...")
    # No maxAllowableOffset: for these large, coastline-heavy polygons it
    # doesn't just simplify, it corrupts -- verified live that offset=500
    # collapsed Otago's ~9,000-point coastline into ten 4-point rectangles,
    # which silently failed point-in-polygon tests for real towns (Wanaka
    # tested as outside Otago). SA2-sized polygons are small enough that
    # the same parameter barely changes their bbox, so only this fetch
    # needed the fix. Full precision here is ~9k points per council,
    # trivial for point-in-polygon.
    r = session.get(REGC_SERVICE, timeout=120, params={
        "where": "1=1", "outFields": "REGC_name",
        "returnGeometry": "true", "geometryPrecision": 6, "f": "geojson",
    })
    r.raise_for_status()
    geo = r.json()

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

    councils = {}
    for f in geo["features"]:
        name = f["properties"].get("REGC_name", "")
        if name.endswith(" Region"):
            name = name[:-len(" Region")]
        if name in ("Area Outside",):     # offshore/exclusion polygon
            continue
        geom = f["geometry"]
        councils[name] = {
            "bbox": bbox_of(geom),
            "coords": geom["coordinates"],
            "multi": geom["type"] == "MultiPolygon",
        }

    save(REGC_BOUNDS_CACHE, councils)
    return councils


def build_region_tree(networks, total_icps, council_bounds):
    """Council-level stats: % of ICPs, installs, MW -- all real EMI
    figures, joined at the network-reporting-region granularity where
    EMI itself publishes both solar and total ICPs (see
    fetch_total_icps). The council grouping on top is a display choice
    (NETWORK_TO_COUNCIL), never a fabricated number.

    This isn't shown as a list in the dashboard (see build_towns for
    that) -- it only powers the "National"/"within map view" stat
    aggregates, which need a real total-ICP denominator that only
    exists at council granularity.
    """
    councils = {}
    # Union with total_icps: a network can have real connections but zero
    # solar rows in the source (e.g. Nelson Electricity) -- it should
    # still count, honestly, rather than silently vanishing.
    for name in set(networks) | set(total_icps):
        council = NETWORK_TO_COUNCIL.get(name)
        if not council:
            print(f"  ! No council mapping for network region: {name}")
            continue
        s = networks.get(name, {"icps": 0, "kW": 0.0})
        total = total_icps.get(name, 0)
        acc = councils.setdefault(council, {"icps": 0, "kW": 0.0, "totalIcps": 0})
        acc["icps"] += s["icps"]; acc["kW"] += s["kW"]; acc["totalIcps"] += total

    tree = []
    for council, acc in councils.items():
        tree.append({
            "name": council,
            "icps": acc["icps"],
            "kW": round(acc["kW"], 1),
            "totalIcps": acc["totalIcps"],
            "pct": round(acc["icps"] / acc["totalIcps"] * 100, 2) if acc["totalIcps"] else 0,
            "bbox": council_bounds.get(council, {}).get("bbox"),
        })
    tree.sort(key=lambda r: r["pct"], reverse=True)
    return tree


def fetch_town_anchors():
    """One representative point per named NZ town ('major_name' in
    LINZ's Suburbs and Localities data), used to group solar
    installations by real, commonly-recognised town rather than a
    statistical-area fragment: SA2 alone would split "Wanaka" into
    "Wanaka North"/"Wanaka West"/etc, and a district-level grouping would
    merge Queenstown and Wanaka into one "Queenstown-Lakes" bucket.

    The anchor for each major_name cluster is the centroid of its
    highest-population locality (not every cluster has a locality
    literally named the same as its major_name).
    """
    cached = load(TOWN_ANCHORS_CACHE, None)
    if cached:
        return cached

    print("Fetching NZ town/locality names from LINZ...")
    best = {}   # major_name -> (population, [lat, lng])
    offset = 0
    while True:
        r = session.get(TOWN_ANCHORS_SERVICE, timeout=120, params={
            "where": "1=1", "outFields": "major_name,population_estimate",
            "returnCentroid": "true", "returnGeometry": "false", "f": "json",
            "resultOffset": offset, "resultRecordCount": 2000,
        })
        r.raise_for_status()
        feats = r.json().get("features", [])
        if not feats:
            break
        for f in feats:
            name = f["attributes"].get("major_name")
            pop = f["attributes"].get("population_estimate") or 0
            c = f.get("centroid")
            if not name or not c:
                continue
            if name not in best or pop > best[name][0]:
                best[name] = (pop, [c["y"], c["x"]])
        offset += len(feats)

    anchors = {name: pt for name, (pop, pt) in best.items()}
    save(TOWN_ANCHORS_CACHE, anchors)
    return anchors


def build_towns(features, town_anchors, council_bounds):
    """Group placed streets into real towns (nearest named-locality
    centre) -- e.g. "Wanaka" and "Queenstown" as separate entries -- each
    tagged with the regional council it falls inside and its own
    coordinates (so the dashboard can filter towns to the current map
    view directly, not via their council's much coarser bbox). No %:
    EMI publishes total ICPs per network-reporting-region, not per town,
    so there's no honest denominator at this granularity -- installs and
    MW only.

    Returns a flat list, sorted by installs descending.
    """
    if not town_anchors:
        return []

    names = list(town_anchors)
    pts = [town_anchors[n] for n in names]

    def nearest_town(lat, lng):
        coslat = math.cos(math.radians(lat))
        best_i, best_d = 0, float("inf")
        for i, (alat, alng) in enumerate(pts):
            dx = (lng - alng) * coslat
            dy = lat - alat
            d = dx * dx + dy * dy
            if d < best_d:
                best_d, best_i = d, i
        return names[best_i]

    towns = {}   # town name -> accumulator
    for f in features:
        p = f["properties"]
        lng, lat = f["geometry"]["coordinates"]
        name = nearest_town(lat, lng)
        t = towns.setdefault(name, {"icps": 0, "kW": 0.0})
        t["icps"] += p["icps"]; t["kW"] += p["kW"]

    out = []
    for name, t in towns.items():
        alat, alng = town_anchors[name]
        council = council_of_point(alat, alng, council_bounds)
        out.append({
            "name": name, "council": council,
            "icps": t["icps"], "kW": round(t["kW"], 1),
            "lat": round(alat, 4), "lng": round(alng, 4),
        })

    out.sort(key=lambda x: x["icps"], reverse=True)
    return out


# ----------------------------------------------------------------------
# Step 3 - Geocode the streets we don't already know
# ----------------------------------------------------------------------

def overpass(query):
    """Try each mirror until one answers."""
    last = ""
    for url in OVERPASS_MIRRORS:
        try:
            r = session.post(url, data={"data": query}, timeout=OVERPASS_TIMEOUT)
            if r.status_code in (429, 504, 406):
                last = f"{r.status_code}"
                time.sleep(5)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last = str(exc)
    raise RuntimeError(f"all Overpass mirrors failed ({last})")


def roads_in_bbox(bbox):
    """Every named-highway way centre in a (possibly large) bbox, grouped
    by normalised name but deliberately NOT collapsed to one point -- a
    name can be several distinct real streets (NZ has a lot of Queen
    Streets), so every candidate location is kept and disambiguated later
    by which SA2 it actually falls inside.
    """
    s, w, n, e = bbox
    data = overpass(
        f'[out:json][timeout:180];way["highway"]["name"]({s},{w},{n},{e});'
        "out center tags;"
    )
    by_name = {}
    for el in data.get("elements", []):
        name = el.get("tags", {}).get("name")
        centre = el.get("center")
        if not name or not centre:
            continue
        by_name.setdefault(normalise(name), []).append((centre["lon"], centre["lat"]))
    return by_name


def geocode(records, areas, council_bounds):
    """One Overpass query per regional council (~16), not per SA2
    (~2,100). Batching by a much larger area cuts network round-trips by
    two orders of magnitude -- verified live: a whole-council query
    (Otago, ~9,200 roads) took 3.5s, the same order as a single small
    per-SA2 query used to take.

    Each SA2's roads are then matched from its council's result set by
    keeping only the candidate points that fall inside *that SA2's own
    bbox* -- the same containment check the old per-SA2 design relied on,
    just done locally instead of via a separate network call, so multiple
    same-named streets in different towns still resolve correctly.
    """
    codes_by_council = {}
    for code in {code for (code, _) in records}:
        if code not in areas:
            continue
        _, s, w, n, e = areas[code]
        cy, cx = (s + n) / 2, (w + e) / 2
        council = council_of_point(cy, cx, council_bounds)
        if council:
            codes_by_council.setdefault(council, []).append(code)

    names_by_code = {}
    for code, norm in records:
        names_by_code.setdefault(code, set()).add(norm)

    cache = {}
    n_councils = len(codes_by_council)
    for i, (council, codes) in enumerate(codes_by_council.items(), 1):
        bbox = council_bounds[council]["bbox"]
        print(f"  [{i}/{n_councils}] {council}: {len(codes)} areas...")
        # A single query covers ~100+ areas now, so a transient failure
        # (a busy mirror timing out, a 504) is worth retrying rather than
        # silently dropping that much data for the whole week.
        by_name, last_exc = None, None
        for attempt in range(3):
            try:
                by_name = roads_in_bbox(bbox)
                break
            except Exception as exc:                  # noqa: BLE001
                last_exc = exc
                if attempt < 2:
                    time.sleep(10)
        if by_name is None:
            print(f"  ! {council} failed after retries, its areas stay unplaced: {last_exc}")
            continue

        for code in codes:
            _, s, w, n, e = areas[code]
            local = {}
            for name in names_by_code.get(code, ()):
                pts = [(lng, lat) for lng, lat in by_name.get(name, ())
                       if w <= lng <= e and s <= lat <= n]
                if pts:
                    local[name] = [round(sum(p[0] for p in pts) / len(pts), 5),
                                    round(sum(p[1] for p in pts) / len(pts), 5)]
            cache[code] = local

    save(ROAD_CACHE, cache)
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
            "sa2": code,
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

    council_bounds = fetch_regional_councils()
    if not council_bounds:
        print("No council boundaries -- can't batch-geocode; aborting")
        sys.exit(1)

    try:
        town_anchors = fetch_town_anchors()
    except Exception as exc:                       # noqa: BLE001
        print(f"Town names unavailable ({exc}) -- town-level grouping will be omitted")
        town_anchors = {}

    region_tree = build_region_tree(networks, total_icps, council_bounds) if networks else []
    national_total = sum(total_icps.values())
    if totals and national_total:
        totals["totalIcps"] = national_total
        totals["pct"] = round(totals["icps"] / national_total * 100, 2)

    cache = geocode(records, areas, council_bounds)

    previous = load("previous_counts.json", {})
    features, missing = build(records, cache, areas, previous)

    towns = build_towns(features, town_anchors, council_bounds)

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
        "towns": towns,
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
