# NZ Solar Installations Map

A map of every street in New Zealand with solar connections, from the
Electricity Authority's EMI data. Dots sit on the actual streets, and the
whole country loads at once.

The map itself does no work: a scheduled job turns EMI's CSVs into a
single GeoJSON file, and the page just downloads and draws it.

```
EMI CSVs ──> process.py ──> docs/streets.geojson ──> the map
  (monthly)   (weekly, in       (static file,          (instant)
               GitHub Actions)   served by Pages)
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
| `process.py` | Downloads EMI data, geocodes streets, writes the GeoJSON |
| `.github/workflows/update-data.yml` | Runs the above weekly |
| `docs/index.html` | The map |
| `docs/streets.geojson` | Built output — every street with coordinates |
| `docs/meta.json` | Build date, totals, match rate, and the region dashboard tree |
| `road_cache.json` | Where each street is, from the latest run (not incrementally reused -- see below) |
| `sa2_areas.json` | Statistical area boundaries, cached |
| `regc_bounds.json` | Real regional council boundaries (Stats NZ), cached -- powers the council grouping and the "regions within map view" filter |
| `town_anchors.json` | One point per real NZ town (LINZ), cached -- powers the town grouping |
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

## Data sources

- Solar installations: [EMI, Electricity Authority](https://www.emi.ea.govt.nz/) (CC BY 4.0)
- Total ICP counts: [EMI, ICP and metering details](https://www.emi.ea.govt.nz/Retail/Datasets/MarketStructure/ICPandMeteringDetails) (CC BY 4.0)
- Street locations: [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL)
- Statistical areas and regional councils: [Stats NZ](https://datafinder.stats.govt.nz/) (CC BY 4.0)
- Town/locality names: [LINZ, Suburbs and Localities](https://data.linz.govt.nz/layer/113764-nz-suburbs-and-localities/) (CC BY 4.0)
