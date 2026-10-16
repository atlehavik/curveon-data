#!/usr/bin/env python3
"""
Official speed limits for Curveon, one SQLite file per country, from free government road data:

  norway       Statens vegvesen, NVDB (object type 105 "Fartsgrense"), NLOD 2.0. Public roads only
               (E-, riks-, fylkes- and kommunal veg): on private and forest roads NVDB mostly holds
               the general limit, not a sign.
  finland      Väylävirasto, Digiroad (dr_nopeusrajoitus), CC BY 4.0. All roads.
  netherlands  NDW / Rijkswaterstaat, Wegkenmerkendatabase "Maximum snelheden" on the NWB, CC0.
  belgium      Flanders only: Agentschap Wegen en Verkeer, "Afgeleide snelheidsregimes" (derived from
               the signs in Verkeersborden.Vlaanderen on the Wegenregister), Modellicentie Gratis
               Hergebruik. Brussels and Wallonia publish none.

Not (yet): Sweden (Trafikverket's NVDB is CC0, but its API needs a registered key), Denmark
(Vejdirektoratet publishes state roads only).

The tablet shows the official limit for the road it is on, and falls back to OpenStreetMap's
maxspeed where there is none (other countries, private roads).

Norway's file also carries, from the same NVDB:
  refs      the road number of each stretch of public road (vegnett, E/R/F/K), for the route
            number signs in the next-turn card
  priority  priority roads (object type 596 "Forkjørsveg"), for the priority road sign
  giveway   give way and stop signs (object type 96 "Skiltplate", 202 Vikeplikt and 204 Stopp), for
            the give way sign while navigating

Format (read by app/.../SpeedLimits.kt):
  meta(key TEXT PRIMARY KEY, value TEXT)  source, licence, built
  cells(cell INTEGER PRIMARY KEY, data BLOB)      speed limits; value = km/h
  refs(cell INTEGER PRIMARY KEY, data BLOB)       road numbers; value = category * 1000000 + number,
                                                  category E=1, R(iksveg)=2, F(ylkesveg)=3, K(ommunal)=4
  priority(cell INTEGER PRIMARY KEY, data BLOB)   priority roads; value = 1
  giveway(cell INTEGER PRIMARY KEY, data BLOB)    give way (202) and stop (204) signs; each a run of
                                                  2 points 10 m apart on the road at the sign;
                                                  value = sign * 10 + kind:
                                                    0  for traffic along the road from the 1st point
                                                       towards the 2nd
                                                    1  for traffic coming out of a side road on the
                                                       right of 1st -> 2nd, 2 on the left, 3 either
    cell = (floor(lat*100) + 9000) * 100000 + (floor(lon*100) + 18000)  (0.01° squares)
    data = runs of road in that square, each: varint point count, varint value, then the points as
           zigzag varints in 1e-5° (lat, lon): the first relative to the square's corner, the rest
           to the previous point. A run that crosses squares is stored in each of them.

Usage: build_speedlimits.py <area id> <out.sqlite>   (needs pyproj for netherlands and belgium)
       SAMPLE=1 build_speedlimits.py …  fetches one page only, to try a source out
       FYLKER=46 build_speedlimits.py norway …  only these counties (comma-separated), to try it out
"""
import functools
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

NVDB = "https://nvdbapiles.atlas.vegvesen.no/vegobjekter/api/v4/vegobjekter/105"
HEADERS = {"X-Client": "curveon-data (github.com/atlehavik/curveon-data)", "Accept": "application/json"}


