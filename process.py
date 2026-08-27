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
import unicodedata
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

# ICPs by ANZSIC industry classification per network region, same page as
# the total-ICPs file above -- gives a real ratio of all-connections to
# residential-only connections, used to scale a town's Census-dwelling
# estimate up to a full-ICP estimate (see fetch_anzsic_ratios).
ANZSIC_LINK_RE = re.compile(r'href="([^"]*\d{8}_MeterCategoryByLevel1ANZSIC\.csv)"')

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

# Real 2018 and 2023 Census occupied-private-dwelling counts, one point
# (SA1 centroid) per statistical area nationally -- same Stats NZ ArcGIS
# org as the boundary layers below. Lets small towns (below EMI's
# per-network-region % granularity) get an *estimated* connection
# percentage: real dwellings inside the town's real boundary, not a
# population/household-size guess (see write_town_boundaries).
SA1_CENSUS_SERVICE = (
    "https://services.arcgis.com/XTtANUDT8Va4DLwI/arcgis/rest/services/"
    "Key_Census_Insights_2018_and_2023_by_SA1/FeatureServer/19/query"
)
SA1_CENSUS_CACHE = "sa1_dwellings.json"

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
OUT_TOWN_BOUNDARIES = "docs/town_boundaries.geojson"

# Last run's town/TLA totals, kept purely so this run can attach a
# month-over-month change/changePct to each region/town/district (the
# leaderboard's data) -- see attach_changes(). Not used for anything
# else, so unlike road_cache/sa2_areas these hold only the small summary
# numbers, not full geometry.
PREV_TOWN_TOTALS = "previous_town_totals.json"
PREV_EV_TOTALS = "previous_ev_totals.json"

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

