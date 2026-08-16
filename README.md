# NZ Solar Installations Map

A map of every street in New Zealand with solar connections, from the
Electricity Authority's EMI data. Dots sit on the actual streets, and the
whole country loads at once. A second dashboard, switchable from the
buttons at the top, shows electric vehicle uptake by district instead.

The map itself does no work: a scheduled job turns EMI's (and NZTA's)
data into static JSON/GeoJSON files, and the page just downloads and
draws them.

```
EMI CSVs ──────────────┐
NZTA vehicle register ─┼─> process.py ──> docs/*.json, *.geojson ──> the map
Stats NZ boundaries ────┘    (weekly, in       (static files,          (instant)
                              GitHub Actions)    served by Pages)
```

## Setup (about ten minutes, once)

1. **Create a GitHub repo** and upload these files, keeping the folder
   structure intact.

2. **Turn on GitHub Pages**
   Settings → Pages → Source: *Deploy from a branch* → Branch: `main`,
   folder: `/docs` → Save.

3. **Allow Actions to commit**
   Settings → Actions → General → Workflow permissions →
   *Read and write permissions* → Save.

4. **Run the first build**
   Actions tab → *Update solar data* → *Run workflow*.

   Takes roughly 15-20 minutes. Road positions are geocoded via
   OpenStreetMap in one query per regional council (~16 requests, not one
   per street), so this isn't a "slow first run, fast after" split --
   every run does the same fresh lookup and finishes in about the same
   time.

5. **Open your map** at
   `https://YOUR-USERNAME.github.io/YOUR-REPO/`

## Embedding it

```html
<iframe src="https://YOUR-USERNAME.github.io/YOUR-REPO/"
        width="100%" height="600" style="border:0"></iframe>
```

