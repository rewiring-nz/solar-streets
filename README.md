# NZ Solar Installations Map

A map of every street in New Zealand with solar connections, from the
Electricity Authority's EMI data — dots sit on the actual streets, and
the whole country loads at once. A second dashboard shows electric
vehicle uptake by district. Both update themselves automatically, on a
schedule, with no server to run or maintain.

**Live demo:** <https://rewiring-nz.github.io/solar-streets/>

## Contents

- [How it works](#how-it-works)
- [Setup](#setup-about-ten-minutes-once)
- [Embedding it](#embedding-it)
- [Keeping it current](#keeping-it-current)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [Data sources](#data-sources)
- [Contributing](#contributing)
- [License](#license)

## How it works

The map itself does no work: a scheduled job turns EMI's (and NZTA's)
data into static JSON/GeoJSON files, and the page just downloads and
draws them. No database, no backend, no API keys to manage.

```
EMI CSVs ──────────────┐
NZTA vehicle register ─┼─> process.py ──> docs/*.json, *.geojson ──> the map
Stats NZ boundaries ────┘    (weekly, in       (static files,          (instant)
                              GitHub Actions)    served by Pages)
```

For how street positions are actually determined, how the region/town
grouping works, and the reliability guards the pipeline runs under, see
[ARCHITECTURE.md](ARCHITECTURE.md).

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

   Takes roughly 15–20 minutes. Road positions are geocoded via
   OpenStreetMap in one query per regional council (~16 requests, not
   one per street), so this isn't a "slow first run, fast after" split —
   every run does the same fresh lookup and finishes in about the same
   time.

5. **Open your map** at `https://YOUR-USERNAME.github.io/YOUR-REPO/`

## Embedding it

```html
<iframe src="https://YOUR-USERNAME.github.io/YOUR-REPO/"
        width="100%" height="600" style="border:0"></iframe>
```

Inside an iframe, scroll-to-zoom automatically requires Ctrl/Cmd+scroll
instead (MapLibre's `cooperativeGestures`), so scrolling past the embed
scrolls the host page rather than fighting the map. Opened standalone,
it behaves as a normal map. If the embed *is* the whole page (nothing
above or below it to scroll past), `?gestures=free` turns that lock back
off so a plain scroll zooms immediately.

Query params customise what loads, for a specific-region embed or link:

| Param | Values | Effect |
|---|---|---|
| `?dataset=` | `solar` (default) or `ev` | Which dashboard opens |
| `?region=` | any region/district/town name | Flies there on load (macron-insensitive, e.g. `Wanaka` matches `Wānaka`) |
| `?chart=` | `open` | Opens the trend chart by default (always closed on mobile-width screens) |
| `?dash=` | `closed` | Starts with the regions/districts list collapsed |
| `?gestures=` | `free` | Turns off the iframe scroll-gesture lock, for a fullscreen embed with no page to scroll past |

e.g. `https://YOUR-USERNAME.github.io/YOUR-REPO/?dataset=ev&region=Canterbury&chart=open`

On the map itself, the search box (top of the page) matches a street,
suburb, region, or district by name and flies there — the interactive
equivalent of `?region=`.

## Keeping it current

The workflow runs every Monday at 6am NZ time and commits only if
something changed. EMI publishes monthly, so nothing goes stale. You can
also trigger it by hand from the Actions tab any time; concurrent runs
are queued rather than allowed to race each other.

## Configuration

The map uses Esri satellite imagery, which needs no account. For LINZ's
NZ aerial imagery — much sharper over towns — get a free key at
<https://basemaps.linz.govt.nz> and paste it into `LINZ_API_KEY` near the
top of `docs/index.html`.

## Project structure

| File | Purpose |
|---|---|
| `process.py` | Downloads EMI/NZTA data, geocodes streets, writes the JSON/GeoJSON |
| `requirements.txt` | Pinned Python dependencies for `process.py` |
| `.github/workflows/update-data.yml` | Runs the above weekly |
| `docs/index.html` | The map (both dashboards) |

<details>
<summary>Generated/cached data files (all written by <code>process.py</code>; none are hand-edited)</summary>

| File | Purpose |
|---|---|
| `docs/streets.geojson` | Solar: every street with coordinates |
| `docs/meta.json` | Solar: build date, totals, match rate, and the region/town dashboard tree |
| `docs/trends.json` | Solar: monthly install/battery history since 2014 |
| `docs/region_boundaries.geojson` | Solar: TLA-level polygons for the Regions map mode, tagged with each district's stats |
| `docs/town_boundaries.geojson` | Solar: town boundary polygons for the Towns map mode |
| `docs/ev.json` | EVs: national/region/district counts, % of local fleet, and yearly uptake trend, per vehicle category |
| `docs/ev_boundaries.geojson` | EVs: simplified district polygons for the choropleth, tagged with each district's stats |
| `road_cache.json` | Where each street is, from the latest successful run per council (see [ARCHITECTURE.md](ARCHITECTURE.md#reliability)) |
| `sa2_areas.json` | Statistical area boundaries, cached |
| `regc_bounds.json` | Real regional council boundaries (Stats NZ), cached — powers the council grouping and the "regions within map view" filter |
| `tla_bounds.json` | Real territorial authority (district/city) boundaries (Stats NZ), cached — full precision, for the EV dashboard |
| `town_anchors.json` | One point per real NZ town (LINZ), cached — powers the solar town grouping |
| `sa1_dwellings.json` | Census dwelling counts by small area, cached — powers the estimated town/district % figures |
| `previous_counts.json` | Last build's per-street numbers, for the "+N since last update" figures |
| `previous_town_totals.json` | Last build's per-town solar totals, for the Leaderboard's month-over-month change |
| `previous_ev_totals.json` | Last build's per-district EV totals, for the Leaderboard's month-over-month change |

</details>

## Data sources

- Solar installations: [EMI, Electricity Authority](https://www.emi.ea.govt.nz/) (CC BY 4.0)
- Total ICP counts: [EMI, ICP and metering details](https://www.emi.ea.govt.nz/Retail/Datasets/MarketStructure/ICPandMeteringDetails) (CC BY 4.0)
- Street locations: [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL)
- Electric vehicle counts: [NZTA Waka Kotahi, Motor Vehicle Register](https://opendata-nzta.opendata.arcgis.com/datasets/NZTA::motor-vehicle-register) (CC BY 4.0)
- Statistical areas, regional councils, and territorial authorities: [Stats NZ](https://datafinder.stats.govt.nz/) (CC BY 4.0)
- Town/locality names: [LINZ, Suburbs and Localities](https://data.linz.govt.nz/layer/113764-nz-suburbs-and-localities/) (CC BY 4.0)

## Contributing

Issues and pull requests are welcome. For anything beyond a small fix,
please open an issue first to discuss the approach — this keeps the
pipeline's data-accuracy guarantees (see [ARCHITECTURE.md](ARCHITECTURE.md))
intact.

## License

[MIT](LICENSE) for the code in this repository. The underlying datasets
keep their own licenses — see [Data sources](#data-sources) above.
