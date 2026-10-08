#!/usr/bin/env python3
"""
Builds the offline destination search index (SQLite + FTS4) for Curveon from an OSM extract.

    python build_search_index.py norway-latest.osm.pbf norway-search.sqlite
    python build_search_index.py a.osm.pbf,b.osm.pbf out.sqlite     # several extracts into one index

Contents
  entries      places (towns, villages, …), useful POIs (fuel, cafés, viewpoints, campsites,
               ferry terminals, mountain passes, …) and streets (one row per street and town)
  entries_fts  FTS4 index over a normalised "name + area" string
  housenumbers house numbers per street row, so "Storgata 12 Lillehammer" resolves to a door
  cameras      fixed speed cameras (highway=speed_camera) for warnings while riding

Text is normalised (lowercase, æ→ae, ø→o, å→a, accents stripped) so the app can use the plain
FTS4 tokenizer, which every Android SQLite has. The app applies the same normalisation to queries.
"""
import math
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict

import osmium

PLACE_RANK = {
    "city": 100, "town": 90, "village": 70, "suburb": 60, "borough": 60, "quarter": 50,
    "hamlet": 45, "neighbourhood": 40, "island": 40, "locality": 30, "isolated_dwelling": 20,
}
POI_TAGS = {
    ("amenity", "fuel"): ("fuel", 55),
    ("amenity", "charging_station"): ("charging", 30),
    ("amenity", "cafe"): ("cafe", 45),
    ("amenity", "restaurant"): ("restaurant", 40),
    ("amenity", "fast_food"): ("fast_food", 35),
    ("amenity", "pub"): ("pub", 30),
    ("amenity", "ferry_terminal"): ("ferry", 60),
    ("amenity", "parking"): ("parking", 15),
    ("tourism", "viewpoint"): ("viewpoint", 60),
    ("tourism", "camp_site"): ("camp_site", 50),
    ("tourism", "hotel"): ("hotel", 45),
    ("tourism", "motel"): ("hotel", 45),
    ("tourism", "guest_house"): ("hotel", 40),
    ("tourism", "hostel"): ("hotel", 40),
    ("tourism", "alpine_hut"): ("hut", 40),
    ("tourism", "wilderness_hut"): ("hut", 30),
    ("tourism", "attraction"): ("attraction", 55),
    ("tourism", "museum"): ("museum", 45),
    ("tourism", "picnic_site"): ("picnic", 25),
    ("shop", "motorcycle"): ("motorcycle_shop", 50),
    ("shop", "supermarket"): ("supermarket", 35),
    ("shop", "convenience"): ("shop", 25),
    ("natural", "peak"): ("peak", 35),
    ("mountain_pass", "yes"): ("pass", 65),
}
TRANS = str.maketrans({"æ": "ae", "ø": "o", "å": "a", "ß": "ss", "đ": "d", "ŋ": "n", "ŧ": "t"})


def normalise(s: str) -> str:
    s = s.lower().translate(TRANS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in s).strip()


def centroid(nodes):
    lat = lon = 0.0
    n = 0
    for nd in nodes:
        if nd.location.valid():
            lat += nd.location.lat
            lon += nd.location.lon
            n += 1
    return (lat / n, lon / n) if n else None


class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.places = []          # (name, kind, rank, lat, lon)
        self.pois = []            # (name, kind, rank, lat, lon, ele)
        self.addresses = defaultdict(list)  # (street, city/postcode area) -> [(number, lat, lon)]
        self.cameras = []         # (lat, lon, maxspeed or None)
        self.count = 0

    def tick(self):
        self.count += 1
        if self.count % 2_000_000 == 0:
            print(f"  {self.count // 1_000_000}M objects, {len(self.places)} places, "
                  f"{len(self.pois)} pois, {len(self.addresses)} streets", flush=True)

    def handle(self, tags, lat, lon):
        if tags.get("highway") == "speed_camera":
            ms = tags.get("maxspeed")
            self.cameras.append((lat, lon, int(ms) if ms and ms.isdigit() else None))
        name = tags.get("name")
        place = tags.get("place")
        if name and place in PLACE_RANK:
            self.places.append((name, place, PLACE_RANK[place], lat, lon))
        elif name:
            for (k, v), (kind, rank) in POI_TAGS.items():
                if tags.get(k) == v:
                    label = name
                    if kind == "fuel" and tags.get("brand") and tags.get("brand").lower() not in name.lower():
                        label = f"{tags.get('brand')} {name}"
                    self.pois.append((label, kind, rank, lat, lon, tags.get("ele")))
                    break
        elif tags.get("amenity") == "fuel" and tags.get("brand"):
            self.pois.append((tags.get("brand"), "fuel", 50, lat, lon, None))
        street = tags.get("addr:street") or tags.get("addr:place")
        number = tags.get("addr:housenumber")
        if street and number:
            area = tags.get("addr:city") or tags.get("addr:postcode") or ""
            self.addresses[(street, area, tags.get("addr:postcode") or "")].append((number, lat, lon))

    def node(self, n):
        self.tick()
        if len(n.tags) and n.location.valid():
            self.handle(n.tags, n.location.lat, n.location.lon)

    def way(self, w):
        self.tick()
        if not len(w.tags):
            return
        tags = w.tags
        if not (tags.get("name") or tags.get("addr:housenumber")):
            return
        c = centroid(w.nodes)
        if c:
            self.handle(tags, c[0], c[1])


