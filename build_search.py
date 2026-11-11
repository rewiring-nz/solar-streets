"""Builds docs/search_places.json: every named road, suburb, town and
locality in New Zealand, for the map's search box.

EMI only lists streets with at least one solar install, so on its own the
search couldn't find a street with none yet -- and people want to look up
their own street either way. This adds every named road and place from
OpenStreetMap (no key needed, same source process.py already uses for
road positions).

Each street is labelled with its nearest suburb/town/locality, and one
entry is kept per (name, place): a road split into many OSM ways is one
search result, while the many Queen Streets in different towns each stay
their own.

Roads barely change week to week, so this only refetches when the file is
older than MAX_AGE_DAYS (or with --force); a failed fetch keeps the old
file rather than failing the data run.

    python build_search.py [--force]
"""

import json
import math
import os
import sys
import time
from datetime import date

import requests

os.chdir(os.path.dirname(os.path.abspath(__file__)))

OUT = "docs/search_places.json"
MAX_AGE_DAYS = 28
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
NZ = 'area["ISO3166-1"="NZ"][admin_level=2]->.a;'
PLACE_TYPES = {"city": "c", "town": "t", "village": "v", "hamlet": "h", "suburb": "s",
               "quarter": "q", "neighbourhood": "n", "locality": "l"}
# Roads people live or work on; footpaths, cycleways and the like are
# left out so a search doesn't fill up with "Smith Walkway".
HIGHWAYS = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
            "living_street|service|road|track|pedestrian|"
            "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link")


def overpass_csv(query, timeout):
    last = None
    for attempt in range(2):
        for url in MIRRORS:
            try:
                r = requests.post(url, data={"data": query}, timeout=timeout + 60)
                r.raise_for_status()
                rows = [line.split("\t") for line in r.text.splitlines()[1:] if line]
                if rows:
                    return rows
                last = RuntimeError(f"{url}: empty result")
            except Exception as exc:                   # noqa: BLE001
                last = exc
            print(f"  ! {url}: {last}")
            time.sleep(5)
    raise RuntimeError(f"all Overpass mirrors failed: {last}")


def fetch_places():
    q = (f'[out:csv(name,place,::lat,::lon;true;"\\t")][timeout:300];{NZ}'
         f'node(area.a)[place~"^({"|".join(PLACE_TYPES)})$"][name];out;')
    out = []
    for name, kind, lat, lng in overpass_csv(q, 300):
        out.append((name.strip(), PLACE_TYPES[kind], float(lng), float(lat)))
    return out


def fetch_roads():
    """One query per regional council's bbox (regc_bounds.json, kept by
    process.py) -- all of NZ in one go is more than the public Overpass
    servers will answer. The bboxes overlap, so ways are de-duplicated
    by id."""
    with open("regc_bounds.json") as fh:
        councils = json.load(fh)
    seen, out = set(), []
    for name, c in councils.items():
        s, w, n, e = c["bbox"]
        q = (f'[out:csv(::id,name,::lat,::lon;true;"\\t")][timeout:300];'
             f'way[highway~"^({HIGHWAYS})$"][name]({s},{w},{n},{e});out center;')
        rows = overpass_csv(q, 300)
        for row in rows:
            if len(row) != 4 or not row[2] or row[0] in seen:
                continue
            seen.add(row[0])
            out.append((row[1].strip(), float(row[3]), float(row[2])))
        print(f"  {name}: {len(rows):,} ways")
    return out


class Grid:
    """Nearest-point lookup over a coarse lat/lng grid -- a quarter of a
    million roads against thousands of places is too slow by brute force."""
    CELL = 0.1

    def __init__(self, pts):
        self.pts, self.cells = pts, {}
        for i, (lng, lat) in enumerate(pts):
            self.cells.setdefault((int(lng // self.CELL), int(lat // self.CELL)), []).append(i)

    def nearest(self, lng, lat, max_rings=6):
        cx, cy = int(lng // self.CELL), int(lat // self.CELL)
        coslat = math.cos(math.radians(lat))
        best, best_d = -1, float("inf")
        for ring in range(max_rings + 1):
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if max(abs(dx), abs(dy)) != ring:
                        continue
                    for i in self.cells.get((cx + dx, cy + dy), ()):
                        plng, plat = self.pts[i]
                        d = ((plng - lng) * coslat) ** 2 + (plat - lat) ** 2
                        if d < best_d:
                            best, best_d = i, d
            # Anything in a further ring is at least `ring` cells away.
            if best >= 0 and math.sqrt(best_d) <= ring * self.CELL * coslat:
                break
        return best


def build(places, roads):
    # The same place is sometimes mapped twice (a node per tag set); keep one.
    seen, uniq = set(), []
    for p in places:
        key = (p[0].lower(), round(p[2], 2), round(p[3], 2))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    places = uniq
    grid = Grid([(p[2], p[3]) for p in places])
    # A suburb's town, for labels like "Parnell · Auckland".
    towns = [i for i, p in enumerate(places) if p[1] in "ct"]
    town_grid = Grid([(places[i][2], places[i][3]) for i in towns])
    out_places = []
    for p in places:
        parent = -1
        if p[1] in "sqn":
            j = town_grid.nearest(p[2], p[3])
            if j >= 0:
                parent = towns[j]
        out_places.append([p[0], p[1], round(p[2], 4), round(p[3], 4), parent])

    groups = {}
    for name, lng, lat in roads:
        pi = grid.nearest(lng, lat)
        g = groups.setdefault((name, pi), [0.0, 0.0, 0])
        g[0] += lng
        g[1] += lat
        g[2] += 1
    streets = [[name, pi, round(sx / n, 4), round(sy / n, 4)]
               for (name, pi), (sx, sy, n) in groups.items()]
    streets.sort(key=lambda s: (s[0], s[1]))
    return {"built": date.today().isoformat(), "places": out_places, "streets": streets}


def main():
    force = "--force" in sys.argv
    if not force and os.path.exists(OUT):
        age = (time.time() - os.path.getmtime(OUT)) / 86400
        try:
            with open(OUT) as fh:
                built = json.load(fh).get("built")
            age = (date.today() - date.fromisoformat(built)).days
        except Exception:                              # noqa: BLE001
            pass
        if age < MAX_AGE_DAYS:
            print(f"{OUT} is {age:.0f} days old, keeping it (--force to rebuild)")
            return
    try:
        places = fetch_places()
        roads = fetch_roads()
    except Exception as exc:                           # noqa: BLE001
        print(f"! Search index not refreshed, keeping the old one: {exc}")
        return
    if len(places) < 3000 or len(roads) < 100000:
        print(f"! Only {len(places)} places / {len(roads)} roads -- looks truncated, keeping the old file")
        return
    data = build(places, roads)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    print(f"Wrote {OUT}: {len(data['places']):,} places, {len(data['streets']):,} streets "
          f"({os.path.getsize(OUT) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