def get(url):
    for attempt in range(8):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=180) as r:
                return json.load(r)
        except Exception as e:  # the API resets connections now and then
            print(f"  retry {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f"gave up on {url}")


def nvdb_counties(url_of, value_of, what):
    """(value, [(lat, lon), …]) from an NVDB listing with geometry, a county at a time in parallel.
    [url_of]: county number -> first page's URL; [value_of]: object -> value, or None to skip it."""
    fylker = [f["nummer"] for f in get("https://nvdbapiles.atlas.vegvesen.no/omrader/api/v4/fylker")]
    if os.environ.get("FYLKER"):
        fylker = [f for f in fylker if str(f) in os.environ["FYLKER"].split(",")]

    def county(nr):
        url = url_of(nr)
        out = []
        while url:
            page = get(url)
            for o in page["objekter"]:
                value = value_of(o)
                wkt = o.get("geometri", {}).get("wkt", "")
                if not value or not wkt:
                    continue
                # NVDB's EPSG:4326 WKT is "lat lon [z]"; a MULTILINESTRING has one part per (…).
                for part in re.findall(r"\(([^()]+)\)", wkt):
                    pts = simplify([tuple(map(float, p.split()[:2])) for p in part.split(",")])
                    if len(pts) >= 2:
                        out.append((int(value), pts))
            url = page["metadata"].get("neste", {}).get("href") if page["metadata"]["returnert"] > 0 else None
            if os.environ.get("SAMPLE"):
                break
        print(f"  county {nr}: {len(out)} {what}", file=sys.stderr)
        return out

    with ThreadPoolExecutor(6) as pool:
        for part in pool.map(county, fylker):
            yield from part


def norway():
    """(km/h, [(lat, lon), …]) for every speed limit on Norway's public roads."""
    # Full geometry, simplified here: NVDB's own geometritoleranse collapses curved pieces (in
    # roundabouts and junctions) to a single point.
    return nvdb_counties(
        lambda nr: f"{NVDB}?vegsystemreferanse=E,R,F,K&fylke={nr}&inkluder=egenskaper,geometri&srid=4326&antall=1000",
        lambda o: next((e.get("verdi") for e in o["egenskaper"] if e.get("id") == 2021), None),
        "speed limits")


CATEGORIES = {"E": 1, "R": 2, "F": 3, "K": 4}


SIGNS = {7642: 202, 7643: 204}  # NVDB Skiltnummer enum id -> sign: 202 Vikeplikt, 204 Stopp
# Where a sign's face points (Ansiktsside, rettet mot) -> mode in the yield table: traffic along
# the road the sign is registered on, or traffic coming out of a side road onto it.
FACING = {"Trafikk i metreringsretning": "along", "Trafikk mot metreringsretning": "against",
          "Parallelt fra veg": "side", "Parallelt mot veg": "side"}


@functools.cache
def norway_signs():
    """Every give way and stop sign on a public road: (sign, mode, veglenkesekvens id, relative
    position, +1/-1 metering direction along the veglenkesekvens, side "H"/"V"/None in metering
    direction)."""
    fylker = [f["nummer"] for f in get("https://nvdbapiles.atlas.vegvesen.no/omrader/api/v4/fylker")]
    if os.environ.get("FYLKER"):
        fylker = [f for f in fylker if str(f) in os.environ["FYLKER"].split(",")]

    def county(nr):
        url = (f"{NVDB.rsplit('/', 1)[0]}/96?egenskap=({'%20OR%20'.join(f'5530={s}' for s in SIGNS)})"
               f"&fylke={nr}&inkluder=egenskaper,lokasjon&srid=4326&antall=1000")
        out = []
        while url:
            page = get(url)
            for o in page["objekter"]:
                props = {e.get("id"): e for e in o["egenskaper"]}
                sign = SIGNS.get(props.get(5530, {}).get("enum_id"))
                mode = FACING.get(props.get(1894, {}).get("verdi"))
                loc = o.get("lokasjon", {})
                st = (loc.get("stedfestinger") or [{}])[0]
                ref = (loc.get("vegsystemreferanser") or [{}])[0]
                metering = ref.get("strekning", {}).get("retning")
                if not sign or not mode or st.get("type") != "Punkt" or metering not in ("MED", "MOT"):
                    continue
                side = ref.get("metrertLokasjon", {}).get("sideposisjon")
                out.append((sign, mode, st["veglenkesekvensid"], st["relativPosisjon"],
                            1 if metering == "MED" else -1, side if side in ("H", "V") else None))
            url = page["metadata"].get("neste", {}).get("href") if page["metadata"]["returnert"] > 0 else None
            if os.environ.get("SAMPLE"):
                break
        print(f"  county {nr}: {len(out)} give way and stop signs", file=sys.stderr)
        return out

    with ThreadPoolExecutor(6) as pool:
        return [s for part in pool.map(county, fylker) for s in part]


# veglenkesekvens id -> [(startposisjon, sluttposisjon, [(lat, lon), …])] for those with a sign,
# kept by norway_refs while it reads the road network.
SEQUENCES = defaultdict(list)


def norway_yield():
    """Give way and stop signs as short stretches of road (see the format above)."""
    for sign, mode, seq, pos, metering, side in norway_signs():
        seg = next((s for s in SEQUENCES.get(seq, []) if s[0] <= pos <= s[1]), None)
        if not seg:
            continue  # on a road not in the network read (a separate carriageway, a ramp)
        start, end, pts = seg
        line = road_around(pts, (pos - start) / (end - start) if end > start else 0.0)
        if not line:
            continue
        a, b = line
        m = b if metering > 0 else (2 * a[0] - b[0], 2 * a[1] - b[1])  # a -> m: metering direction
        if mode == "side":
            yield sign * 10 + {"H": 1, "V": 2}.get(side, 3), [a, m]
        elif mode == "along":
            yield sign * 10, [a, m]
        else:
            yield sign * 10, [a, (2 * a[0] - m[0], 2 * a[1] - m[1])]


def road_around(pts, frac):
    """The point [frac] of the way along [pts] and one 10 m further on; None when [pts] has no length."""
    kx = math.cos(math.radians(pts[0][0])) * 111_320.0
    lens = [math.hypot((q[1] - p[1]) * kx, (q[0] - p[0]) * 110_574.0) for p, q in zip(pts, pts[1:])]
    total = sum(lens)
    if total <= 0:
        return None

    def at(d):
        d = max(0.0, min(total, d))
        for (p, q), l in zip(zip(pts, pts[1:]), lens):
            if d <= l and l > 0:
                t = d / l
                return p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])
            d -= l
        return pts[-1]

    d = frac * total
    a = at(d)
    if d + 10 <= total:
        return a, at(d + 10)
    c = at(d - 10)  # the sign sits near the end: the 10 m before it, carried on past
    return a, (2 * a[0] - c[0], 2 * a[1] - c[1])