class NearestPlace:
    """Grid lookup of the nearest town/village, used as the 'area' label for POIs."""

    def __init__(self, places):
        self.grid = defaultdict(list)
        for name, kind, rank, lat, lon in places:
            if rank >= 45 and kind not in ("island",):
                self.grid[(int(lat * 10), int(lon * 5))].append((name, rank, lat, lon))

    def find(self, lat, lon):
        best, best_score = "", 1e18
        gy, gx = int(lat * 10), int(lon * 5)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for name, rank, plat, plon in self.grid.get((gy + dy, gx + dx), ()):
                    d = (plat - lat) ** 2 + ((plon - lon) * math.cos(math.radians(lat))) ** 2
                    score = d / (1 + rank / 50)  # prefer real towns over hamlets at similar distance
                    if score < best_score:
                        best, best_score = name, score
        return best


def main(pbfs, out):
    t0 = time.time()
    c = Collector()
    for pbf in pbfs.split(","):
        print(f"Reading {pbf} …", flush=True)
        c.apply_file(pbf, locations=True, idx="flex_mem")
    print(f"Read in {time.time() - t0:.0f}s: {len(c.places)} places, {len(c.pois)} pois, "
          f"{len(c.addresses)} streets, {len(c.cameras)} speed cameras", flush=True)

    near = NearestPlace(c.places)
    db = sqlite3.connect(out)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        DROP TABLE IF EXISTS entries; DROP TABLE IF EXISTS entries_fts; DROP TABLE IF EXISTS housenumbers;
        DROP TABLE IF EXISTS meta;
        CREATE TABLE entries(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                             area TEXT, rank INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL);
        CREATE VIRTUAL TABLE entries_fts USING fts4(search);
        CREATE TABLE housenumbers(entry_id INTEGER NOT NULL, number TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        DROP TABLE IF EXISTS cameras;
        CREATE TABLE cameras(lat REAL NOT NULL, lon REAL NOT NULL, maxspeed INTEGER);
    """)
    rows = []
    for name, kind, rank, lat, lon in c.places:
        area = "" if rank >= 90 else near.find(lat, lon)
        rows.append((name, kind, area if area != name else "", rank, lat, lon, None))
    for name, kind, rank, lat, lon, ele in c.pois:
        rows.append((name, kind, near.find(lat, lon), rank, lat, lon, None))
    for (street, area, postcode), numbers in c.addresses.items():
        lat = sum(n[1] for n in numbers) / len(numbers)
        lon = sum(n[2] for n in numbers) / len(numbers)
        town = area if area and not area.isdigit() else near.find(lat, lon)
        label = f"{postcode} {town}".strip() if postcode else town
        rows.append((street, "street", label, 10, lat, lon, numbers))

    cur = db.cursor()
    for i, (name, kind, area, rank, lat, lon, numbers) in enumerate(rows, start=1):
        cur.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?)", (i, name, kind, area, rank, round(lat, 6), round(lon, 6)))
        cur.execute("INSERT INTO entries_fts(docid, search) VALUES (?, ?)", (i, normalise(f"{name} {area or ''}")))
        if numbers:
            cur.executemany("INSERT INTO housenumbers VALUES (?,?,?,?)",
                            [(i, num, round(la, 6), round(lo, 6)) for num, la, lo in numbers])
    cur.executemany("INSERT INTO cameras VALUES (?,?,?)", [(round(a, 6), round(b, 6), m) for a, b, m in c.cameras])
    cur.execute("INSERT INTO meta VALUES ('built', ?)", (time.strftime("%Y-%m-%d"),))
    cur.execute("INSERT INTO meta VALUES ('schema', '2')")
    cur.execute("INSERT INTO meta VALUES ('source', ?)", (",".join(p.split("/")[-1] for p in pbfs.split(",")),))
    db.commit()
    print("Indexing …", flush=True)
    db.executescript("""
        CREATE INDEX housenumbers_entry ON housenumbers(entry_id);
        CREATE INDEX entries_kind_lat ON entries(kind, lat);
        CREATE INDEX cameras_lat ON cameras(lat);
        INSERT INTO entries_fts(entries_fts) VALUES('optimize');
        VACUUM;
    """)
    db.close()
    print(f"Wrote {out}: {len(rows)} entries in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
