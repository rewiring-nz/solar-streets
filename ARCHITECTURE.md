# Architecture

How the pipeline and the two dashboards actually work, and why they're
built the way they are. Aimed at anyone modifying `process.py` or
`docs/index.html` — if you just want to run or embed the map, see
[README.md](README.md) instead.

## Contents

- [How streets get their positions](#how-streets-get-their-positions)
- [The sidebar's region/town list](#the-sidebars-regiontown-list)
- [The trend chart's Leaderboard tab](#the-trend-charts-leaderboard-tab)
- [Searching for a street or suburb](#searching-for-a-street-or-suburb)
- [The EV dashboard](#the-ev-dashboard)

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

Expect a match rate in the 95%+ range. The stragglers are usually
private lanes, rural roads and recent renames. `process.py` prints the
rate at the end of every run, and deliberately fails the build if it
ever drops below 50%, so a bad build never gets published — see
[Reliability](#reliability) below for the other failure modes this
guards against.

## The sidebar's region/town list

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

## The trend chart's Leaderboard tab

A second tab inside the trend chart panel (both dashboards) ranks
regions/districts by real month-over-month change — raw count and %,
biggest gain first — meant to answer "is there anything worth telling
people about this month". Decliners are left off entirely: a negative
figure is far more likely a reporting/attribution quirk (an EV
re-registered to a new district, an ICP recounted under a different
network) than a real drop, so showing it would read as a false signal.

This is a *different* ranking from the sidebar's region/town list above
— that one shows the current standing (installs, MW, % of connections);
this one shows what changed since last time. Backed by
`previous_town_totals.json`/`previous_ev_totals.json`, each holding the
prior run's own totals purely so the next run can diff against them (see
`_change()` in `process.py`) — so it needs at least two real runs before
it has anything to show, and stays empty (rather than guessing) until
then.

## Searching for a street or suburb

The search box next to the dataset tabs matches a region, town/district,
or (solar only) an individual street name — same free-text matching as
the `?region=` embed param (`findPlace()` in `docs/index.html`),
extended with a flat search index built from the already-loaded street
points. EVs have no individual-street data (NZTA's register only
carries district-level addresses), so EV search covers regions/districts
only.

## The EV dashboard

Built entirely from NZTA's Motor Vehicle Register (MVR) — the live
register of every currently-registered NZ vehicle (~5.9M rows), queried
as server-side aggregate counts (`fetch_ev_snapshot`/`fetch_ev_trends`
in `process.py`), never downloaded whole.

Every vehicle in the MVR carries its owner's real Territorial Authority
(TLA — district/city council, e.g. "Queenstown-Lakes District")
directly, so unlike solar's towns this needs no nearest-anchor
approximation: TLA *is* the real "district level" granularity, straight
from official data. Districts roll up to the same 16 regions solar uses,
via `assign_tla_regions` — derived geometrically (real point-in-polygon
against the regional council boundaries already fetched for solar), not
hand-typed, and checked against all 67 real districts.
`TLA_REGION_OVERRIDES` covers the one genuine exception: Rotorua Lakes
District's own territory straddles Bay of Plenty and Waikato.

Five vehicle categories (Cars, Utes, Trucks, Buses, Tractors) are drawn
straight from the MVR's own `VEHICLE_TYPE`/`BODY_TYPE` fields
(`EV_CATEGORIES` in `process.py`), not guessed from make or model. Each
one's % figure is a real join: electric count and *total local fleet*
count for that category, in that district, from the same register at
the same time — not population-normalised, so it answers "what fraction
of this district's trucks/buses/etc. are electric", the same style of
honest, real-join percentage as solar's "% of connections".

The uptake chart is a cumulative count of vehicles by first-NZ-
registration year that are still on the road today — not a strict
historical registration count (a vehicle scrapped or exported since
wouldn't show), but EVs are almost all under ~12 years old, so the
difference is negligible. The display window starts at 2013; a handful
of EVs go back to the 1930s (early imports/curiosities), and including
those 80-odd near-flat years would waste the whole chart width on
nothing — the running total itself still starts from the real first
year, so 2013's value correctly includes everything before it.

Because the MVR has no street-level address (only district + postcode,
for privacy), districts are shown as a shaded choropleth rather than
individual dots like solar's streets — there's no honest point to place
a dot at.

## Reliability

The pipeline touches half a dozen external services (EMI, NZTA, Stats
NZ, LINZ, OpenStreetMap) on every run, so it's built to degrade rather
than break:

- **Every non-Overpass network fetch retries transient failures** through
  a single shared `get()` helper (`process.py`), instead of one bad
  request dropping a whole feature for a week. Overpass gets its own
  heavier retry-and-multi-mirror handling in `overpass()`, since it fails
  far more often.
- **`geocode()` starts from last run's own cache**, not empty — a
  council's road positions are only overwritten once its Overpass query
  actually succeeds. If Overpass is having a bad day for one council,
  that council keeps last run's real positions instead of every street
  in it vanishing from the map.
- **A near-zero record count aborts the build** before any of the
  expensive work runs. This catches an EMI CSV schema/column-rename
  change, which the match-rate check alone can't: a schema change makes
  both the matched and total counts collapse together, and 0 of 0 never
  trips a percentage threshold.
- **A match rate below 50% aborts the build** (see above), so a bad run
  is never published over a good one.
- **Every fetch (except the initial street CSV) is wrapped in a
  try/except** that logs a warning and continues with that feature
  omitted, rather than taking down the whole run over one missing data
  source.