def norway_refs():
    """(category * 1000000 + number, [(lat, lon), …]) for the road number of every stretch of public
    road that is open (fase V) and for vehicles (not footways and cycle paths)."""
    wanted = {s[2] for s in norway_signs()}

    def value(o):
        if o.get("veglenkesekvensid") in wanted:
            wkt = o.get("geometri", {}).get("wkt", "")
            m = re.search(r"\(([^()]+)\)", wkt)
            if m and o.get("sluttposisjon") is not None:
                SEQUENCES[o["veglenkesekvensid"]].append(
                    (o["startposisjon"], o["sluttposisjon"], [tuple(map(float, p.split()[:2])) for p in m.group(1).split(",")]))
        ref = o.get("vegsystemreferanse", {})
        vs = ref.get("vegsystem", {})
        if vs.get("fase") != "V" or ref.get("strekning", {}).get("trafikantgruppe") != "K":
            return None
        cat = CATEGORIES.get(vs.get("vegkategori"))
        return cat * 1000000 + vs["nummer"] if cat and vs.get("nummer") else None
    return nvdb_counties(
        lambda nr: f"https://nvdbapiles.atlas.vegvesen.no/vegnett/api/v4/veglenkesekvenser/segmentert"
                   f"?vegsystemreferanse=E,R,F,K&fylke={nr}&srid=4326&antall=1000",
        value, "road number stretches")


def norway_priority():
    """(1, [(lat, lon), …]) for every priority road (Forkjørsveg)."""
    return nvdb_counties(
        lambda nr: f"https://nvdbapiles.atlas.vegvesen.no/vegobjekter/api/v4/vegobjekter/596"
                   f"?fylke={nr}&inkluder=geometri&srid=4326&antall=1000",
        lambda o: 1, "priority roads")