Inside an iframe, scroll-to-zoom automatically requires Ctrl/Cmd+scroll
instead (MapLibre's `cooperativeGestures`), so scrolling past the embed
scrolls the host page rather than fighting the map. Opened standalone,
it behaves as a normal map.

Query params customise what loads, for a specific-region embed or link:

| Param | Values | Effect |
|---|---|---|
| `?dataset=` | `solar` (default) or `ev` | Which dashboard opens |
| `?region=` | any region/district/town name | Flies there on load (macron-insensitive, e.g. `Wanaka` matches `Wānaka`) |
| `?chart=` | `open` | Opens the trend chart by default (always closed on mobile-width screens) |
| `?dash=` | `closed` | Starts with the regions/districts list collapsed |

e.g. `https://YOUR-USERNAME.github.io/YOUR-REPO/?dataset=ev&region=Canterbury&chart=open`

## Keeping it current

The workflow runs every Monday at 6am NZ time and commits only if
something changed. EMI publishes monthly, so nothing goes stale. You can
also trigger it by hand from the Actions tab any time.

## Sharper imagery (optional)

The map uses Esri satellite imagery, which needs no account. For LINZ's
NZ aerial imagery — much sharper over towns — get a free key at
<https://basemaps.linz.govt.nz> and paste it into `LINZ_API_KEY` near the
top of `docs/index.html`.

## Files

| File | Purpose |
|---|---|
| `process.py` | Downloads EMI/NZTA data, geocodes streets, writes the JSON/GeoJSON |
| `.github/workflows/update-data.yml` | Runs the above weekly |
| `docs/index.html` | The map (both dashboards) |
| `docs/streets.geojson` | Solar: every street with coordinates |
| `docs/meta.json` | Solar: build date, totals, match rate, and the region/town dashboard tree |
| `docs/trends.json` | Solar: monthly install/battery history since 2014 |
| `docs/ev.json` | EVs: national/region/district counts, % of local fleet, and yearly uptake trend, per vehicle category |
| `docs/ev_boundaries.geojson` | EVs: simplified district polygons for the choropleth, tagged with each district's stats |
| `road_cache.json` | Where each street is, from the latest run (not incrementally reused -- see below) |
| `sa2_areas.json` | Statistical area boundaries, cached |
| `regc_bounds.json` | Real regional council boundaries (Stats NZ), cached -- powers the council grouping and the "regions within map view" filter |
| `tla_bounds.json` | Real territorial authority (district/city) boundaries (Stats NZ), cached -- full precision, for the EV dashboard |
| `town_anchors.json` | One point per real NZ town (LINZ), cached -- powers the solar town grouping |
| `previous_counts.json` | Last build's numbers, for the "+N since last update" figures |

## How streets get their positions

EMI publishes street *names*, not coordinates — and New Zealand has a
great many Queen Streets. Each EMI row also carries a Statistical Area 2
(SA2) code, and every SA2 has a known bounding box (from Stats NZ,
covering all boundary vintages back to 2018 — EMI's data references a
mix of them). A road only counts for a given street if its OpenStreetMap
position falls inside the exact SA2 EMI assigned it to, which is what
keeps Auckland's Queen Street out of Invercargill.

The OpenStreetMap side is queried once per regional council (~16
requests covering the whole country), not once per SA2 (~2,100) — a
council's worth of roads is fetched in one go, then matched to the right
SA2 locally. Same accuracy, far fewer requests.

Expect a match rate in the 80–95% range. The stragglers are usually
private lanes, rural roads and recent renames. `process.py` prints the
rate at the end of every run, and deliberately fails the build if it ever
drops below 50%, so a bad build never gets published.

## The dashboard's region leaderboard

The sidebar ranks NZ's 16 regional councils by % of ICPs with solar,
alongside installs and MW. The % figure is a real join, not an estimate:
EMI publishes solar ICPs and total ICPs for the same 39 "network
reporting regions", so both numbers come from the same source at the
same granularity. The council grouping on top of that is a display
choice (`NETWORK_TO_COUNCIL` in `process.py`) — a handful of networks
straddle a council boundary and are marked there.

Expand a council to see its towns — e.g. "Wānaka" and "Queenstown" as
separate entries — with their own installs and MW. No % at this level:
EMI doesn't publish a total-ICP figure per town, only per network
region, so there's no honest denominator to divide by that finely.

Towns are real places, not SA2 fragments: each solar record is assigned
to its nearest named town centre from LINZ's Suburbs and Localities data
(`fetch_town_anchors()`/`town_anchors.json`), grouped by that dataset's
own `major_name` field. Plain SA2 would split "Wanaka" into "Wanaka
North"/"Wanaka West"; a district-level grouping would merge Wānaka and
Queenstown into one "Queenstown-Lakes" bucket. This sits at the
granularity in between — one row per commonly-recognised town.

Both levels use real regional-council boundaries from Stats NZ
(`fetch_regional_councils()`/`regc_bounds.json`) to decide which council
a town falls inside, and to power the "regions within map view" filter.
An earlier version approximated council boundaries by unioning network
operators' own footprints, which don't follow council lines — Network
Tasman serves most of Nelson city, for instance, so that approach had
Nelson's real numbers geographically misattributed to Tasman. Real
council polygons don't have that problem.

## The EV dashboard

Built entirely from NZTA's Motor Vehicle Register (MVR) -- the live
register of every currently-registered NZ vehicle (~5.9M rows), queried
as server-side aggregate counts (`fetch_ev_snapshot`/`fetch_ev_trends`
in `process.py`), never downloaded whole.

Every vehicle in the MVR carries its owner's real Territorial Authority
(TLA -- district/city council, e.g. "Queenstown-Lakes District")
directly, so unlike solar's towns this needs no nearest-anchor
approximation: TLA *is* the real "district level" granularity, straight
from official data. Districts roll up to the same 16 regions solar
uses, via `assign_tla_regions` -- derived geometrically (real
point-in-polygon against the regional council boundaries already
fetched for solar), not hand-typed, and checked against all 67 real
districts. `TLA_REGION_OVERRIDES` covers the one genuine exception:
Rotorua Lakes District's own territory straddles Bay of Plenty and
Waikato.

Five vehicle categories (Cars, Utes, Trucks, Buses, Tractors) are drawn
straight from the MVR's own `VEHICLE_TYPE`/`BODY_TYPE` fields
(`EV_CATEGORIES` in `process.py`), not guessed from make or model. Each
one's % figure is a real join: electric count and *total local fleet*
count for that category, in that district, from the same register at
the same time -- not population-normalised, so it answers "what
fraction of this district's trucks/buses/etc. are electric", the same
style of honest, real-join percentage as solar's "% of connections".

The uptake chart is a cumulative count of vehicles by first-NZ-
registration year that are still on the road today -- not a strict
historical registration count (a vehicle scrapped or exported since
wouldn't show), but EVs are almost all under ~12 years old, so the
difference is negligible. The display window starts at 2013; a handful
of EVs go back to the 1930s (early imports/curiosities), and including
those 80-odd near-flat years would waste the whole chart width on
nothing -- the running total itself still starts from the real first
year, so 2013's value correctly includes everything before it.

Because the MVR has no street-level address (only district + postcode,
for privacy), districts are shown as a shaded choropleth rather than
individual dots like solar's streets -- there's no honest point to
place a dot at.

## Data sources

- Solar installations: [EMI, Electricity Authority](https://www.emi.ea.govt.nz/) (CC BY 4.0)
- Total ICP counts: [EMI, ICP and metering details](https://www.emi.ea.govt.nz/Retail/Datasets/MarketStructure/ICPandMeteringDetails) (CC BY 4.0)
- Street locations: [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL)
- Electric vehicle counts: [NZTA Waka Kotahi, Motor Vehicle Register](https://opendata-nzta.opendata.arcgis.com/datasets/NZTA::motor-vehicle-register) (CC BY 4.0)
- Statistical areas, regional councils, and territorial authorities: [Stats NZ](https://datafinder.stats.govt.nz/) (CC BY 4.0)
- Town/locality names: [LINZ, Suburbs and Localities](https://data.linz.govt.nz/layer/113764-nz-suburbs-and-localities/) (CC BY 4.0)
