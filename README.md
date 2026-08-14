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

   The first run geocodes about 2,100 areas and takes roughly an hour.
   It's caching where every street physically is, which is the slow part
   and only has to happen once. Later runs take a few minutes because
   they only look up streets that are genuinely new.

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
| `road_cache.json` | Where each street is. The expensive bit, cached |
| `sa2_areas.json` | Statistical area boundaries, cached |
| `previous_counts.json` | Last build's numbers, for the "+N since last update" figures |

## How streets get their positions

EMI publishes street *names*, not coordinates — and New Zealand has a
great many Queen Streets. Each EMI row also carries a Statistical Area 2
code, so the pipeline asks OpenStreetMap for the roads inside *that
specific area* and matches on name. A road only counts if it falls within
the area EMI assigned it to, which is what keeps Auckland's Queen Street
out of Invercargill.

Expect a match rate in the 80–95% range. The stragglers are usually
private lanes, rural roads and recent renames. `process.py` prints the
rate at the end of every run, and deliberately fails the build if it ever
drops below 50%, so a bad build never gets published.

## The dashboard's region leaderboard

The sidebar ranks NZ's 16 regional councils (expand one to see its
network operators) by installs, % of ICPs with solar, or MW. The % figure
is a real join, not an estimate: EMI publishes solar ICPs and total ICPs
for the same 39 "network reporting regions", so both numbers come from
the same source at the same granularity. The council grouping on top of
that is a display choice (`NETWORK_TO_COUNCIL` in `process.py`) — a
handful of networks straddle a council boundary and are marked there.

## Data sources

- Solar installations: [EMI, Electricity Authority](https://www.emi.ea.govt.nz/) (CC BY 4.0)
- Total ICP counts: [EMI, ICP and metering details](https://www.emi.ea.govt.nz/Retail/Datasets/MarketStructure/ICPandMeteringDetails) (CC BY 4.0)
- Street locations: [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL)
- Statistical areas: [Stats NZ](https://datafinder.stats.govt.nz/) (CC BY 4.0)
