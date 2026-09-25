# Architecture

How the pipeline and the two dashboards work, where every published
number comes from, and why they're built the way they are. Aimed at
anyone modifying `process.py` or `docs/index.html` — if you just want to
run or embed the map, see [README.md](README.md).

## Contents

- [Measured versus estimated](#measured-versus-estimated)
- [How streets get their positions](#how-streets-get-their-positions)
- [Regions](#regions)
- [Towns and districts](#towns-and-districts)
- [Homes, business and farms](#homes-business-and-farms)
- [Batteries](#batteries)
- [The trend chart](#the-trend-chart)
- [The leaderboard](#the-leaderboard)
- [The EV dashboard](#the-ev-dashboard)
- [Searching](#searching)
- [Reliability](#reliability)
- [Maintenance calendar](#maintenance-calendar)

## Measured versus estimated

Every figure on the map is one of two kinds, and the UI keeps them
visibly apart:

| Figure | Kind | Source |
|---|---|---|
| Region installs, MW, homes/business | Measured | EMI installs-by-region file (`SolarInstallationsByRegion.csv`) |
| Region % of connections | Measured | EMI's published uptake rate (GUEHMT report) |
| Region battery count and % | Measured | EMI GUEHMT report |
| Street installs | Measured, but "3 or less" rows are suppressed | EMI street file (`SolarInstallationsByStreet.csv`) |
| Town/district installs and MW ("~") | Estimated from streets | Street file, calibrated to EMI's region totals |
| Town/district % ("est") | Modelled | Census dwellings + EMI connection mix |
| Farms (a range, "est") | Modelled | Stats NZ urban/rural areas and EMI ANZSIC classification |
| EV counts and % of fleet | Measured | NZTA Motor Vehicle Register |

`reconcile()` in `process.py` checks the measured figures against EMI's
own identities (residential + business = total, nationally and per
region exactly; the regions summing to the national row within 1%,
currently about 0.1%) on every run and fails the run if they disagree.

## How streets get their positions

EMI publishes street *names*, not coordinates — and New Zealand has a
great many Queen Streets. Each EMI row also carries a Statistical Area 2
(SA2) code, and every SA2 has a known bounding box from Stats NZ's
archive layer (every boundary vintage, since EMI's data references a mix
of them). A road only counts for a given street if its OpenStreetMap
position falls inside the exact SA2 EMI assigned it to, which is what
keeps Auckland's Queen Street out of Invercargill.

OpenStreetMap is queried once per regional council (~16 requests covering
the whole country), and each council's roads are then matched to the
right SA2 locally, using only the parts of each road that fall inside
that SA2. Names are normalised on both sides (abbreviations, macrons,
Northland's "(PVT)" suffix), with a fallback that drops a trailing
street-type word when the exact name finds nothing.

Expect a match rate around 97%; `process.py` prints it every run and
fails the build below 50%. The map's methodology panel shows the current
rate.

## Regions

The 16 regional councils. Installs, MW and the residential/business
split are EMI's own per-council rows. **% of connections is EMI's own
published uptake rate** for the council, from the "Installed distributed
generation trends" report (GUEHMT), under the same council attribution
as the counts.

It is deliberately *not* computed by dividing the council's solar count
by total ICPs rolled up from EMI's 39 network reporting regions
(`NETWORK_TO_COUNCIL`). Networks don't follow council lines — Network
Tasman serves most of Nelson city, Network Waitaki serves Oamaru in
Otago, Electra serves Horowhenua in Manawatū-Whanganui — so that
denominator describes a different area from the numerator, and put some
councils' rates badly out (Tasman at about half its real rate, Nelson
with none). The rollup is kept only as a fallback if GUEHMT is
unavailable, and a % it can't support is omitted rather than published.

Region boundaries (for "which council is this town in" and the
map-view filter) are Stats NZ's regional council polygons.

## Towns and districts

**Which town.** Streets are assigned to towns by containment in each
town's footprint — the union of its LINZ Suburbs and Localities polygons,
grouped by LINZ's `major_name` (so "Wānaka" and "Queenstown" are
separate, and neither is split into SA2 fragments). The ~1% of streets
outside every footprint go to the nearest town centre. Census dwellings
go through the identical rule, so both sides of the town % come from the
same catchment. Districts (Stats NZ territorial authorities) sum the
towns whose centre falls inside them.

**Installs ("~").** Town counts are added up from the street file, where
82% of rows say "3 or less". Counting those as a flat 2 overstated town
totals by anything from 1% to 40% depending on the region, because most
suppressed streets hold one install. Instead each council's suppressed
streets are counted at the average that makes that council's streets add
up to EMI's exact total (`suppressed_weights`):

```
weight = (EMI council total − exact-street installs) / suppressed streets
```

clamped to 1–3. Nationally it's about 1.6. Town totals therefore land
slightly *under* EMI's national figure — short by the streets that
couldn't be placed on a road. Individual street popups show suppressed
counts as "1–3", never as a number EMI didn't publish.

**% of connections ("est").** EMI publishes no connection total below
region level, so this is modelled (`_estimate_town_pcts`):

1. 2023 Census occupied dwellings inside the town (SA1 centroids).
2. Projected to the current year at the town's own 2018–2023 growth rate
   (clamped to −5%…+15% a year; towns under 30 dwellings are skipped).
3. Scaled from dwellings to all connections by the council's ratio of
   total to residential ICPs — from GUEHMT's uptake rates (EMI's council
   attribution), or EMI's ANZSIC file rolled up by network if GUEHMT is
   unavailable. The two agree to within ~1% where both exist.

An estimate that comes out below the town's own install count is
dropped rather than shown. The region lines under each town are always
the region's own measured figures, labelled as such.

## Homes, business and farms

Homes and business are EMI's own published split (they add up exactly
to the total). "Farms" is shown as a range, because EMI publishes no farm
split and each available method is biased in a known direction:

- **Upper:** the share of a region's street-level business installs more
  than 100 m outside every Stats NZ urban area and rural settlement,
  applied to EMI's business count. Catches rural schools, marae,
  packhouses and tourism too, so it runs high. (100 m tolerance because
  the boundaries hug shorelines and are simplified to ~11 m.)
- **Lower:** agriculture's share of the region's non-residential
  connections (EMI's ANZSIC classification), applied to its business
  installs. Assumes farms adopt solar at the same rate as other
  businesses, which likely runs low.

On the street map, individual install dots are coloured by segment
(home, business, rural business) using the street's own split.

## Batteries

EMI publishes solar-plus-battery counts by regional council only, in
GUEHMT. Regions show their own count and rate; towns show their region's
rate, labelled as the region's. The rate's base is GUEHMT's install
count for the same month, which can differ slightly from the
installs-by-region file's figure, so the tooltip names the base.

## The trend chart

Solar: GUEHMT's monthly series since 2014 at national, council and
network level — installs, total MW, the average size of *new* residential
systems each month (as EMI publishes it; one month, July 2025, reflects a
reclassification rather than a real jump, and is annotated), and % of
installs with a battery. National counts are the sum of councils;
national averages come from GUEHMT's own NZ row.

EVs: vehicles still on the road today, by the year they were first
registered in NZ, stacked by category. Not a strict history (a vehicle
scrapped or exported since wouldn't show), but EVs are young enough that
the difference is small. The window starts at 2013; earlier curiosities
are included in the running total.

## The leaderboard

Ranks regions by growth since the previous data release — % growth by
default (raw numbers would put the biggest regions on top every time),
with a toggle for the absolute number. Decliners are left off: a negative
figure is far more likely an attribution quirk than a real removal.

The comparison baseline only moves when the source publishes new
figures (`rolling_baseline`). EMI rewrites its files daily without
changing them, so a solar release is identified by a fingerprint of its
figures (`content_vintage`) rather than the file date; NZTA's register by
its own data-edit date. The date of the release being compared against
is published and shown ("Fastest growth since 13 Sep 2026"). The same
mechanism drives the "+N since …" line in street popups.

## The EV dashboard

Built from NZTA's Motor Vehicle Register, queried as server-side grouped
counts (never downloaded whole; grouped queries are paged past the
server's 2,000-row cap). The service name changes whenever NZTA stands up
a new one, so it's found by searching ArcGIS each run, with the last
known name as a fallback.

Every vehicle carries its owner's territorial authority, so district
figures need no approximation. Districts roll up to the 16 regions by
point-in-polygon against the regional council boundaries
(`TLA_REGION_OVERRIDES` covers Rotorua Lakes, whose territory straddles
two regions).

- **Electric** means `MOTIVE_POWER = 'ELECTRIC'` (battery-electric).
- **% of the fleet** is electric vehicles over all registered vehicles
  in the area *excluding trailers and caravans* (`FLEET_WHERE`), which are
  ~15% of the register and have no engine.
- **Categories** (cars, utes, vans, motorbikes, trucks, buses, tractors)
  come from the register's own `VEHICLE_TYPE`/`BODY_TYPE` (`EV_CATEGORIES`);
  each category's % is against that category's local fleet. The headline
  EV count also includes electric vehicles outside these categories
  (forklifts and other mobile machines, ATVs), so the categories don't
  quite sum to it.
- **Vehicle Details** lists the 50 most common models per area, electric
  or fossil-fuelled (petrol, diesel, hybrid, plug-in hybrid, LPG/CNG,
  range-extended), with the fuel shown per model. The electric list's
  total is checked against the EV headline every run.

The register has no street-level address, so districts are drawn as a
choropleth rather than dots.

## Searching

The search box (and the `?region=` embed parameter) matches a region,
town/district, or (solar only) a street name, macron-insensitively —
exact, then prefix, then substring, broader places first. For a street
name that exists in several places it prefers the one nearest the current
view; "Queen Street, Auckland" narrows by suburb.

## Reliability

The pipeline touches EMI, NZTA, Stats NZ, LINZ and OpenStreetMap on every
run, so it's built to degrade rather than break — and to say so:

- **Every optional dataset is isolated.** A failure leaves that dataset's
  previously published file in place, records the failure in
  `build_status.json`, and the workflow turns the run red *after*
  committing whatever did build.
- **Retries.** All HTTP goes through one retrying `get()`; Overpass has
  its own multi-mirror retry, and a council whose query still fails keeps
  last run's road positions rather than losing its streets.
- **Hard stops.** Fewer than 1,000 street records (an EMI schema change)
  or a match rate below 50% aborts before publishing anything.
- **Reconciliation.** `reconcile()` checks published figures against
  EMI's own identities; the EV model list is checked against the EV
  total.
- **Self-updating references.** The newest dated EMI ICP files are picked
  by the date in their filename; the vehicle register service is found by
  search; SA2 boundaries are refetched if EMI starts referencing codes
  the cache doesn't have.
- **Flagged for a human.** A network reporting region missing from
  `NETWORK_TO_COUNCIL`, or a Census baseline more than six years old,
  fails the run with a message saying what to update.

## Maintenance calendar

Things that won't fix themselves:

- **Census.** Town and district estimates project the 2023 Census
  forward. When the 2028 Census dwelling counts are published, point
  `SA1_CENSUS_SERVICE` at them, set `CENSUS_YEAR`, and delete
  `sa1_dwellings.json` so it's refetched. The run starts failing as a
  reminder from 2030.
- **Network changes.** If EMI renames or merges a network reporting
  region, the run fails naming it; add it to `NETWORK_TO_COUNCIL`.
- **Boundary caches.** `regc_bounds.json`, `tla_bounds.json`,
  `town_anchors.json`, `town_polygons.json` and `urban_areas.json` are
  fetched once and reused. If Stats NZ or LINZ publish revised
  boundaries you want picked up, delete the relevant file and the next
  run refetches it.
- **Scheduled runs.** GitHub disables scheduled workflows on a public
  repository after 60 days without repository activity. The weekly data
  commits normally count, but if the data stops changing for two months
  the schedule can pause; re-enable it from the Actions tab.
