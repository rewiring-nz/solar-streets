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
OUT_TRENDS = "docs/trends.json"
OUT_REGION_BOUNDARIES = "docs/region_boundaries.geojson"

# EMI's "Installed distributed generation trends" report (GUEHMT) --
# monthly ICP-count history since 2014, exportable as CSV per fuel type
# and region granularity, keyless. Same report the rest of this pipeline
# is a snapshot of; this pulls the time series instead.
GUEHMT_URL = "https://www.emi.ea.govt.nz/Retail/Download/DataReport/CSV/GUEHMT"

# ----------------------------------------------------------------------
# EV dashboard -- a second, independent dataset the map can switch to.
# Built entirely from NZTA's Motor Vehicle Register (MVR): a live,
# keyless ArcGIS table of every currently-registered NZ vehicle (~5.9M
# rows). Never downloaded whole -- every number here comes from a
# server-side grouped-count query, same idea as EMI's own pre-aggregated
# figures, just aggregated by us instead of them.
# ----------------------------------------------------------------------

MVR_SERVICE = (
    "https://services.arcgis.com/CXBb7LAjgIIdcsPt/arcgis/rest/services/"
    "MVR_Mar26/FeatureServer/0/query"
)

# Real Territorial Authority (district/city council) boundaries -- the
# MVR tags every vehicle with its owner's TLA directly, so unlike solar
# this needs no town-anchor approximation: TLA *is* the real, official
# "town level" granularity. Same ArcGIS org as the regional council
# boundaries above.
TLA_SERVICE = (
    "https://services.arcgis.com/XTtANUDT8Va4DLwI/arcgis/rest/services/"
    "nz_territorial_authorities/FeatureServer/0/query"
)
TLA_BOUNDS_CACHE = "tla_bounds.json"
OUT_EV = "docs/ev.json"
OUT_EV_BOUNDARIES = "docs/ev_boundaries.geojson"

# Vehicle categories for the EV dashboard, drawn straight from the MVR's
# own VEHICLE_TYPE/BODY_TYPE fields (verified live against real data),
# not guessed from make/model. "Trucks" folds in vans, since the MVR's
# own VEHICLE_TYPE bucket ("GOODS VAN/TRUCK/UTILITY") already lumps
# them and there's no separate BODY_TYPE for "van" fleet trucks.
EV_CATEGORIES = [
    ("Cars", "VEHICLE_TYPE = 'PASSENGER CAR/VAN'"),
    ("Utes", "BODY_TYPE = 'UTILITY'"),
    ("Trucks", "BODY_TYPE IN ('FLAT-DECK TRUCK','ARTICULATED TRUCK','OTHER TRUCK',"
               "'CAB AND CHASSIS ONLY','HEAVY VAN','LIGHT VAN')"),
    ("Buses", "VEHICLE_TYPE = 'BUS'"),
    ("Tractors", "VEHICLE_TYPE = 'TRACTOR'"),
]

# Rotorua Lakes District's territory straddles Bay of Plenty and
# Waikato -- most of its area and Rotorua city itself is Bay of Plenty,
# but the district's geometric centroid falls on its (larger, rural)
# Waikato-side land. Every other TLA resolves correctly via real
# point-in-polygon against council boundaries (checked all 67 against
# known fact); this is the one genuine exception, the same kind of
# real-world straddle NETWORK_TO_COUNCIL above already documents.
TLA_REGION_OVERRIDES = {
    "Rotorua District": "Bay of Plenty",
}

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


