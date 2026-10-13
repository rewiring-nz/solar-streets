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
| Region % of connections, and of homes / of businesses | Measured | EMI's published uptake rates (GUEHMT report) |
| Region battery count and % | Measured | EMI GUEHMT report |
| Street installs | Measured, but "3 or less" rows are suppressed | EMI street file (`SolarInstallationsByStreet.csv`) |
| Town/district installs and MW ("~") | Estimated from streets | Street file, calibrated to EMI's region totals |
| Town/district % ("est") | Modelled | Census dwellings + EMI connection mix |
| Farms ("~") | Modelled | EMI business installs, Stats NZ urban/rural areas, Manaaki Whenua land cover, OpenStreetMap |
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
to the total). Each connection is classed by its use, so a farmhouse is
normally a home and a milking shed or pump a business.

The % beside each is that segment's adoption rate, from GUEHMT's latest
month (`_segment_pcts`): homes is EMI's published residential uptake
rate; business is non-residential solar over non-residential ICPs, both
derived as "all minus residential" from the same report (EMI's segments
split every ICP into residential and SME/commercial/industrial). The
residential ICP count this implies agrees with EMI's ANZSIC file to
within 0.1%.

**Farms** are an estimate (`estimate_farms`): EMI publishes no farm split,
and no other public dataset links solar to farms. Every road outside
towns and rural settlements (Stats NZ urban/rural areas, 100 m tolerance)
with business solar is judged on the evidence along the road itself,
within 250 m either side (`farm_evidence`, `farm_class`):

| Class | Rule |
|---|---|
| Likely farm | ≥ 60% farmland (Manaaki Whenua LCDB 2023: pasture, cropland, orchard/vineyard, tussock) and no non-farm business on the road |
| Likely not | a non-farm business (school, marae, church, hall, shop, café, fuel, accommodation, winery, industry, quarry — from OpenStreetMap) on a road ≤ 1.5 km, or < 30% farmland |
| Uncertain | anything else — usually a long road with farmland and a school or hall somewhere along it; counted part-farm (below) |

An uncertain road counts as part-farm, in proportion to how many farms
line it compared with other businesses (`farm_probability`):

```
P(farm) = F / (F + k)
F = farm connections per km × road km × farmland share
k = non-farm businesses mapped along the road
    + other businesses per km × road km × (1 − farmland share)
```

The second part of k allows for businesses OpenStreetMap doesn't know
about on the non-farm part of a road, at the density mapped across all
rural business roads (~0.4 per km); without it, a half-forest road with
nothing mapped would count as certainly a farm.

Farm connections per km is EMI's national count of agricultural
connections (ANZSIC A, ~85,000) over NZTA's rural road length (76,700 km,
June 2024): about 1.1. The range is the same calculation at half and
twice that density. (Splitting uncertain roads at the farm rate of the
clear-cut ones instead gives ~84% against this model's ~70%; it's biased
upward, because "likely not" can only fire on short roads.)

Each road's business installs count at their published value, or at the
national measured average per "3 or less" business row (about 1.3, from
EMI's exact business total). Each region's share of business installs
that are farms is applied to EMI's own business count for the region, so
the estimate scales to EMI's totals rather than the street file's.

Known limits, stated in the UI:

- **Farmhouse solar is excluded.** EMI classes it as residential, so the
  number of farms with *any* solar is probably higher than the estimate.
- **A long road's school or hall doesn't mean the install is there,**
  which is why long roads with mixed evidence are "uncertain", not "no".
- **OpenStreetMap misses some rural businesses** (contractors, home
  businesses), so "likely farm" is not certain.

Why not the obvious alternatives: assuming every business outside a town
is a farm overcounted badly — 58% of those roads have a non-farm business
on them, and the very largest "rural businesses" were grid-scale solar
farms. Applying agriculture's share of business connections (EMI's ANZSIC
file) assumes farms adopt solar at the business average, which nothing
supports. EMI's meter-register file can't identify solar (export
registers are configured on far more connections than have generation),
and only two networks have farm-specific tariffs.

Evidence is cached per road in `farm_evidence.json` (land cover and
businesses change slowly) and rechecked yearly; new roads are looked up
as they appear, a capped number per run. Road lines for business streets
are kept in `road_lines.json` for this.

**Solar farms.** A street is a grid-scale solar farm when it averages
1 MW or more per connection (`GRID_SCALE_KW`) and is either outside
towns, or averages 3 MW+ per connection (`GRID_SCALE_URBAN_KW`), or has a
published street total of 5 MW+ (`GRID_SCALE_STREET_KW`). Inside towns
the largest warehouse and airport rooftops reach about 2 MW per
connection but never 5 MW on one street, while solar farms on a
settlement's edge do. It's shown as its own map
category and excluded from the farm, rural business, town and district
figures (region totals include it, as EMI's do). When EMI
publishes such a row as "3 or less", it's counted as one plant at its
published average size — otherwise the placeholder count would double a
single plant's capacity in town and district totals.

On the street map, individual install dots are coloured by segment
(home, business, rural business, solar farm) using the street's own
split, and a rural business street's popup says how the farm estimate
judged it and why.

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

Ranks regions by growth over the last month, 3 months or 12 months — % growth by
default (raw numbers would put the biggest regions on top every time),
with a toggle for the absolute number. It reads the published monthly
series directly, so it always has data and never depends on when the
pipeline ran:

- **Solar:** EMI's monthly install count per region (GUEHMT, the same
  series as the trend chart). Growth = latest month-end minus the
  month-end k months earlier.
- **EVs:** vehicles first registered in NZ in each month, per region, from
  the register's own `FIRST_NZ_REGISTRATION_YEAR/MONTH`
  (`fetch_ev_recent`, `ev.json` → `recent`). Only months before the
  register's data month count, so a part-reported month never looks like
  a slump. % growth is against the region's EV count at the start of the
  period.

Falls are left off: in these series a negative is almost always a
connection or vehicle moving between regions, not one removed. (An
earlier version compared snapshots the pipeline saved between runs; EMI
republishes near-daily with tiny changes, so it often compared two
near-identical releases and showed nothing.)

The "+N since …" line in street popups still uses a saved per-street
snapshot (`rolling_baseline`, keyed on a fingerprint of the figures),
since EMI publishes no street-level history.

## The monthly EV leaderboard (draft)

A trophy button (shown with `?draft`, or opened directly with
`?leaderboard`) plays an animated summary of the latest complete month:
new electric vehicles counted up from zero, then regions ranked by **EV
adoption** — the share of each region's newly registered vehicles
(trailers excluded) that were electric — revealed 3rd, 2nd, 1st, then the
rest. Regions with fewer than 50 new vehicles that month are listed but
not ranked. Data comes from `ev.json` → `recent` (`fetch_ev_recent`:
electric and all vehicles by month of first NZ registration, per region),
so it updates itself each month. Respects reduced-motion settings.

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