# NZTA names each published MVR service after the month they stood it
# up -- "MVR_Mar26", and "MVR_May23" before that. The current one *is*
# refreshed in place (verified live: MVR_Mar26 carries registrations
# through Jul 2026), so the name is a birth date, not a data vintage --
# but the day NZTA stands up the next one, a pinned name either 404s or,
# worse, keeps quietly serving a frozen copy. So resolve it by searching
# ArcGIS for whatever NZTA currently publishes, and keep the known-good
# name only as a fallback for when that search is unreachable.
MVR_SEARCH = "https://www.arcgis.com/sharing/rest/search"
MVR_SEARCH_QUERY = 'owner:Open.Data_NZTA title:"Motor Vehicle Register" type:"Feature Service"'
MVR_SERVICE_FALLBACK = (
    "https://services.arcgis.com/CXBb7LAjgIIdcsPt/arcgis/rest/services/"
    "MVR_Mar26/FeatureServer/0/query"
)
# Resolved once per run by resolve_mvr_service(); every _mvr_query reads
# this rather than re-searching.
MVR_SERVICE = MVR_SERVICE_FALLBACK

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
# not guessed from make/model. Vans are real BODY_TYPE values
# ("LIGHT VAN"/"HEAVY VAN") that show up under *both* the
# "PASSENGER CAR/VAN" and "GOODS VAN/TRUCK/UTILITY" VEHICLE_TYPE
# buckets -- a real quirk of NZ's vehicle classification (e.g. a
# passenger-configured Hyundai Staria vs a goods-configured Toyota
# HiAce), not a data error -- so Cars excludes them explicitly to
# avoid double-counting the same vehicle in two categories.
# Motorbikes covers MOPED and MOTORCYCLE; ATV (quad bikes) is left
# out even though the MVR tags it BODY_TYPE="MOTORCYCLE" too, since
# it isn't what "motorbike" means in common usage.
EV_CATEGORIES = [
    ("Cars", "VEHICLE_TYPE = 'PASSENGER CAR/VAN' AND BODY_TYPE NOT IN ('LIGHT VAN','HEAVY VAN')"),
    ("Utes", "BODY_TYPE = 'UTILITY'"),
    ("Vans", "BODY_TYPE IN ('LIGHT VAN','HEAVY VAN')"),
    ("Motorbikes", "VEHICLE_TYPE IN ('MOTORCYCLE','MOPED')"),
    ("Trucks", "BODY_TYPE IN ('FLAT-DECK TRUCK','ARTICULATED TRUCK','OTHER TRUCK',"
               "'CAB AND CHASSIS ONLY')"),
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

# GUEHMT (fetch_trends' battery-count source) spells one council's name
# without macrons, unlike every other real council name used throughout
# this file -- verified live: "Manawatu-Wanganui" (GUEHMT) vs the real
# "Manawatū-Whanganui" is the only mismatch across all 16 councils, and
# without this it would silently drop that one council's real battery
# data (see council_battery/main()).
GUEHMT_COUNCIL_ALIASES = {
    "Manawatu-Wanganui": "Manawatū-Whanganui",
}

SUPPRESSED = 2       # EMI's "3 or less" counted as this many

session = requests.Session()
session.headers["User-Agent"] = "nz-solar-map/1.0 (github actions; weekly build)"


def get(url, retries=3, backoff=10, **kwargs):
    """session.get() with retries -- most of this file's data sources are
    government ArcGIS/statistics endpoints, and a plain single-shot
    request would turn one transient hiccup (a slow day, a dropped
    connection) into a whole feature going missing for a week, even
    though the endpoint is otherwise fine. Every non-Overpass fetch in
    this file goes through here (Overpass gets its own retry-and-
    multi-mirror handling in overpass(), since it fails far more often
    and needs a bigger hammer).
    """
    last_exc = None
    for attempt in range(retries):
        try:
            r = session.get(url, **kwargs)
            r.raise_for_status()
            return r
        except Exception as exc:                      # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(backoff)
    raise last_exc


# ----------------------------------------------------------------------
# Street name matching
# ----------------------------------------------------------------------

ABBREV = {
    "RD": "ROAD", "ST": "STREET", "AVE": "AVENUE", "AV": "AVENUE",
    "DR": "DRIVE", "CRES": "CRESCENT", "PL": "PLACE", "TCE": "TERRACE",
    "TER": "TERRACE", "LN": "LANE", "CT": "COURT", "HWY": "HIGHWAY",
    "GRV": "GROVE", "GRVE": "GROVE", "PDE": "PARADE", "CL": "CLOSE",
    "CLSE": "CLOSE", "BLVD": "BOULEVARD", "MT": "MOUNT", "SH": "STATE HIGHWAY",
    "HTS": "HEIGHTS",
}

# EMI's PhysicalAddressStreet always carries a formal street-type suffix
# (postal-address convention); OSM's own `name` tag sometimes doesn't --
# common for private roads, rural accessways and paddock tracks, e.g. a
# real Queenstown road is EMI "O'LEARYS PADDOCK ROAD" but OSM just
# "O'Learys Paddock". _strip_street_type() lets geocode() retry a lookup
# with the trailing suffix removed -- fallback only (see geocode()),
# never in place of an exact match, so it can't merge two distinctly
# named real streets that happen to share everything but the suffix.
STREET_TYPE_WORDS = set(ABBREV.values()) | {
    "ROAD", "STREET", "LANE", "PLACE", "DRIVE", "CRESCENT", "TERRACE",
    "COURT", "AVENUE", "WAY", "CLOSE", "GROVE", "RISE", "PARADE",
    "HIGHWAY", "TRACK", "LOOP", "RIDGE", "ESPLANADE", "QUAY", "SQUARE",
    "ROW", "WALK", "BEND", "CIRCLE", "CIRCUIT", "MEWS",
}


def _strip_street_type(norm):
    """'O LEARYS PADDOCK ROAD' -> 'O LEARYS PADDOCK', or None if the
    normalised name doesn't end in a recognised street-type word (or
    would be empty without it -- a bare "ROAD" shouldn't fall back to
    "").
    """
    words = norm.split()
    if len(words) > 1 and words[-1] in STREET_TYPE_WORDS:
        return " ".join(words[:-1])
    return None


def normalise(name):
    """EMI writes 'QUEEN ST', OSM writes 'Queen Street'.

    Only the final word is expanded, so 'St Andrews Road' keeps its
    saint instead of becoming 'Street Andrews Road'.

    Northland's EMI data appends "(PVT)" to private-road addresses --
    a real marker of who owns/maintains the road, not part of the
    road's own name, and never present in OSM's name tag -- stripped
    before anything else so "Dryland Track (Pvt)" still finds OSM's
    plain "Dryland Track". Verified live: 184 of 188 Northland (PVT)
    streets that were otherwise completely unmatched turned out to
    already be in OSM under their plain name.

    Many OSM way names carry macrons on te reo Māori words ("Kākāpō
    Street") while EMI's own street field never does ("Kakapo Street") --
    NFD-decomposing and dropping the combining-mark codepoints (the same
    technique the frontend's own place search already uses for e.g.
    "Wanaka"/"Wānaka") folds a macron vowel down to its plain letter
    *before* the character-class filter below, which would otherwise
    silently blank out each accented letter into a space and mangle the
    whole word (e.g. "KĀKĀPŌ" -> "K K P", never matching anything).
    Verified live: every one of Ahipara's unmatched bird-named streets
    (Kaka/Kakapo/Kokopu/Korora/Kotare) turned out to be exactly this --
    already in OSM, just spelled with macrons.
    """
    if not name:
        return ""
    name = re.sub(r"\(PVT\)\s*$", "", name.strip(), flags=re.IGNORECASE)
    name = "".join(c for c in unicodedata.normalize("NFD", name) if unicodedata.category(c) != "Mn")
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


# Datasets that failed this run. Every optional source below is wrapped
# in its own try/except so one upstream outage can't take the whole
# build down -- but each of those saves sits *inside* its try, so a
# failure means that file simply isn't rewritten and the previously
# committed copy keeps being served. That's the right behaviour (stale
# real data beats no data), but on its own it's invisible: the run still
# goes green and the site still looks fine, so an upstream that broke in
# March wouldn't be noticed in August. Recording failures here lets the
# workflow surface them and go red *after* committing whatever did
# succeed. See BUILD_STATUS / report_failures().
FAILURES = []
BUILD_STATUS = "build_status.json"


def note_failure(dataset, exc, consequence):
    """Record (and print) an optional dataset failing, so the run can be
    marked failed at the end without losing the data that did build.
    """
    FAILURES.append({"dataset": dataset, "error": str(exc), "consequence": consequence})
    print(f"{dataset} unavailable ({exc}) -- {consequence}")


def report_failures():
    """Write the run's failure list and emit a GitHub Actions error
    annotation per failure. Doesn't exit -- main() must still return
    normally so the workflow's commit step runs and publishes whatever
    did build; the workflow fails the job afterwards off this file.
    """
    save(BUILD_STATUS, {
        "ran": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "failures": FAILURES,
    }, indent=2)
    for f in FAILURES:
        print(f"::error title=Stale dataset: {f['dataset']}::"
              f"{f['error']} -- {f['consequence']} "
              f"(the previously committed file is still being served)")


def _change(current, previous):
    """(change, changePct) against a prior run's total for the same
    area -- the leaderboard's whole data source. None/None (rather than
    treating a missing previous total as 0) both on this area's very
    first appearance, and on the very first run this feature has ever
    seen -- either way there's no real prior number to compare against,
    and claiming e.g. "+100%" against a fabricated zero baseline would be
    a fake signal, not a real one.
    """
    if not previous:
        return None, None
    change = current - previous
    return change, round(change / previous * 100, 1)


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
    #
    # No maxAllowableOffset -- full precision matters here (same reasoning
    # as fetch_territorial_authorities): this geometry is only ever
    # reduced to a bbox below, but a *simplified* polygon's bbox can be
    # meaningfully smaller than the real one, not just coarser-looking.
    # Verified live: with the previous maxAllowableOffset=500,
    # Wellington's tiny "Ngaio North" SA2 cached a bbox barely half the
    # true one's height, silently rejecting every real geocoded match
    # near its (wrongly-cut-off) southern edge. The bbox is all this
    # function keeps, so the extra precision costs nothing downstream --
    # just a heavier one-time fetch (cached to disk afterwards, same as
    # every other boundary layer in this file).
    print("Fetching SA2 boundaries from Stats NZ (all vintages)...")
    areas, offset = {}, 0
    while True:
        r = get(SA2_SERVICE, timeout=180, params={
            "where": "1=1", "orderByFields": "dataset_year ASC",
            "outFields": "SA2_code,SA2_name,dataset_year",
            "returnGeometry": "true",
            "geometryPrecision": 5, "f": "geojson",
            "resultOffset": offset, "resultRecordCount": 1000,
        })
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
    r = get(url, timeout=300)
    return r.content.decode("utf-8-sig", errors="replace")


def fetch_data_date(url):
    """When EMI itself last published this file (its HTTP Last-Modified
    header), not when our own pipeline happened to run -- the two can
    easily disagree by days or weeks, since EMI republishes on its own
    monthly cadence while this runs weekly. "Updated" should describe how
    fresh the *data* is, not how recently a cron job fired; a run on the
    20th showing "Updated August" would wrongly claim a month whose real
    figures EMI hasn't finished publishing yet. None (falling back to
    today's date at the call site) if the header is ever missing.
    """
    try:
        r = session.head(url, timeout=30)
        r.raise_for_status()
        last_modified = r.headers.get("Last-Modified")
        if last_modified:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(last_modified).date().isoformat()
    except Exception:                                  # noqa: BLE001
        pass
    return None


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
            raw_sum = (row.get("GenerationCapacityKilowattsSum") or "").strip()
            if raw_sum:
                try:
                    rec["kW"] = float(raw_sum)
                except ValueError:
                    pass
            else:
                # EMI blanks the exact Sum for small-cell-suppressed rows
                # (privacy: "3 or less" ICPs) -- but still publishes a
                # real per-ICP Avg for that same row, since an average
                # alone doesn't identify a household the way an exact
                # count/sum pair could. Verified live: every one of the
                # 6,569 non-suppressed rows has a real Sum (never blank),
                # consistently ~= Avg * ICPs, while all 30,490 suppressed
                # rows have a blank Sum but a real Avg -- so avg * this
                # row's own (nominal, SUPPRESSED-constant) icps count is
                # a real, grounded estimate, not a guess. Before this
                # fix, every suppressed row's kW silently defaulted to a
                # flatly wrong 0.0 -- 82% of all street records
                # nationally, dragging every kW total that sums over
                # them (region/town/national MW figures) well below the
                # real number.
                try:
                    avg = float(row.get("GenerationCapacityKilowattsAvg") or 0)
                    rec["kW"] = avg * n
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


def newest_dated_link(page, link_re):
    """The newest YYYYMMDD-stamped file matching link_re on an EMI page,
    as an absolute URL -- or None if the page has none.

    Picks by the date *in the filename* rather than trusting the page to
    list newest-first. EMI does currently list newest-first, so
    .search()'s first match happens to be right today -- but that's a
    presentation detail that could change without notice, and silently
    pulling last year's ICP totals would skew every "% of connections"
    on the map with nothing obviously broken to notice.
    """
    matches = link_re.findall(page)
    if not matches:
        return None
    newest = max(matches, key=lambda href: re.search(r"\d{8}", href).group(0))
    return "https://www.emi.ea.govt.nz" + newest if newest.startswith("/") else newest


def fetch_total_icps():
    """Total (not just solar) ICPs per network reporting region.

    The source file is a monthly, date-stamped CSV -- we scrape today's
    link off the EMI page rather than hardcoding a filename that expires.
    """
    page = get(ICP_TOTALS_PAGE, timeout=60).text
    url = newest_dated_link(page, ICP_TOTALS_LINK_RE)
    if not url:
        print("  ! Couldn't find the ICP totals CSV link -- skipping % of ICPs")
        return {}

    import csv
    import io

    text = fetch_csv(url)
    totals = {}
    for row in csv.DictReader(io.StringIO(text)):
        name = row.get("Network reporting region")
        n, _ = icp_value(row.get("ICPs (Total)"))
        totals[name] = totals.get(name, 0) + n
    return totals


def fetch_anzsic_ratios():
    """{council: total ICPs / residential ICPs}, from EMI's own ICP count
    by ANZSIC industry classification per network region (same page as
    fetch_total_icps, a different date-stamped CSV off it). ANZSIC L1
    code "0" is Residential; everything else is business/industrial.
    Rolled up from network to council via NETWORK_TO_COUNCIL, same as
    the rest of this file's region grouping.

    Used by write_town_boundaries to scale a town's real Census-dwelling
    count (residential-only) up to an estimated *total* ICP figure --
    the real ratio EMI's own data shows for that town's council, not a
    guessed multiplier.
    """
    page = get(ICP_TOTALS_PAGE, timeout=60).text
    url = newest_dated_link(page, ANZSIC_LINK_RE)
    if not url:
        print("  ! Couldn't find the ANZSIC breakdown CSV link -- skipping town % estimates")
        return {}

    import csv
    import io

    text = fetch_csv(url)
    totals, residential = {}, {}
    for row in csv.DictReader(io.StringIO(text)):
        region = row.get("Network reporting region")
        n, _ = icp_value(row.get("ICP count (total)"))
        totals[region] = totals.get(region, 0) + n
        if row.get("ANZSIC L1") == "0":
            residential[region] = residential.get(region, 0) + n

    council_totals, council_res = {}, {}
    for region, n in totals.items():
        council = NETWORK_TO_COUNCIL.get(region)
        if not council:
            continue
        council_totals[council] = council_totals.get(council, 0) + n
        council_res[council] = council_res.get(council, 0) + residential.get(region, 0)

    ratios = {
        c: council_totals[c] / council_res[c]
        for c in council_totals if council_res.get(c)
    }
    # National blend across every network, as a fallback for a council
    # whose own ratio is unrepresentative rather than simply missing --
    # concretely "Nelson": its *network* footprint (Nelson Electricity)
    # covers only a few blocks, while the real town's installs are
    # mostly served by Network Tasman (see NETWORK_TO_COUNCIL), so
    # Nelson's own ratio is built from a tiny, skewed sample. Applied to
    # Nelson's real (much larger) dwelling count, that ratio produced an
    # estimated total *smaller* than Nelson's real install count already
    # observed -- the same "impossible" guard _estimate_town_pcts uses
    # elsewhere -- which silently dropped Nelson's estimate entirely.
    # Verified live: this is exactly why Nelson showed no estPct.
    total_res = sum(residential.values())
    if total_res:
        ratios["__national__"] = sum(totals.values()) / total_res
    return ratios


def fetch_sa1_dwellings():
    """[[lat, lng, dwellings_2018, dwellings_2023], ...] -- real Census
    occupied-private-dwelling counts, one point (SA1 centroid) per
    statistical area, nationally.

    Feeds _estimate_town_pcts' town-level % estimate: real dwellings in
    each town's real catchment (nearest-anchor, matched against these
    centroids -- see _nearest_town_fn), not a population/household-size
    guess. SA1 is Stats NZ's finest published granularity -- far smaller
    than the SA2 areas used elsewhere in this file -- so a centroid is
    precise enough without needing full SA1 geometry.
    """
    cached = load(SA1_CENSUS_CACHE, None)
    if cached is not None:
        print(f"SA1 dwelling points: {len(cached)} (cached)")
        return cached

    print("Fetching 2018/2023 Census dwelling counts by SA1...")
    rows, offset = [], 0
    while True:
        r = get(SA1_CENSUS_SERVICE, timeout=180, params={
            "where": "1=1",
            "outFields": "C18_DwellOccupancy_Occupied,C23_DwellOccupancy_Occupied",
            "returnCentroid": "true", "returnGeometry": "false", "f": "json",
            "outSR": 4326,   # this layer's native SR is NZTM (2193), not lat/lng
            "resultOffset": offset, "resultRecordCount": 2000,
        })
        feats = r.json().get("features", [])
        if not feats:
            break
        for f in feats:
            c = f.get("centroid")
            a = f["attributes"]
            c23 = a.get("C23_DwellOccupancy_Occupied")
            if not c or c23 is None:
                continue
            rows.append([c["y"], c["x"], a.get("C18_DwellOccupancy_Occupied") or 0, c23])
        offset += len(feats)

    save(SA1_CENSUS_CACHE, rows)
    print(f"SA1 dwelling points: {len(rows)}")
    return rows


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
    r = get(REGC_SERVICE, timeout=120, params={
        "where": "1=1", "outFields": "REGC_name",
        "returnGeometry": "true", "geometryPrecision": 6, "f": "geojson",
    })
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
    r = get(TLA_SERVICE, timeout=120, params={
        "where": "1=1", "outFields": "TA_name_ascii",
        "returnGeometry": "true", "geometryPrecision": 6, "f": "geojson",
    })
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
    r = get(TLA_SERVICE, timeout=120, params={
        "where": "1=1", "outFields": "TA_name_ascii",
        "returnGeometry": "true", "geometryPrecision": 4, "maxAllowableOffset": 0.005,
        "f": "geojson",
    })
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


def write_region_boundaries(region_tree, tla_region, towns, sa1_dwellings, anzsic_ratios):
    """docs/region_boundaries.geojson -- for solar's "Regions" map mode.

    Drawn at TLA (district) granularity -- the same simplified shapes
    the EV choropleth uses. The choropleth's fill colour still comes
    from the *parent council's* real installs/MW/% (see
    build_region_tree) -- EMI's solar/%-of-ICPs join only exists at
    council granularity, so there's no real per-TLA figure to colour
    each shape by individually.

    But installs/MW themselves don't need EMI's join at all -- towns
    already carry real, geographically-placed install/kW figures (see
    build_towns), so summing the towns that fall inside each TLA's real
    boundary (point-in-polygon, not the council-wide total) gives every
    TLA its own genuine install/MW count. And for a district-level %,
    this reuses the same estimate _estimate_town_pcts builds for towns:
    real Census dwellings inside this exact TLA boundary, projected to
    the current year, scaled by the *parent council's* real
    residential/business ICP mix (EMI has no ANZSIC breakdown finer
    than council either) -- surfaced as "estPct", clearly an estimate,
    never confused with the real council-level "pct" that drives the
    colour.
    """
    from shapely.geometry import Point, mapping, shape
    from shapely.strtree import STRtree

    print("Fetching simplified TLA boundaries for the solar choropleth...")
    r = get(TLA_SERVICE, timeout=120, params={
        "where": "1=1", "outFields": "TA_name_ascii",
        "returnGeometry": "true", "geometryPrecision": 4, "maxAllowableOffset": 0.005,
        "f": "geojson",
    })
    geo = r.json()

    by_council = {row["name"]: row for row in region_tree}
    names, geoms, council_of = [], [], {}
    for f in geo["features"]:
        tla_name = f["properties"].get("TA_name_ascii", "")
        council_name = tla_region.get(tla_name)
        if council_name not in by_council:
            continue
        names.append(tla_name)
        geoms.append(shape(f["geometry"]).buffer(0))
        council_of[tla_name] = council_name

    tree = STRtree(geoms)

    def nearest_index(lng, lat):
        pt = Point(lng, lat)
        idx = tree.query(pt, predicate="intersects")
        if len(idx):
            return int(idx[0])
        return int(tree.nearest(pt))   # simplified boundary, tiny coastal gaps

    # Real installs/kW per TLA, from towns' already-real geographic data.
    tla_solar = [[0, 0.0] for _ in names]
    for t in towns:
        i = nearest_index(t["lng"], t["lat"])
        tla_solar[i][0] += t["icps"]
        tla_solar[i][1] += t["kW"]

    # Real Census dwellings inside each TLA's exact boundary, for estPct.
    tla_dwell = [[0, 0] for _ in names]
    for lat, lng, d18, d23 in sa1_dwellings:
        i = nearest_index(lng, lat)
        tla_dwell[i][0] += d18
        tla_dwell[i][1] += d23

    all_d18 = sum(v[0] for v in tla_dwell)
    all_d23 = sum(v[1] for v in tla_dwell)
    fallback_cagr = (all_d23 / all_d18) ** (1 / 5) - 1 if all_d18 else 0.0
    years_ahead = max(datetime.now(timezone.utc).year - 2023, 0)
    national_ratio = anzsic_ratios.get("__national__")

    features = []
    for i, tla_name in enumerate(names):
        council_name = council_of[tla_name]
        row = by_council[council_name]
        icps, kW = tla_solar[i]
        d18, d23 = tla_dwell[i]
        est_pct = None
        if d23 >= 30:   # below this, the growth-rate projection is mostly noise
            cagr = (d23 / d18) ** (1 / 5) - 1 if d18 else fallback_cagr
            cagr = max(-0.05, min(0.15, cagr))
            est_dwellings = d23 * (1 + cagr) ** years_ahead
            # See _estimate_town_pcts for why a council's own ratio can
            # be unrepresentative (concretely Nelson) and needs a
            # national-blend fallback rather than just being omitted.
            if est_dwellings > 0:
                for ratio in (anzsic_ratios.get(council_name), national_ratio):
                    if not ratio:
                        continue
                    est_total = est_dwellings * ratio
                    if est_total >= icps:   # impossible otherwise -- omit, don't mislead
                        est_pct = round(icps / est_total * 100, 1)
                        break
        geom_dict = mapping(geoms[i])
        lat, lng = _representative_point(geom_dict["coordinates"], geom_dict["type"] == "MultiPolygon")
        features.append({
            "type": "Feature",
            "geometry": geom_dict,
            "properties": {
                # "name" is the parent council (what "pct"/the fill
                # colour measure); "tla" is this shape's own real name
                # with its own real icps/kW and estimated estPct.
                # lat/lng is this shape's single representative point
                # (frontend label anchor -- see _representative_point).
                "name": council_name, "tla": tla_name,
                "icps": icps, "kW": round(kW, 1), "pct": row["pct"],
                "estPct": est_pct,
                "lat": round(lat, 4), "lng": round(lng, 4),
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
        r = get(TOWN_ANCHORS_SERVICE, timeout=120, params={
            "where": "1=1", "outFields": "major_name,population_estimate",
            "returnCentroid": "true", "returnGeometry": "false", "f": "json",
            "resultOffset": offset, "resultRecordCount": 2000,
        })
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


def _estimate_town_pcts(towns, town_anchors, sa1_dwellings, anzsic_ratios):
    """Attaches row["estPct"] in place to entries of `towns`, for towns
    small enough that EMI has no real per-town ICP total to divide by
    (see build_towns). An *estimate*, built entirely from real numbers:

      1. Real 2018 and 2023 Census occupied-dwelling counts inside the
         town's real catchment -- SA1 centroids assigned to their
         nearest town anchor via _nearest_town_fn, the *same* catchment
         build_towns uses for installs (not the narrower boundary
         polygon written by write_town_boundaries, which only covers
         named localities -- using that here would put installs and
         dwellings on two different-sized catchments and skew the
         ratio; see fetch_sa1_dwellings for the source).
      2. The town's own real 2018->2023 dwelling growth rate, compounded
         forward to the current year (clamped to +-5%/15% annually --
         a guard against small-sample noise in low-dwelling towns, not
         a claim about real growth ceilings).
      3. Scaled from a residential dwelling count to a full ICP estimate
         using its council's real residential/total ICP mix (EMI's own
         ANZSIC breakdown -- see fetch_anzsic_ratios).

    Two approximations remain even so -- dwellings aren't exactly
    residential ICPs, and a town's business density is assumed to match
    its whole council's -- which is why this is surfaced in the UI as
    an estimate, never with the same weight as the real region %.
    Towns where the chain doesn't produce a sane number (no dwelling
    data, no council ratio, or an impossible result) are left with no
    estPct at all -- the same "omit rather than mislead" rule the real
    percentages elsewhere in this file already follow.
    """
    if not sa1_dwellings or not anzsic_ratios or not town_anchors:
        return

    nearest_town = _nearest_town_fn(town_anchors)
    sums = {}   # name -> [dwellings_2018, dwellings_2023]
    for lat, lng, d18, d23 in sa1_dwellings:
        acc = sums.setdefault(nearest_town(lat, lng), [0, 0])
        acc[0] += d18
        acc[1] += d23

    # A town with too few (or zero) 2018 dwellings to compute its own
    # growth rate falls back to the national rate across every matched
    # town, rather than getting no estimate at all.
    all_d18 = sum(v[0] for v in sums.values())
    all_d23 = sum(v[1] for v in sums.values())
    fallback_cagr = (all_d23 / all_d18) ** (1 / 5) - 1 if all_d18 else 0.0

    years_ahead = max(datetime.now(timezone.utc).year - 2023, 0)
    by_name = {t["name"]: t for t in towns}

    national_ratio = anzsic_ratios.get("__national__")

    for name, (d18, d23) in sums.items():
        row = by_name.get(name)
        # Below ~30 dwellings the 2018->2023 ratio swings wildly on a
        # handful of houses (a literal conservation park matched 9
        # dwellings in testing) -- too little signal for a 3-year
        # compounded projection to mean anything.
        if not row or d23 < 30:
            continue
        cagr = (d23 / d18) ** (1 / 5) - 1 if d18 else fallback_cagr
        cagr = max(-0.05, min(0.15, cagr))
        est_dwellings = d23 * (1 + cagr) ** years_ahead
        if est_dwellings <= 0:
            continue
        # Try the council's own ratio first; if it's missing, or -- as
        # happens for Nelson -- produces an "impossible" result (a
        # smaller estimated total than the real install count already
        # observed, because that council's own network-derived ratio is
        # unrepresentative; see fetch_anzsic_ratios), fall back to the
        # national blend rather than dropping the estimate entirely.
        for ratio in (anzsic_ratios.get(row["council"]), national_ratio):
            if not ratio:
                continue
            est_total_icps = est_dwellings * ratio
            if est_total_icps >= row["icps"]:
                row["estPct"] = round(row["icps"] / est_total_icps * 100, 1)
                break


def write_town_boundaries(towns, town_anchors=None, sa1_dwellings=None, anzsic_ratios=None):
    """docs/town_boundaries.geojson -- a real boundary per town, for
    solar's "Towns" map mode (border lines rather than dots). Built by
    merging LINZ's own locality polygons within each major_name
    grouping -- the same field fetch_town_anchors already groups by for
    its anchor point -- into one shape via shapely, a real union rather
    than an approximation (e.g. a convex hull, which would bulge over
    empty land for any spread-out town). A town's footprint can extend
    well beyond its built-up area for locality groups with surrounding
    rural land -- that's the real LINZ grouping, drawn here purely for
    display; it's *narrower* in places than build_towns' actual
    nearest-anchor install catchment, which is why the estPct step below
    uses town_anchors instead of these polygons (see _estimate_town_pcts).

    Also computes each town's estPct (see _estimate_town_pcts), when
    Census/ANZSIC data is available -- mutates `towns` in place, so
    meta.json picks it up too.

    Needs shapely (not used anywhere else in this file) for the union
    itself; degrades gracefully (see main()) if it's not installed.
    """
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union

    print("Fetching LINZ locality polygons for town boundaries...")
    groups = {}   # major_name -> [shapely geometry, ...]
    offset = 0
    while True:
        r = get(TOWN_ANCHORS_SERVICE, timeout=180, params={
            "where": "1=1", "outFields": "major_name",
            "returnGeometry": "true", "geometryPrecision": 4, "maxAllowableOffset": 0.002,
            "f": "geojson", "resultOffset": offset, "resultRecordCount": 2000,
        })
        feats = r.json().get("features", [])
        if not feats:
            break
        for f in feats:
            name = f["properties"].get("major_name")
            geom = f.get("geometry")
            if not name or not geom:
                continue
            # buffer(0) repairs the minor self-intersections
            # maxAllowableOffset's simplification sometimes introduces --
            # a standard fix, not a precision compromise: unary_union
            # below refuses to run on invalid geometry otherwise.
            groups.setdefault(name, []).append(shape(geom).buffer(0))
        offset += len(feats)

    by_name = {t["name"]: t for t in towns}
    merged_by_name = {}
    for name, polys in groups.items():
        if name not in by_name:
            continue
        try:
            merged_by_name[name] = unary_union(polys)
        except Exception as exc:                       # noqa: BLE001
            print(f"  ! Couldn't merge locality boundary for {name}: {exc}")

    try:
        _estimate_town_pcts(towns, town_anchors, sa1_dwellings, anzsic_ratios)
    except Exception as exc:                       # noqa: BLE001
        print(f"  ! Town %-estimate step failed ({exc}) -- towns will show no estPct")

    features = []
    for name, merged in merged_by_name.items():
        row = by_name[name]
        features.append({
            "type": "Feature",
            "geometry": mapping(merged),
            "properties": {
                "name": name, "council": row["council"],
                "icps": row["icps"], "kW": row["kW"], "councilPct": row.get("councilPct"),
                "estPct": row.get("estPct"),
                "councilBattInstalls": row.get("councilBattInstalls"),
                "councilBattPct": row.get("councilBattPct"),
            },
        })

    save(OUT_TOWN_BOUNDARIES, {"type": "FeatureCollection", "features": features})


GUEHMT_MW_COLUMN = "Total capacity installed (MW)"


def _fetch_guehmt(fuel_type, region_type):
    """One fuel-type/region-granularity slice of the GUEHMT report:
    {region name: {date: (ICP count, cumulative MW)}}.

    Both metrics come out of the same response: GUEHMT's CSV export
    carries every column it can produce regardless of the report's own
    "Show" setting (verified live -- the request below doesn't ask for
    capacity and gets it anyway), so the MW series is free rather than a
    fifth and sixth round trip.
    """
    r = get(GUEHMT_URL, timeout=180, params={
        "DateFrom": "20130901",
        "DateTo": datetime.now(timezone.utc).strftime("%Y%m%d"),
        "FuelType": fuel_type, "RegionType": region_type, "_rsdr": "ALL",
    })
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
        try:
            mw = round(float(row.get(GUEHMT_MW_COLUMN) or 0), 1)
        except ValueError:
            mw = 0.0
        d, m, y = date.split("/")
        by_region.setdefault(name, {})[f"{y}-{m}-{d}"] = (icps, mw)
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
            # mw is only read off the all-solar pull: the battery pull's
            # own capacity column is the capacity of battery-equipped
            # systems, which isn't what "total installed MW" means here.
            out[name] = {
                "installs": [all_map[name].get(d, (0, 0.0))[0] for d in dates],
                "battery": [batt_map.get(name, {}).get(d, (0, 0.0))[0] for d in dates],
                "mw": [all_map[name].get(d, (0, 0.0))[1] for d in dates],
            }
        return out

    councils = series_for(council_all, council_batt)
    networks = series_for(network_all, network_batt)

    # National = sum of councils, rather than a 5th/6th fetch.
    national = {
        "installs": [sum(c["installs"][i] for c in councils.values()) for i in range(len(dates))],
        "battery": [sum(c["battery"][i] for c in councils.values()) for i in range(len(dates))],
        "mw": [round(sum(c["mw"][i] for c in councils.values()), 1) for i in range(len(dates))],
    }

    return {"dates": dates, "national": national, "councils": councils, "networks": networks}


# ----------------------------------------------------------------------
# EV dashboard
# ----------------------------------------------------------------------

def resolve_mvr_service():
    """Point MVR_SERVICE at whatever Motor Vehicle Register service NZTA
    currently publishes, and return that service's real data vintage
    (its layer's dataLastEditDate) as YYYY-MM-DD.

    The vintage is the EV equivalent of fetch_data_date's Last-Modified
    for the solar CSVs: when NZTA last refreshed the register, not when
    this pipeline happened to run. Without it a frozen upstream would
    still be stamped with today's date and look perfectly fresh.

    Falls back to the pinned MVR_SERVICE_FALLBACK (and a None vintage)
    if the search is unreachable -- a search outage shouldn't take the
    EV dashboard down while the service it would have found is fine.
    """
    global MVR_SERVICE
    try:
        r = get(MVR_SEARCH, timeout=60, params={
            "q": MVR_SEARCH_QUERY, "f": "json", "num": 25,
            "sortField": "created", "sortOrder": "desc",
        })
        results = r.json().get("results") or []
        base = results[0]["url"].rstrip("/") if results else None
    except Exception as exc:                           # noqa: BLE001
        MVR_SERVICE = MVR_SERVICE_FALLBACK
        print(f"  ! MVR service lookup failed ({exc}) -- using pinned {MVR_SERVICE_FALLBACK}")
        return None

    if not base:
        MVR_SERVICE = MVR_SERVICE_FALLBACK
        print(f"  ! MVR service lookup found nothing -- using pinned {MVR_SERVICE_FALLBACK}")
        return None

    MVR_SERVICE = f"{base}/0/query"
    print(f"MVR service: {base.rsplit('/', 2)[-2]}")

    try:
        info = get(f"{base}/0", timeout=60, params={"f": "json"}).json()
        edited = (info.get("editingInfo") or {}).get("dataLastEditDate")
        if edited:
            return datetime.fromtimestamp(edited / 1000, timezone.utc).date().isoformat()
    except Exception as exc:                           # noqa: BLE001
        print(f"  ! Couldn't read MVR data vintage ({exc})")
    return None


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
        r = get(MVR_SERVICE, timeout=180, params=params)
        return r.json().get("features", [])
    params["returnCountOnly"] = "true"
    r = get(MVR_SERVICE, timeout=180, params=params)
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


def _fmt_vehicle_word(w):
    """NZTA's MAKE/MODEL come back shouting ("TESLA", "MODEL Y"). Title-
    case each word for display, except short tokens/anything with a
    digit -- brand initialisms (BMW, MG, BYD) and model codes (EV6,
    ID.4) read wrong title-cased ("Bmw", "Ev6"), so those are left as
    the register spells them.
    """
    return w if len(w) <= 3 or any(c.isdigit() for c in w) else w.capitalize()


def _fmt_vehicle(make, model):
    return (" ".join(_fmt_vehicle_word(w) for w in make.split()),
            " ".join(_fmt_vehicle_word(w) for w in model.split()))


def fetch_ev_vehicle_models(tla_names):
    """Real Make/Model breakdown of the current electric fleet, per TLA,
    alongside each vehicle's VEHICLE_TYPE/BODY_TYPE -- everything
    EV_CATEGORIES' clauses key off, so _ev_category_for can bucket
    every row into a category client-side without 7 more queries.

    One groupBy(VEHICLE_TYPE, BODY_TYPE, MAKE, MODEL) query per TLA,
    rather than a single TLA x MAKE x MODEL query -- verified live that
    the MVR server silently truncates any single grouped query at 2000
    rows (exceededTransferLimit) once that 3-way combination is
    requested, while even Auckland alone (the single biggest TLA)
    returns a complete, untruncated 951 rows on the 4-field group
    queried on its own -- one MAKE/MODEL is practically always the same
    VEHICLE_TYPE/BODY_TYPE, so adding those two fields barely raises
    the row count. Getting each TLA's *full* breakdown (not just a
    truncated top slice) matters because region/national totals below
    are summed from these per-TLA results rather than fetched
    separately.
    """
    print("Fetching EV make/model breakdown from the Motor Vehicle Register...")
    by_tla = {}
    for raw, name in tla_names.items():
        escaped = raw.replace("'", "''")   # e.g. "Central Hawke's Bay District"
        rows = _mvr_query(
            f"MOTIVE_POWER = 'ELECTRIC' AND TLA = '{escaped}'",
            "VEHICLE_TYPE,BODY_TYPE,MAKE,MODEL",
        )
        models = []
        for r in rows:
            a = r["attributes"]
            make, model = a.get("MAKE"), a.get("MODEL")
            if make and model:
                models.append((a.get("VEHICLE_TYPE"), a.get("BODY_TYPE"), make, model, a["cnt"]))
        by_tla[name] = models
    return by_tla


TOP_VEHICLES_N = 50
VEHICLE_CATEGORY_ALL = "All"


def _ev_category_for(vehicle_type, body_type):
    """Classify one vehicle's VEHICLE_TYPE/BODY_TYPE into the same
    category a given EV_CATEGORIES clause would match -- hand-ported
    from those SQL clauses (kept in sync with them by hand, since
    they're simple enough that a real SQL-clause evaluator would be
    overkill) rather than 7 separate per-category queries.
    """
    if vehicle_type == "PASSENGER CAR/VAN" and body_type not in ("LIGHT VAN", "HEAVY VAN"):
        return "Cars"
    if body_type == "UTILITY":
        return "Utes"
    if body_type in ("LIGHT VAN", "HEAVY VAN"):
        return "Vans"
    if vehicle_type in ("MOTORCYCLE", "MOPED"):
        return "Motorbikes"
    if body_type in ("FLAT-DECK TRUCK", "ARTICULATED TRUCK", "OTHER TRUCK", "CAB AND CHASSIS ONLY"):
        return "Trucks"
    if vehicle_type == "BUS":
        return "Buses"
    if vehicle_type == "TRACTOR":
        return "Tractors"
    return None   # e.g. ATVs -- not one of EV_CATEGORIES' buckets, and left out of "All" too


def build_top_vehicles(tla_models, tla_region):
    """National + per-region + per-TLA "most popular vehicle" lists,
    each keyed by category ("All" plus every name in EV_CATEGORIES) --
    rolled up from fetch_ev_vehicle_models' real per-TLA counts (never
    a separate national/region query -- see that function's docstring
    for why summing here is the accurate path, not just the
    convenient one).
    """
    categories = [VEHICLE_CATEGORY_ALL] + [name for name, _ in EV_CATEGORIES]

    def empty_counters():
        return {cat: {} for cat in categories}

    def top_all(counters):
        return {
            cat: [
                {"make": make, "model": model, "count": c}
                for (make, model), c in sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:TOP_VEHICLES_N]
            ]
            for cat, counter in counters.items()
        }

    tlas_out, region_acc, national_acc = {}, {}, empty_counters()
    for tla, models in tla_models.items():
        counters = empty_counters()
        for vehicle_type, body_type, make, model, cnt in models:
            key = _fmt_vehicle(make, model)
            counters[VEHICLE_CATEGORY_ALL][key] = counters[VEHICLE_CATEGORY_ALL].get(key, 0) + cnt
            cat = _ev_category_for(vehicle_type, body_type)
            if cat:
                counters[cat][key] = counters[cat].get(key, 0) + cnt
        tlas_out[tla] = top_all(counters)

        region = tla_region.get(tla)
        racc = region_acc.setdefault(region, empty_counters()) if region else None
        for cat, counter in counters.items():
            for key, cnt in counter.items():
                if racc is not None:
                    racc[cat][key] = racc[cat].get(key, 0) + cnt
                national_acc[cat][key] = national_acc[cat].get(key, 0) + cnt

    return {
        "national": top_all(national_acc),
        "regions": {name: top_all(acc) for name, acc in region_acc.items()},
        "tlas": tlas_out,
    }


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


def _nearest_town_fn(town_anchors):
    """A lat,lng -> town-name closure over town_anchors' nearest-named-
    anchor catchment. Shared by build_towns (streets) and
    _estimate_town_pcts (Census dwellings) so both sides of the
    estimated-%'s ratio are drawn from the identical catchment -- a
    town's boundary polygon (write_town_boundaries) only covers its
    named localities and is narrower than this catchment in places with
    sparse surrounding naming, so using it for both would systematically
    undercount whichever side used it, skewing the ratio.
    """
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

    return nearest_town


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

    nearest_town = _nearest_town_fn(town_anchors)

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
    """Every named-highway way's full node geometry (not just its centre)
    in a (possibly large) bbox, grouped by normalised name but
    deliberately NOT collapsed to one point -- a name can be several
    distinct real streets (NZ has a lot of Queen Streets), so every
    candidate way's geometry is kept and disambiguated later by which
    SA2 it actually falls inside.

    Full geometry, not a single centre point per way, because a real
    street is often one long OSM way spanning several SA2s (a
    residential street straddling two suburbs, a road running the
    length of a gorge) -- that way's overall centre can land outside
    the specific SA2 an install is actually in, even though part of
    the very same way genuinely passes through it. Verified live: in
    one real case (Chelmsford Street, Wellington) a single 68-node way
    had 28 nodes in one SA2 and 56 in the neighbouring one, so its
    centre fell only in the second -- the first SA2's real install
    would go unmatched under the old centre-only approach. Keeping
    every node lets geocode() place a street using only the nodes that
    actually fall inside the SA2 in question.
    """
    s, w, n, e = bbox
    data = overpass(
        f'[out:json][timeout:180];way["highway"]["name"]({s},{w},{n},{e});'
        "out geom tags;"
    )
    by_name = {}
    for el in data.get("elements", []):
        name = el.get("tags", {}).get("name")
        geom = el.get("geometry")
        if not name or not geom:
            continue
        pts = [(pt["lon"], pt["lat"]) for pt in geom if "lon" in pt and "lat" in pt]
        if pts:
            by_name.setdefault(normalise(name), []).append(pts)
    return by_name


def geocode(records, areas, council_bounds):
    """One Overpass query per regional council (~16), not per SA2
    (~2,100). Batching by a much larger area cuts network round-trips by
    two orders of magnitude -- verified live: a whole-council query
    (Wellington, ~18,800 roads, full node geometry) took 24s, comfortably
    inside the pipeline's budget even at that scale.

    Each SA2's roads are then matched from its council's result set by
    keeping only the candidate *nodes* that fall inside *that SA2's own
    bbox* -- the same containment check the old per-SA2 design relied on,
    just done locally instead of via a separate network call, so multiple
    same-named streets in different towns still resolve correctly.

    A name with zero candidates falls back to a second lookup with its
    trailing street-type word stripped (see _strip_street_type) -- OSM
    sometimes tags a private/rural road without the generic suffix EMI's
    postal-address data always includes (e.g. EMI "O'Learys Paddock
    Road" vs OSM "O'Learys Paddock"). Only tried when the exact name
    found nothing at all, so it can't misroute a real "Queen Street" -
    style match.
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

    # Starts from last run's own cache, not empty -- a council's entries
    # are only ever touched below once its query actually succeeds. If
    # Overpass is having a bad day for one council, that council simply
    # keeps last week's real positions instead of every street in it
    # silently vanishing from the map. Verified live: this is exactly
    # what happened to Northland/Nelson/Tasman/Gisborne during a spell of
    # Overpass mirror outages -- without this fallback, a single bad
    # geocoding run would have erased four regions' worth of real,
    # previously-placed streets on the next publish.
    cache = load(ROAD_CACHE, {})
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
            print(f"  ! {council} failed after retries, keeping last run's data for its areas: {last_exc}")
            continue

        for code in codes:
            _, s, w, n, e = areas[code]
            local = {}
            for name in names_by_code.get(code, ()):
                # Only the nodes of each candidate way that actually fall
                # inside this SA2's own bbox -- not the way's overall
                # centre, which can sit outside it for a way that spans
                # multiple SA2s (see roads_in_bbox). A street real in
                # this SA2 is placed at the average of just its own
                # portion of the way, not dragged toward wherever the
                # rest of a long way happens to run.
                pts = [(lng, lat) for way in by_name.get(name, ())
                       for lng, lat in way if w <= lng <= e and s <= lat <= n]
                if not pts:
                    # Exact name has no node in this SA2 at all -- try
                    # again without a trailing street-type word (see
                    # _strip_street_type) before giving up. Only reached
                    # when the exact match found literally nothing, so a
                    # real "Queen Street" is never at risk of being
                    # pulled toward an unrelated "Queen".
                    stripped = _strip_street_type(name)
                    if stripped:
                        pts = [(lng, lat) for way in by_name.get(stripped, ())
                               for lng, lat in way if w <= lng <= e and s <= lat <= n]
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
    # A real run has ~35-40k records; a number this far below that means
    # read_streets() couldn't find its expected columns at all (an EMI
    # schema/rename change) rather than there genuinely being almost no
    # solar installs in NZ. Unlike a low match *rate* (checked at the end,
    # once geocoding has actually run), this failure mode makes matched
    # and total collapse *together*, so the existing "< 50% matched"
    # guard alone would never catch it -- 0 of 0 doesn't trip a percentage
    # check. Failing here, before any of the expensive work below, keeps
    # a schema change from quietly publishing an empty map.
    if len(records) < 1000:
        print("Suspiciously few street records -- likely an EMI CSV schema "
              "change; failing so nothing broken gets published")
        sys.exit(1)
    data_date = fetch_data_date(STREET_CSV) or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        totals, networks = read_regions(fetch_csv(REGION_CSV))
    except Exception as exc:                       # noqa: BLE001
        note_failure("Region file", exc, "region totals continue from the previous run")
        totals, networks = {}, {}

    try:
        total_icps = fetch_total_icps()
    except Exception as exc:                       # noqa: BLE001
        note_failure("ICP totals", exc, "% of connections will be omitted")
        total_icps = {}

    council_bounds = fetch_regional_councils()
    if not council_bounds:
        print("No council boundaries -- can't batch-geocode; aborting")
        sys.exit(1)

    # TLA boundaries/region-assignment are foundational to both the EV
    # dashboard and solar's Regions map mode -- fetched once, here, so
    # neither feature's availability depends on the other's.
    try:
        tlas = fetch_territorial_authorities()
        tla_region, tla_centroids = assign_tla_regions(tlas, council_bounds)
    except Exception as exc:                       # noqa: BLE001
        note_failure("TLA boundaries", exc, "EV dashboard and solar's Districts mode will be omitted")
        tlas, tla_region, tla_centroids = {}, {}, {}

    try:
        town_anchors = fetch_town_anchors()
    except Exception as exc:                       # noqa: BLE001
        note_failure("Town names", exc, "town-level grouping will be omitted")
        town_anchors = {}

    council_battery = {}   # {council: {"installs": N, "battery": N, "pct": N}} -- see below
    national_battery = None   # same, for the whole country
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
        # Real current-snapshot numbers per network (not just its trend
        # series) -- the same real EMI join build_region_tree uses at
        # council granularity (see fetch_total_icps), just not rolled
        # up. Lets the frontend show real installs/kW/% for a single
        # network picked from the chart dropdown, not only its parent
        # council's totals.
        trends["networkSnapshot"] = {
            name: {
                "icps": row["icps"], "kW": row["kW"],
                "totalIcps": total_icps.get(name),
                "pct": round(row["icps"] / total_icps[name] * 100, 2) if total_icps.get(name) else None,
            }
            for name, row in networks.items()
        }
        save(OUT_TRENDS, trends)

        # Real council-level battery counts, for the region list/popups
        # (see below, after region_tree/towns exist). The most recent
        # month of the same real EMI series the trend chart already
        # draws on -- installs and battery come from the same GUEHMT
        # pull here, so the % is a real join, not a number paired across
        # two different sources (which is exactly the kind of mismatch
        # that caused the Nelson estPct bug elsewhere in this file).
        # Only council granularity is published, not per-TLA/town, so
        # those show this same council figure labelled as such rather
        # than a number of their own that doesn't exist.
        for name, series in trends["councils"].items():
            name = GUEHMT_COUNCIL_ALIASES.get(name, name)
            installs, battery = series["installs"][-1], series["battery"][-1]
            if installs:
                council_battery[name] = {
                    "installs": installs, "battery": battery,
                    "pct": round(battery / installs * 100, 1),
                }

        # Whole-country equivalent, for the topbar's national view --
        # GUEHMT's own national series (itself the sum of its councils,
        # see fetch_trends), so it carries the same base/percentage
        # relationship as the per-council figures above.
        nat_installs = trends["national"]["installs"][-1]
        nat_battery = trends["national"]["battery"][-1]
        if nat_installs:
            national_battery = {
                "installs": nat_installs, "battery": nat_battery,
                "pct": round(nat_battery / nat_installs * 100, 1),
            }
    except Exception as exc:                       # noqa: BLE001
        note_failure("Trend history / battery counts", exc, "charts and battery figures will be omitted")

    if tlas:
        try:
            tla_names = {name.upper(): name for name in tlas}
            ev_data_date = resolve_mvr_service()
            overall_ev, overall_total, ev_categories = fetch_ev_snapshot(tla_names)
            ev_years, ev_trend_series = fetch_ev_trends(tla_names)
            ev_national, ev_regions, ev_tlas, ev_trends = build_ev_data(
                tlas, tla_region, tla_centroids, council_bounds,
                overall_ev, overall_total, ev_categories, ev_years, ev_trend_series,
            )
            ev_vehicle_models = fetch_ev_vehicle_models(tla_names)
            top_vehicles = build_top_vehicles(ev_vehicle_models, tla_region)

            # Month-over-month change, for the leaderboard -- see
            # _change() and PREV_EV_TOTALS. TLA totals compare directly
            # against last run's own snapshot; each region's previous
            # total is summed from that same snapshot grouped by *this*
            # run's tla_region (regions are static, so last run's real
            # per-TLA numbers grouped today are equivalent to -- and
            # simpler than -- also having archived last run's grouping).
            prev_ev = load(PREV_EV_TOTALS, {})
            for row in ev_tlas:
                row["change"], row["changePct"] = _change(row["ev"], prev_ev.get(row["name"], {}).get("ev"))
            prev_region_ev = {}
            for row in ev_tlas:
                prev = prev_ev.get(row["name"])
                if prev:
                    prev_region_ev[row["region"]] = prev_region_ev.get(row["region"], 0) + prev["ev"]
            for row in ev_regions:
                row["change"], row["changePct"] = _change(row["ev"], prev_region_ev.get(row["name"]))
            save(PREV_EV_TOTALS, {row["name"]: {"ev": row["ev"]} for row in ev_tlas})

            save(OUT_EV, {
                # NZTA's own last-refresh date, not this run's -- same
                # honest "how fresh is the data" framing as solar's
                # fetch_data_date. Today's date only as a last resort.
                "updated": ev_data_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "categories": [name for name, _ in EV_CATEGORIES],
                "national": ev_national,
                "regions": ev_regions,
                "tlas": ev_tlas,
                "trends": ev_trends,
                "topVehicles": top_vehicles,
            })
            write_ev_boundaries(ev_tlas)
        except Exception as exc:                       # noqa: BLE001
            note_failure("EV data", exc, "EV dashboard will be omitted")

    region_tree = build_region_tree(networks, total_icps, council_bounds) if networks else []
    national_total = sum(total_icps.values())
    if totals and national_total:
        totals["totalIcps"] = national_total
        totals["pct"] = round(totals["icps"] / national_total * 100, 2)
    if totals and national_battery:
        totals["battInstalls"] = national_battery["battery"]
        totals["battPct"] = national_battery["pct"]
        totals["battBase"] = national_battery["installs"]

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
    council_icps = {r["name"]: r["icps"] for r in region_tree}
    for t in towns:
        t["councilPct"] = council_pct.get(t["council"], 0)
        t["councilIcps"] = council_icps.get(t["council"])

    # Real battery counts (see council_battery, above) -- council-level
    # only, attached directly to region_tree entries (regions' own real
    # numbers) and as labelled council context on towns, same pattern as
    # councilPct just above.
    #
    # battBase is the install count GUEHMT itself counted the batteries
    # against, and it is NOT the same as this region's "icps" -- the two
    # come from different EMI reports that group ICPs by council
    # differently (SolarInstallationsByRegion rolled up through
    # NETWORK_TO_COUNCIL, whose networks straddle council lines, versus
    # GUEHMT's own REG_COUNCIL classification). Verified live across all
    # 16 councils: GUEHMT runs lower nearly everywhere, e.g. Otago 5,960
    # vs 6,578, so battInstalls/icps gives 14.5% where the real, reported
    # rate is 16.0%. Publishing the base lets the UI show where the
    # percentage actually comes from instead of inviting a division that
    # silently disagrees with it.
    for r in region_tree:
        cb = council_battery.get(r["name"])
        if cb:
            r["battInstalls"], r["battPct"] = cb["battery"], cb["pct"]
            r["battBase"] = cb["installs"]
    for t in towns:
        cb = council_battery.get(t["council"])
        if cb:
            t["councilBattInstalls"], t["councilBattPct"] = cb["battery"], cb["pct"]
            t["councilBattBase"] = cb["installs"]

    # Month-over-month change, for the leaderboard -- see _change() and
    # PREV_TOWN_TOTALS. Each council's previous total is summed from last
    # run's own per-town snapshot grouped by *this* run's town->council
    # assignment (real council boundaries barely ever move, so that's
    # equivalent to -- and simpler than -- also archiving last run's
    # grouping).
    prev_towns = load(PREV_TOWN_TOTALS, {})
    for t in towns:
        t["change"], t["changePct"] = _change(t["icps"], prev_towns.get(t["name"], {}).get("icps"))
    prev_council_icps = {}
    for t in towns:
        prev = prev_towns.get(t["name"])
        if prev and t["council"]:
            prev_council_icps[t["council"]] = prev_council_icps.get(t["council"], 0) + prev["icps"]
    for r in region_tree:
        r["change"], r["changePct"] = _change(r["icps"], prev_council_icps.get(r["name"]))
    save(PREV_TOWN_TOTALS, {t["name"]: {"icps": t["icps"]} for t in towns})

    # Census dwellings / ANZSIC ratio: fetched once here and shared by
    # both write_region_boundaries (TLA-level estPct) and
    # write_town_boundaries (town-level estPct) rather than twice.
    try:
        sa1_dwellings = fetch_sa1_dwellings()
    except Exception as exc:                       # noqa: BLE001
        note_failure("Census dwellings", exc, "district/town % estimates will be omitted")
        sa1_dwellings = []
    try:
        anzsic_ratios = fetch_anzsic_ratios()
    except Exception as exc:                       # noqa: BLE001
        note_failure("ANZSIC ICP breakdown", exc, "district/town % estimates will be omitted")
        anzsic_ratios = {}

    if region_tree and tla_region:
        try:
            write_region_boundaries(region_tree, tla_region, towns, sa1_dwellings, anzsic_ratios)
        except Exception as exc:                       # noqa: BLE001
            note_failure("Region boundaries", exc, "solar's Districts map mode will be omitted")

    if towns:
        try:
            write_town_boundaries(towns, town_anchors, sa1_dwellings, anzsic_ratios)
        except Exception as exc:                       # noqa: BLE001
            note_failure("Town boundaries", exc, "solar's Towns map mode will fall back to dots")

    # Re-sort now that estPct exists (build_towns sorted by installs,
    # before write_town_boundaries had computed it) -- % of connections
    # is the more meaningful ranking for the sidebar than raw install
    # count, which just favours big towns. Towns with no estimate (rare;
    # see _estimate_town_pcts) keep their prior installs-sorted relative
    # order and sink to the end, rather than being scattered by a
    # missing value sorting as if it were zero.
    towns.sort(key=lambda t: (t.get("estPct") is not None, t.get("estPct", 0)), reverse=True)

    save(OUT_GEOJSON, {"type": "FeatureCollection", "features": features})
    save("previous_counts.json",
         {f"{c}|{n}": r["icps"] for (c, n), r in records.items()})

    matched = len(features)
    total = len(records)
    save(OUT_META, {
        "updated": data_date,
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

    # Deliberately last, and deliberately not an exit code: everything
    # that did build is already written above and should still be
    # committed. The workflow reads BUILD_STATUS after committing and
    # fails the job there, so a partial build publishes real data *and*
    # shows up red rather than passing quietly with a stale file.
    report_failures()
    if FAILURES:
        print(f"\n{len(FAILURES)} dataset(s) failed -- see the errors above")
    else:
        print("\nAll datasets refreshed successfully")


if __name__ == "__main__":
    main()