def wfs(base, layer, value, crs=None, page=10000, sort=None, workers=6):
    """(km/h, [(lat, lon), …]) from a WFS 2.0 layer as GeoJSON, pages fetched in parallel.
    [crs]: the layer's own EPSG code when its EPSG:4326 output is rounded (converted here)."""
    count_url = f"{base}?service=WFS&version=2.0.0&request=GetFeature&typeNames={layer}&resultType=hits"
    with urllib.request.urlopen(urllib.request.Request(count_url, headers=HEADERS), timeout=300) as r:
        total = int(re.search(rb'numberMatched="(\d+)"', r.read()).group(1))
    to_wgs84 = None
    if crs:
        from pyproj import Transformer
        to_wgs84 = Transformer.from_crs(crs, 4326, always_xy=True)
    srs = "" if crs else "&srsName=EPSG:4326"
    order = f"&sortBy={sort}" if sort else ""

    def fetch(start):
        d = get(f"{base}?service=WFS&version=2.0.0&request=GetFeature&typeNames={layer}&outputFormat=application/json"
                f"{srs}{order}&count={page}&startIndex={start}")
        out = []
        for f in d["features"]:
            kmh = f["properties"].get(value)
            g = f.get("geometry")
            if not kmh or not g or int(kmh) <= 0:
                continue
            parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"] if g["type"] == "MultiLineString" else []
            for coords in parts:
                if to_wgs84:
                    xs, ys = to_wgs84.transform([c[0] for c in coords], [c[1] for c in coords])
                    coords = list(zip(xs, ys))
                pts = simplify([(c[1], c[0]) for c in coords])
                if len(pts) >= 2:
                    out.append((int(kmh), pts))
        print(f"  {layer}: {min(start + page, total)}/{total}", file=sys.stderr)
        return out

    starts = range(0, total, page)
    if os.environ.get("SAMPLE"):  # trying the builder out: one page only
        starts = starts[:1]
    with ThreadPoolExecutor(workers) as pool:
        for part in pool.map(fetch, starts):
            yield from part


def finland():
    return wfs("https://avoinapi.vaylapilvi.fi/vaylatiedot/digiroad/wfs", "digiroad:dr_nopeusrajoitus", "arvo", sort="id_pk")


def belgium():
    # Lambert 72: the EPSG:4326 output of this server is rounded to 3 decimals (~100 m).
    return wfs("https://opendata.apps.mow.vlaanderen.be/opendata-geoserver/awv/ows", "awv:Afgeleide_snelheidsregimes",
               "Snelheid", crs=31370, sort="Wegsegment_ID")


def simplify(pts, tol_m=1.5):
    """Douglas-Peucker in metres: keeps a road's shape to within [tol_m], drops points on straights."""
    if len(pts) < 3:
        return pts
    kx = math.cos(math.radians(pts[0][0])) * 111_320.0
    xy = [(lon * kx, lat * 110_574.0) for lat, lon in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (ax, ay), (bx, by) = xy[i], xy[j]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        far, far_d = -1, tol_m
        for k in range(i + 1, j):
            px, py = xy[k]
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
            d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > far_d:
                far, far_d = k, d
        if far >= 0:
            keep[far] = True
            stack += [(i, far), (far, j)]
    return [p for p, k in zip(pts, keep) if k]


WKD = "https://downloads.rijkswaterstaatdata.nl/wkd/geogegevens/Geopackage/Maximum%20Snelheden/"


def netherlands(work="work"):
    """The newest monthly WKD speed limits (a GeoPackage of the whole NWB in RD New, ~800 MB)."""
    import struct
    from pyproj import Transformer
    with urllib.request.urlopen(urllib.request.Request(WKD, headers=HEADERS), timeout=120) as r:
        months = re.findall(r'href="(\d\d)-(\d\d)-(\d{4})/"', r.read().decode())
    d, m, y = max(months, key=lambda x: (x[2], x[1], x[0]))
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, "Snelheden.gpkg")
    if not os.path.exists(path):
        url = f"{WKD}{d}-{m}-{y}/Snelheden.gpkg"
        print(f"  {url}", file=sys.stderr)
        for attempt in range(8):  # the server drops long downloads: resume them
            have = os.path.getsize(path + ".part") if os.path.exists(path + ".part") else 0
            try:
                req = urllib.request.Request(url, headers={**HEADERS, "Range": f"bytes={have}-"})
                with urllib.request.urlopen(req, timeout=300) as r, open(path + ".part", "ab") as out:
                    while chunk := r.read(1 << 20):
                        out.write(chunk)
                os.rename(path + ".part", path)
                break
            except Exception as e:
                print(f"  retry {attempt + 1}: {e}", file=sys.stderr)
                time.sleep(5)
        else:
            raise SystemExit("gave up on the WKD download")
    to_wgs84 = Transformer.from_crs(28992, 4326, always_xy=True)
    db = sqlite3.connect(path)
    for geom, kmh in db.execute("SELECT geom, MAXSHD FROM Snelheden"):
        if not geom or not kmh or not str(kmh).strip().isdigit():
            continue  # "NVT": paths and such without one
        # GeoPackage geometry: "GP", version, flags (envelope size in bits 1-3), srs id, envelope, WKB.
        flags = geom[3]
        env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 7, 0)
        wkb = geom[8 + env:]
        bo = "<" if wkb[0] == 1 else ">"
        gtype = struct.unpack(bo + "I", wkb[1:5])[0]
        dims = 3 if gtype in (1002, 0x80000002) else 4 if gtype == 3002 else 2
        if gtype % 1000 != 2 and gtype != 0x80000002:
            continue
        n = struct.unpack(bo + "I", wkb[5:9])[0]
        vals = struct.unpack(bo + "d" * (n * dims), wkb[9:9 + 8 * n * dims])
        xs, ys = to_wgs84.transform(vals[0::dims], vals[1::dims])
        pts = simplify(list(zip(ys, xs)))
        if len(pts) >= 2:
            yield int(kmh), pts