def _bbox_of(geom):
    """[south, west, north, east] of a GeoJSON Polygon/MultiPolygon."""
    xs, ys = [], []

    def scan(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for s in c:
                scan(s)

    scan(geom["coordinates"])
    return [min(ys), min(xs), max(ys), max(xs)]


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

    councils = {}
    for f in geo["features"]:
        name = f["properties"].get("REGC_name", "")
        if name.endswith(" Region"):
            name = name[:-len(" Region")]
        if name in ("Area Outside",):     # offshore/exclusion polygon
            continue
        geom = f["geometry"]
        councils[name] = {
            "bbox": _bbox_of(geom),
            "coords": geom["coordinates"],
            "multi": geom["type"] == "MultiPolygon",
        }

    save(REGC_BOUNDS_CACHE, councils)
    return councils


def fetch_territorial_authorities():
    """Real TLA (district/city council) polygons, for the EV dashboard.
    Cached, same reasoning as fetch_regional_councils -- these boundaries
    essentially never change, and full precision matters for the same
    reason (see the no-maxAllowableOffset note above).

    Unlike solar's towns, the EV data source (NZTA's vehicle register)
    already tags every vehicle with its real TLA directly -- these
    boundaries are only needed to draw the choropleth and to derive
    each TLA's parent region (assign_tla_regions).
    """
    cached = load(TLA_BOUNDS_CACHE, None)
    if cached:
        return cached

    print("Fetching territorial authority boundaries from Stats NZ...")
    r = session.get(TLA_SERVICE, timeout=120, params={
        "where": "1=1", "outFields": "TA_name_ascii",
        "returnGeometry": "true", "geometryPrecision": 6, "f": "geojson",
    })
    r.raise_for_status()
    geo = r.json()

    tlas = {}
    for f in geo["features"]:
        name = f["properties"].get("TA_name_ascii", "")
        if not name or name in ("Area Outside Territorial Authority", "Chatham Islands Territory"):
            continue    # not part of any of the 16 mainland regions
        geom = f["geometry"]
        tlas[name] = {
            "bbox": _bbox_of(geom),
            "coords": geom["coordinates"],
            "multi": geom["type"] == "MultiPolygon",
        }

    save(TLA_BOUNDS_CACHE, tlas)
    return tlas


def write_ev_boundaries(ev_tla_rows):
    """docs/ev_boundaries.geojson -- TLA polygons for the EV choropleth,
    tagged with each TLA's stats so the frontend can colour them without
    a second lookup. A visually-simplified fetch (unlike
    fetch_territorial_authorities' full precision, which point-in-polygon
    needs to be exact) -- fine for a filled map layer, and cuts this from
    tens of MB to a few hundred KB.
    """
    print("Fetching simplified TLA boundaries for the EV choropleth...")
    r = session.get(TLA_SERVICE, timeout=120, params={
        "where": "1=1", "outFields": "TA_name_ascii",
        "returnGeometry": "true", "geometryPrecision": 4, "maxAllowableOffset": 0.005,
        "f": "geojson",
    })
    r.raise_for_status()
    geo = r.json()

    by_name = {row["name"]: row for row in ev_tla_rows}
    features = []
    for f in geo["features"]:
        name = f["properties"].get("TA_name_ascii", "")
        row = by_name.get(name)
        if not row:
            continue
        features.append({
            "type": "Feature",
            "geometry": f["geometry"],
            "properties": {
                "name": name, "region": row["region"],
                "ev": row["ev"], "total": row["total"], "pct": row["pct"],
            },
        })

    save(OUT_EV_BOUNDARIES, {"type": "FeatureCollection", "features": features})


def write_region_boundaries(region_tree):
    """docs/region_boundaries.geojson -- regional council polygons for
    solar's "Regions" map mode, tagged with each region's real
    installs/MW/% (see build_region_tree). Same visually-simplified
    fetch as write_ev_boundaries -- fine for a filled map layer, not
    point-in-polygon.
    """
    print("Fetching simplified regional council boundaries for the solar choropleth...")
    r = session.get(REGC_SERVICE, timeout=120, params={
        "where": "1=1", "outFields": "REGC_name",
        "returnGeometry": "true", "geometryPrecision": 4, "maxAllowableOffset": 0.005,
        "f": "geojson",
    })
    r.raise_for_status()
    geo = r.json()

    by_name = {row["name"]: row for row in region_tree}
    features = []
    for f in geo["features"]:
        name = f["properties"].get("REGC_name", "")
        if name.endswith(" Region"):
            name = name[:-len(" Region")]
        row = by_name.get(name)
        if not row:
            continue
        features.append({
            "type": "Feature",
            "geometry": f["geometry"],
            "properties": {
                "name": name, "icps": row["icps"], "kW": row["kW"], "pct": row["pct"],
            },
        })

    save(OUT_REGION_BOUNDARIES, {"type": "FeatureCollection", "features": features})


def _representative_point(coords, is_multi):
    """A single representative point for a (possibly fragmented)
    polygon -- the centroid of its largest ring, so a convoluted
    coastline (many small islands/inlets, e.g. Marlborough Sounds)
    still gets exactly one sensible anchor point instead of one per
    fragment. Used for region assignment (council_of_point needs a
    point, not a whole polygon) and as the single deliberate label
    anchor for choropleth map layers -- placing a symbol layer directly
    on a polygon source puts one label per *ring*, which spams a
    fragmented coastline with dozens of duplicate labels for one area.
    """
    ring = max((poly[0] for poly in (coords if is_multi else [coords])), key=len)
    lat = sum(p[1] for p in ring) / len(ring)
    lng = sum(p[0] for p in ring) / len(ring)
    return lat, lng


def assign_tla_regions(tlas, councils):
    """Which region each TLA belongs to -- derived geometrically (real
    point-in-polygon against council boundaries, same function used for
    every other region assignment in this file), not hand-typed, so it's
    checked against the same real boundary data everywhere else relies
    on. Verified against all 67 real TLAs; TLA_REGION_OVERRIDES covers
    the one genuine exception (a TLA whose territory itself straddles
    two regions).

    Also returns each TLA's representative point (see
    _representative_point) -- the frontend's EV choropleth label layer.
    """
    result = {}
    centroids = {}
    for name, t in tlas.items():
        lat, lng = _representative_point(t["coords"], t["multi"])
        centroids[name] = (lat, lng)
        if name in TLA_REGION_OVERRIDES:
            result[name] = TLA_REGION_OVERRIDES[name]
            continue
        result[name] = council_of_point(lat, lng, councils)
    return result, centroids


def build_region_tree(networks, total_icps, council_bounds):
    """Council-level stats: % of ICPs, installs, MW -- all real EMI
    figures, joined at the network-reporting-region granularity where
    EMI itself publishes both solar and total ICPs (see
    fetch_total_icps). The council grouping on top is a display choice
    (NETWORK_TO_COUNCIL), never a fabricated number.

    Powers the "National"/"within map view" stat aggregates (which need
    a real total-ICP denominator that only exists at council
    granularity), and -- via lat/lng, the same representative-point
    idea used for the EV choropleth's labels -- the solar dashboard's
    own "Regions" map mode.
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
        bounds = council_bounds.get(council)
        lat, lng = _representative_point(bounds["coords"], bounds["multi"]) if bounds else (None, None)
        tree.append({
            "name": council,
            "icps": acc["icps"],
            "kW": round(acc["kW"], 1),
            "totalIcps": acc["totalIcps"],
            "pct": round(acc["icps"] / acc["totalIcps"] * 100, 2) if acc["totalIcps"] else 0,
            "bbox": bounds.get("bbox") if bounds else None,
            "lat": round(lat, 4) if lat is not None else None,
            "lng": round(lng, 4) if lng is not None else None,
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


def _fetch_guehmt(fuel_type, region_type):
    """One fuel-type/region-granularity slice of the GUEHMT report:
    {region name: {date: ICP count}}.
    """
    r = session.get(GUEHMT_URL, timeout=180, params={
        "DateFrom": "20130901",
        "DateTo": datetime.now(timezone.utc).strftime("%Y%m%d"),
        "FuelType": fuel_type, "RegionType": region_type, "_rsdr": "ALL",
    })
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("Month end,")), None)
    if start is None:
        raise RuntimeError("GUEHMT CSV format changed -- no 'Month end,' header found")

    import csv
    import io

    by_region = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        name = row.get("Region name")
        date = row.get("Month end")   # DD/MM/YYYY
        if not name or not date:
            continue
        try:
            icps = int(float(row["ICP count"]))
        except (TypeError, ValueError):
            continue
        d, m, y = date.split("/")
        by_region.setdefault(name, {})[f"{y}-{m}-{d}"] = icps
    return by_region


def fetch_trends():
    """Monthly solar-install history since 2014, at council and network-
    reporting-region granularity, alongside how many of those installs
    also have a battery -- real EMI figures, the time-series view of the
    same "Installed distributed generation trends" report the rest of
    this pipeline draws a single snapshot from.
    """
    print("Fetching historical install trends from EMI...")
    council_all = _fetch_guehmt("solar_all", "REG_COUNCIL")
    council_batt = _fetch_guehmt("solarplusbattery", "REG_COUNCIL")
    network_all = _fetch_guehmt("solar_all", "NWK_REPORTING_REGION_DIST")
    network_batt = _fetch_guehmt("solarplusbattery", "NWK_REPORTING_REGION_DIST")

    dates = sorted({d for series in council_all.values() for d in series}
                    | {d for series in network_all.values() for d in series})

    def series_for(all_map, batt_map):
        out = {}
        for name in all_map:
            out[name] = {
                "installs": [all_map[name].get(d, 0) for d in dates],
                "battery": [batt_map.get(name, {}).get(d, 0) for d in dates],
            }
        return out

    councils = series_for(council_all, council_batt)
    networks = series_for(network_all, network_batt)

    # National = sum of councils, rather than a 5th/6th fetch.
    national = {
        "installs": [sum(c["installs"][i] for c in councils.values()) for i in range(len(dates))],
        "battery": [sum(c["battery"][i] for c in councils.values()) for i in range(len(dates))],
    }

    return {"dates": dates, "national": national, "councils": councils, "networks": networks}


# ----------------------------------------------------------------------
# EV dashboard
# ----------------------------------------------------------------------

def _mvr_query(where, group_fields=None):
    """One query against the Motor Vehicle Register -- either a plain
    count, or a server-side grouped count (never raw rows: the table is
    ~5.9M records, far too large to page through for what's ultimately
    just a handful of numbers per TLA).
    """
    params = {"where": where, "f": "json"}
    if group_fields:
        params["groupByFieldsForStatistics"] = group_fields
        params["outStatistics"] = json.dumps([{
            "statisticType": "count", "onStatisticField": "OBJECTID", "outStatisticFieldName": "cnt",
        }])
        params["orderByFields"] = group_fields
        r = session.get(MVR_SERVICE, timeout=180, params=params)
        r.raise_for_status()
        return r.json().get("features", [])
    params["returnCountOnly"] = "true"
    r = session.get(MVR_SERVICE, timeout=180, params=params)
    r.raise_for_status()
    return r.json().get("count", 0)


def fetch_ev_snapshot(tla_names):
    """Current EV counts, and each category's real % of the *local*
    vehicle fleet, by TLA -- one groupBy-TLA query per category for EVs
    and one for that category's total fleet (2 x 5 categories + 2
    overall = 12 queries total, each aggregated server-side).

    tla_names maps the MVR's own ALL-CAPS TLA spelling (e.g. "FAR NORTH
    DISTRICT") to the boundary layer's proper-case name ("Far North
    District") -- the two are different fields from different sources,
    matched by uppercasing rather than str.title() (which mangles
    apostrophes: "Hawke's" -> "Hawke'S").
    """
    print("Fetching EV snapshot from the Motor Vehicle Register...")

    def counts_by_tla(where):
        rows = _mvr_query(where, "TLA")
        out = {}
        for r in rows:
            raw = r["attributes"]["TLA"]
            name = tla_names.get(raw)
            if name:
                out[name] = out.get(name, 0) + r["attributes"]["cnt"]
        return out

    overall_ev = counts_by_tla("MOTIVE_POWER = 'ELECTRIC'")
    overall_total = counts_by_tla("1=1")

    categories = {}
    for name, clause in EV_CATEGORIES:
        categories[name] = {
            "ev": counts_by_tla(f"MOTIVE_POWER = 'ELECTRIC' AND {clause}"),
            "total": counts_by_tla(clause),
        }

    return overall_ev, overall_total, categories


def fetch_ev_trends(tla_names):
    """Yearly EV history per TLA, overall and per category -- "vehicles
    first registered in NZ in year Y that are still on the road today",
    from one 2-D groupBy (TLA x year) query per series rather than one
    per TLA. This is current-fleet-by-vintage, not a strict historical
    registration count (a vehicle scrapped or exported since its first
    year wouldn't show) -- but EVs are almost all under ~12 years old,
    so the gap is negligible, same honest framing as the MVR itself
    supports.
    """
    print("Fetching EV registration history...")

    def yearly_by_tla(where):
        rows = _mvr_query(where, "TLA,FIRST_NZ_REGISTRATION_YEAR")
        out = {}
        for r in rows:
            a = r["attributes"]
            name, year = tla_names.get(a["TLA"]), a["FIRST_NZ_REGISTRATION_YEAR"]
            if not name or not year:
                continue
            yearly = out.setdefault(name, {})
            yearly[year] = yearly.get(year, 0) + a["cnt"]
        return out

    series = {"All": yearly_by_tla("MOTIVE_POWER = 'ELECTRIC'")}
    for name, clause in EV_CATEGORIES:
        series[name] = yearly_by_tla(f"MOTIVE_POWER = 'ELECTRIC' AND {clause}")

    years = sorted({y for by_tla in series.values() for counts in by_tla.values() for y in counts})
    return years, series


def build_ev_data(tlas, tla_region, tla_centroids, council_bounds, overall_ev, overall_total, categories, years, trend_series):
    """Assemble docs/ev.json: national + per-region + per-TLA snapshots
    (counts and real % of the local fleet, overall and per category),
    plus cumulative yearly trend lines at the same three granularities.
    """
    def pct(ev, total):
        return round(ev / total * 100, 2) if total else None

    tla_rows = []
    for name, bounds in tlas.items():
        region = tla_region.get(name)
        if not region:
            continue
        ev = overall_ev.get(name, 0)
        total = overall_total.get(name, 0)
        cat_out = {}
        for cat_name, _ in EV_CATEGORIES:
            c_ev = categories[cat_name]["ev"].get(name, 0)
            c_total = categories[cat_name]["total"].get(name, 0)
            cat_out[cat_name] = {"ev": c_ev, "total": c_total, "pct": pct(c_ev, c_total)}
        lat, lng = tla_centroids.get(name, (None, None))
        tla_rows.append({
            "name": name, "region": region,
            "ev": ev, "total": total, "pct": pct(ev, total),
            "bbox": bounds["bbox"],
            "lat": round(lat, 4) if lat is not None else None,
            "lng": round(lng, 4) if lng is not None else None,
            "categories": cat_out,
        })
    tla_rows.sort(key=lambda r: r["ev"], reverse=True)

    region_acc = {}
    for row in tla_rows:
        acc = region_acc.setdefault(row["region"], {
            "ev": 0, "total": 0,
            "categories": {c: {"ev": 0, "total": 0} for c, _ in EV_CATEGORIES},
        })
        acc["ev"] += row["ev"]; acc["total"] += row["total"]
        for cat_name, _ in EV_CATEGORIES:
            acc["categories"][cat_name]["ev"] += row["categories"][cat_name]["ev"]
            acc["categories"][cat_name]["total"] += row["categories"][cat_name]["total"]

    region_rows = []
    for name, acc in region_acc.items():
        cat_out = {c: {"ev": v["ev"], "total": v["total"], "pct": pct(v["ev"], v["total"])}
                   for c, v in acc["categories"].items()}
        region_rows.append({
            "name": name, "ev": acc["ev"], "total": acc["total"], "pct": pct(acc["ev"], acc["total"]),
            "bbox": council_bounds.get(name, {}).get("bbox"), "categories": cat_out,
        })
    region_rows.sort(key=lambda r: r["ev"], reverse=True)

    national_categories = {}
    for cat_name, _ in EV_CATEGORIES:
        c_ev = sum(categories[cat_name]["ev"].values())
        c_total = sum(categories[cat_name]["total"].values())
        national_categories[cat_name] = {"ev": c_ev, "total": c_total, "pct": pct(c_ev, c_total)}
    national_ev, national_total = sum(overall_ev.values()), sum(overall_total.values())
    national = {
        "ev": national_ev, "total": national_total, "pct": pct(national_ev, national_total),
        "categories": national_categories,
    }

    # A handful of EVs go back to the 1930s (early imports/curiosities),
    # but real uptake doesn't start until ~2016 -- stretching the chart
    # back to cover 80-odd near-flat years wastes the whole width on
    # nothing. The cumulative running total still starts from the real
    # first year (so 2013's value correctly includes everything before
    # it); only the displayed window is trimmed.
    DISPLAY_START_YEAR = 2013
    start_idx = next((i for i, y in enumerate(years) if y >= DISPLAY_START_YEAR), 0)
    dates = [str(y) for y in years[start_idx:]]

    def cumulative(year_counts):
        out, running = [], 0
        for y in years:
            running += year_counts.get(y, 0)
            out.append(running)
        return out[start_idx:]

    trend_tlas, trend_regions, trend_national = {}, {}, {}
    for series_name, by_tla in trend_series.items():
        trend_tlas[series_name] = {tla: cumulative(counts) for tla, counts in by_tla.items()}

        region_year = {}
        for tla, counts in by_tla.items():
            region = tla_region.get(tla)
            if not region:
                continue
            acc = region_year.setdefault(region, {})
            for y, c in counts.items():
                acc[y] = acc.get(y, 0) + c
        trend_regions[series_name] = {r: cumulative(counts) for r, counts in region_year.items()}

        nat_year = {}
        for counts in by_tla.values():
            for y, c in counts.items():
                nat_year[y] = nat_year.get(y, 0) + c
        trend_national[series_name] = cumulative(nat_year)

    trends = {"dates": dates, "national": trend_national, "regions": trend_regions, "tlas": trend_tlas}

    return national, region_rows, tla_rows, trends


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

    try:
        trends = fetch_trends()
        # So the frontend can offer "drill into a network region within
        # this council" without a separate lookup.
        by_council = {}
        for network in trends["networks"]:
            council = NETWORK_TO_COUNCIL.get(network)
            if council:
                by_council.setdefault(council, []).append(network)
        trends["networksByCouncil"] = by_council
        save(OUT_TRENDS, trends)
    except Exception as exc:                       # noqa: BLE001
        print(f"Trend history unavailable ({exc}) -- charts will be omitted")

    try:
        tlas = fetch_territorial_authorities()
        tla_region, tla_centroids = assign_tla_regions(tlas, council_bounds)
        tla_names = {name.upper(): name for name in tlas}
        overall_ev, overall_total, ev_categories = fetch_ev_snapshot(tla_names)
        ev_years, ev_trend_series = fetch_ev_trends(tla_names)
        ev_national, ev_regions, ev_tlas, ev_trends = build_ev_data(
            tlas, tla_region, tla_centroids, council_bounds,
            overall_ev, overall_total, ev_categories, ev_years, ev_trend_series,
        )
        save(OUT_EV, {
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "categories": [name for name, _ in EV_CATEGORIES],
            "national": ev_national,
            "regions": ev_regions,
            "tlas": ev_tlas,
            "trends": ev_trends,
        })
        write_ev_boundaries(ev_tlas)
    except Exception as exc:                       # noqa: BLE001
        print(f"EV data unavailable ({exc}) -- EV dashboard will be omitted")

    region_tree = build_region_tree(networks, total_icps, council_bounds) if networks else []
    national_total = sum(total_icps.values())
    if totals and national_total:
        totals["totalIcps"] = national_total
        totals["pct"] = round(totals["icps"] / national_total * 100, 2)

    cache = geocode(records, areas, council_bounds)

    previous = load("previous_counts.json", {})
    features, missing = build(records, cache, areas, previous)

    towns = build_towns(features, town_anchors, council_bounds)

    # region_tree's installs/kW come from EMI's network-operator join,
    # which mismatches the real council polygon for a handful of small
    # embedded networks -- "Nelson (Nelson Electricity)" covers only a
    # few blocks, while "Tasman (Network Tasman)" actually serves most
    # of Nelson city (see NETWORK_TO_COUNCIL). towns are placed by real
    # geography instead (council_of_point), so summing them per council
    # gives the true figure -- e.g. Nelson's real installs, not its tiny
    # embedded network's. Overriding here keeps every council's stats
    # consistent with the town rows shown underneath it.
    geo_by_council = {}
    for t in towns:
        if not t["council"]:
            continue
        acc = geo_by_council.setdefault(t["council"], {"icps": 0, "kW": 0.0})
        acc["icps"] += t["icps"]; acc["kW"] += t["kW"]
    for r in region_tree:
        geo = geo_by_council.get(r["name"])
        if not geo:
            continue
        r["icps"] = geo["icps"]
        r["kW"] = round(geo["kW"], 1)
        # The % denominator is still the network's total ICPs, which for
        # these same mismatched networks can be smaller than the real
        # (geographic) install count just replaced above -- a logical
        # impossibility that's the tell the denominator doesn't cover
        # the real area. Omit rather than show a nonsense/misleading %.
        if r["totalIcps"] and r["icps"] > r["totalIcps"]:
            r["pct"] = None

    # Each town's parent council's real % of ICPs, attached as labelled
    # regional context -- not a town-specific figure (see build_towns).
    council_pct = {r["name"]: r["pct"] for r in region_tree}
    for t in towns:
        t["councilPct"] = council_pct.get(t["council"], 0)

    if region_tree:
        try:
            write_region_boundaries(region_tree)
        except Exception as exc:                       # noqa: BLE001
            print(f"Region boundaries unavailable ({exc}) -- solar's Regions map mode will be omitted")

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