SOURCES = {
    "norway": (norway, "Statens vegvesen, NVDB", "NLOD 2.0 (https://data.norge.no/nlod/no/2.0)"),
    "finland": (finland, "Väylävirasto, Digiroad", "CC BY 4.0"),
    "netherlands": (netherlands, "NDW / Rijkswaterstaat, Wegkenmerkendatabase", "CC0 1.0"),
    "belgium": (belgium, "Bron: MOW - AWV (Flanders)", "Modellicentie Gratis Hergebruik v1.0"),
}


def cell_of(lat, lon):
    return (math.floor(lat * 100) + 9000) * 100000 + (math.floor(lon * 100) + 18000)


def varint(n, out):
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def zigzag(n):
    return (n << 1) ^ (n >> 63)


# Further tables a country's file carries besides the speed limits (see the format above).
EXTRA = {"norway": {"refs": norway_refs, "priority": norway_priority, "giveway": norway_yield}}


def build(area, out_path):
    fetch, source, licence = SOURCES[area]
    db = sqlite3.connect(out_path)
    db.execute("DROP TABLE IF EXISTS meta")
    db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    write_runs(db, "cells", fetch())
    for table, extra in EXTRA.get(area, {}).items():
        write_runs(db, table, extra())
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("source", source), ("licence", licence),
                                                      ("built", time.strftime("%Y-%m-%d"))])
    db.commit()
    db.execute("VACUUM")
    db.close()


def write_runs(db, table, items):
    """(value, [(lat, lon), …]) into [table], cut into 0.01° squares (see the format above)."""
    runs = defaultdict(list)  # cell -> [(value, [(lat5, lon5), …])]
    for kmh, pts in items:
        q = [(round(lat * 1e5), round(lon * 1e5)) for lat, lon in pts]
        # Each segment goes to every square its box touches; consecutive segments in one square
        # stay one run.
        open_runs = {}
        for i in range(len(q) - 1):
            a, b = q[i], q[i + 1]
            cells = {cell_of(la / 1e5, lo / 1e5) for la in range(min(a[0], b[0]), max(a[0], b[0]) + 1, 1000)
                     for lo in range(min(a[1], b[1]), max(a[1], b[1]) + 1, 1000)}
            cells |= {cell_of(a[0] / 1e5, a[1] / 1e5), cell_of(b[0] / 1e5, b[1] / 1e5)}
            for c in cells:
                run = open_runs.get(c)
                if run is not None and run[-1] == a:
                    run.append(b)
                else:
                    run = [a, b]
                    open_runs[c] = run
                    runs[c].append((kmh, run))
    db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute(f"CREATE TABLE {table}(cell INTEGER PRIMARY KEY, data BLOB NOT NULL)")
    for c, rs in runs.items():
        lat0 = (c // 100000 - 9000) * 1000
        lon0 = (c % 100000 - 18000) * 1000
        out = bytearray()
        for kmh, pts in rs:
            varint(len(pts), out)
            varint(kmh, out)
            plat, plon = lat0, lon0
            for la, lo in pts:
                varint(zigzag(la - plat), out)
                varint(zigzag(lo - plon), out)
                plat, plon = la, lo
        db.execute(f"INSERT INTO {table} VALUES (?, ?)", (c, bytes(out)))
    print(f"{table}: {len(runs)} squares", file=sys.stderr)


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
