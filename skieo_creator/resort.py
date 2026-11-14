"""Real trails and lifts from OpenStreetMap, built into a map's starting save.

OpenStreetMap maps ski resorts as aerialway ways (chair lifts, gondolas, cable cars, drag lifts, with
aerialway:occupancy / aerialway:detachable) and piste ways and areas (piste:type=downhill, piste:difficulty,
piste:name). This module fetches them for the map's square (Overpass API) and turns them into the game's own save
records, so a new game on the map starts with the real resort already built:

  lifts   -> LiftSaveData: the closest stock lift (a detachable six -> High-Speed Six, a fixed quad -> fixed quad,
             a gondola -> Gondola, a cable car -> Aerial Tram; drag lifts, which the game doesn't have, -> fixed
             chairs), straight from the bottom station to the top, towers spaced and sized like the game places them,
             a queue line, hubs shared by terminals close together (and with the ticket office)
  trails  -> TrailInfo: named, with the player-rated difficulty from piste:difficulty, as the game's own chain of
             quadratic curves (~100 m each) along the piste; piste areas become bowls
  trees   -> none on the trails or under the lifts (clear_mask() for the forest), and the game's cleared zones in the
             save, as the game writes them when a trail or lift is built
The rules (tower spacing, bowl centre and radius, zones, hours) were read off saves the game wrote. The record layouts
come from data/resort_templates.bin (packaging/make_resort_templates.py): the game's starting save has no lift or trail.
"""
import json
import logging
import math
import re
import struct
import time
import urllib.parse
from collections import Counter, defaultdict

import numpy as np
from PIL import Image, ImageDraw

from . import geo, net, paths
from .engine import gvas_tree as G

log = logging.getLogger(__name__)

OVERPASS = ('https://overpass-api.de/api/interpreter', 'https://overpass.private.coffee/api/interpreter',
            'https://maps.mail.ru/osm/tools/overpass/api/interpreter')
QUERY = """[out:json][timeout:120];
(
  way["piste:type"="downhill"]({s},{w},{n},{e});
  relation["piste:type"="downhill"]({s},{w},{n},{e});
  way["aerialway"]({s},{w},{n},{e});
  node["aerialway"="station"]({s},{w},{n},{e});
);
out body;
>;
out skel qt;"""
CACHE = paths.APP_DIR / 'cache' / 'osm'
CACHE_DAYS = 7

# the game's stock lifts (DT_BuildingEconomy row): blueprint, name (as in the build menu), build cost, daily operating
# cost, tower spacing (m), tower height range (cm), tree clearing radius (cm)
LIFTS = {
    'OnePersonFixed': ('/Game/Blueprints/Lifts/BP_FixedChair1', '1-Person Fixed Grip', 12500, 60, 50, 1350, 3000, 1223),
    'TwoPersonFixed': ('/Game/Blueprints/Lifts/BP_FixedChair2', '2-Person Fixed Grip', 39000, 195, 50, 1100, 3000, 1250),
    'FourPersonFixed': ('/Game/Blueprints/Lifts/BP_FixedChair4', '4-Person Fixed Grip', 118000, 590, 50, 1150, 3000, 1300),
    'HighSpeedQuad': ('/Game/Blueprints/Lifts/BP_HighSpeedQuad', 'High-Speed Quad', 381000, 1905, 50, 1300, 3039, 1300),
    'HighSpeedSix': ('/Game/Blueprints/Lifts/BP_HighSpeedSix', 'High-Speed Six', 1202000, 6000, 50, 1100, 3039, 1400),
    'Gondola': ('/Game/Blueprints/Lifts/BP_Gondola', 'Gondola', 4024000, 20100, 50, 2150, 3039, 1500),
    'AerialTram': ('/Game/Blueprints/Lifts/BP_AerialTram', 'Aerial Tram', 14199000, 71000, 240, 4451, 4451, 1600),
}
SURFACE = {'t-bar': 'TwoPersonFixed', 'j-bar': 'OnePersonFixed', 'platter': 'OnePersonFixed',
           'drag_lift': 'OnePersonFixed', 'rope_tow': 'OnePersonFixed', 'magic_carpet': 'OnePersonFixed'}
KIND_NAME = {'chair_lift': 'Chair', 'gondola': 'Gondola', 'cable_car': 'Tram', 'mixed_lift': 'Chondola',
             't-bar': 'T-Bar', 'j-bar': 'J-Bar', 'platter': 'Platter', 'drag_lift': 'Surface Lift',
             'rope_tow': 'Rope Tow', 'magic_carpet': 'Carpet'}
DIFFICULTY = {'novice': 'Beginner', 'easy': 'Beginner', 'intermediate': 'Intermediate', 'advanced': 'Expert',
              'expert': 'Pro', 'extreme': 'Pro', 'freeride': 'Pro'}
RUN_NAME = {'Beginner': 'Green Run', 'Intermediate': 'Blue Run', 'Expert': 'Black Run', 'Pro': 'Double Black'}
WIDTH_M = {'Beginner': 40.0, 'Intermediate': 36.0, 'Expert': 30.0, 'Pro': 26.0}
EXPRESS = re.compile(r'express|high[ -]?speed|\bhs[q6]?\b|detach|bubble', re.I)

MIN_LIFT_M = 60.0                               # shorter lifts (most magic carpets) are left out
MAX_LIFT_M = 7500.0
EDGE_M = 60.0                                   # lift stations must be this far inside the square
CURVE_M = 100.0                                 # trail curve length (the game's are 20-850 m, half 80-150 m)
MIN_TRAIL_M = 120.0
MIN_UNNAMED_M = 350.0
STATION_M = 120.0                               # an OSM station this close to a terminal names its hub
HUB_M = 150.0                                   # terminals this close share a hub (the game: 106 m and 110 m seen)
OFFICE_HUB_M = 200.0                            # ... and with the ticket office (184 m seen)
OPEN_HOURS = (9.0, 21.0)                        # what the current game version gives new lifts


class ResortError(Exception):
    pass


# ---------------------------------------------------------------- fetching

def _bbox(lat, lon, size_m, margin=1.03):
    g = geo.LocalGrid(lat, lon)
    h = size_m / 2.0 * margin
    la, lo = g.to_latlon(np.array([-h, h, h, -h]), np.array([h, h, -h, -h]))
    return float(la.min()), float(lo.min()), float(la.max()), float(lo.max())


def _complete(raw):
    """raw if it is a whole Overpass answer; a timed-out or out-of-memory query still answers 200 with what it had so
    far and says so in 'remark'"""
    d = json.loads(raw)
    remark = str(d.get('remark') or '')
    if 'elements' not in d or re.search(r'error|timed out|out of memory', remark, re.I):
        raise ValueError(remark[:160] or 'no elements in the answer')
    return raw


def fetch(lat, lon, size_m, cancel=net.NEVER, progress=lambda f, t: None):
    """raw Overpass JSON (bytes) for the map square, cached CACHE_DAYS days"""
    s, w, n, e = _bbox(lat, lon, size_m)
    f = CACHE / ('%.5f_%.5f_%.5f_%.5f.json' % (s, w, n, e))
    if f.is_file() and time.time() - f.stat().st_mtime < CACHE_DAYS * 86400:
        try:
            return _complete(f.read_bytes())
        except (OSError, ValueError):
            pass
    q = urllib.parse.quote(QUERY.format(s=s, w=w, n=n, e=e))
    last = None
    for i, url in enumerate(OVERPASS):
        progress(0.1 + 0.2 * i, 'Downloading the trails and lifts from OpenStreetMap...')
        try:
            raw = _complete(net.get(url + '?data=' + q, timeout=150, retries=1, cancel=cancel, per_host=1,
                                    accept='application/json'))
            paths.atomic_write(f, raw)
            return raw
        except (net.NetError, ValueError) as ex:
            last = ex
            log.warning('Overpass %s failed: %s', url, ex)
    raise ResortError('The OpenStreetMap servers didn\'t answer right now (%s). They are often busy: try again in a '
                      'few minutes.' % str(last).split(' Check your')[0].rstrip('.'))


# ---------------------------------------------------------------- reading OSM

def _num(v):
    m = re.match(r'\s*(\d+(?:\.\d+)?)', str(v or ''))
    return float(m.group(1)) if m else None


def _clean(s):
    return re.sub(r'\s+', ' ', s).strip() if s else None


def lift_row(t, europe):
    """OSM aerialway tags -> (game lift row, what the real lift is) or (None, why it is left out)"""
    kind = t.get('aerialway', '')
    if t.get('disused') == 'yes' or t.get('abandoned') == 'yes' or kind in ('construction', 'proposed', 'disused'):
        return None, 'not running'
    occs = [_num(x) for x in str(t.get('aerialway:occupancy', '')).split(';')]
    occs = [v for v in occs if v]
    occ = int(max(occs)) if occs else None
    cap = _num(t.get('aerialway:capacity'))
    if kind == 'cable_car':
        return 'AerialTram', 'cable car'
    if kind == 'gondola':
        return ('AerialTram', '%d-person cabins' % occ) if occ and occ >= 20 else ('Gondola', 'gondola')
    if kind == 'mixed_lift':
        return 'HighSpeedSix', 'chairs and cabins'
    if kind in SURFACE:
        return SURFACE[kind], kind.replace('_', ' ')
    if kind != 'chair_lift':
        return None, 'aerialway=%s' % (kind or '?')
    det = t.get('aerialway:detachable')
    name = ' '.join(t.get(k, '') for k in ('name', 'name:en', 'description'))
    if det in ('yes', 'no'):
        detachable = det == 'yes'
    elif EXPRESS.search(name) or t.get('aerialway:bubble') == 'yes' or t.get('aerialway:heating') == 'yes':
        detachable = True
    elif occ is not None and occ >= 6:
        detachable = True
    elif occ in (None, 4) and cap:
        detachable = cap >= 2200
    else:
        detachable = occ == 4 and europe            # European quads are mostly detachable, North American ones often not
    if occ is None:
        occ = (6 if cap and cap >= 3000 else 4) if detachable else \
            (2 if not cap else 1 if cap < 700 else 2 if cap < 1300 else 3 if cap < 1700 else 4)
    if occ <= 1:
        return 'OnePersonFixed', 'single chair'
    if occ == 2:
        return 'TwoPersonFixed', 'double chair'
    if occ == 3:
        return ('HighSpeedQuad', 'detachable triple') if detachable else ('FourPersonFixed', 'triple chair')
    if occ == 4:
        return ('HighSpeedQuad', 'detachable quad') if detachable else ('FourPersonFixed', 'fixed quad')
    return 'HighSpeedSix', '%d-seat chair' % occ


def read(osm):
    nodes, ways, rels = {}, {}, []
    for el in osm.get('elements', ()):                   # 'out skel' copies (no tags) never replace the tagged ones
        if el['type'] == 'node':
            if el['id'] not in nodes or el.get('tags'):
                nodes[el['id']] = (el['lat'], el['lon'], el.get('tags', {}))
        elif el['type'] == 'way':
            if el['id'] not in ways or el.get('tags'):
                ways[el['id']] = (el.get('nodes', []), el.get('tags', {}))
        elif el['type'] == 'relation':
            rels.append(el)
    return nodes, ways, rels


# ---------------------------------------------------------------- terrain

class Terrain:
    """the map's heightmap; positions in metres from its north-west corner (x east, y south), like trees.plant"""

    def __init__(self, H, fr, lat, lon, size_m):
        self.H = np.asarray(H, np.float64)
        self.sp, self.sc, self.half, self.size = fr.sp, fr.sc, fr.half, size_m
        self.grid = geo.LocalGrid(lat, lon)
        self.n = self.H.shape[0]

    def xy(self, lat, lon):
        e, n = self.grid.to_xy(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
        return np.asarray(e) + self.half, self.half - np.asarray(n)

    def latlon(self, x, y):
        return self.grid.to_latlon(np.asarray(x, np.float64) - self.half, self.half - np.asarray(y, np.float64))

    def z(self, x, y):
        """ground height in world cm at corner metres x, y (bilinear)"""
        c = np.clip(np.asarray(x, np.float64) / self.sp, 0, self.n - 1.001)
        r = np.clip(np.asarray(y, np.float64) / self.sp, 0, self.n - 1.001)
        c0, r0 = np.floor(c).astype(int), np.floor(r).astype(int)
        fc, fr = c - c0, r - r0
        H = self.H
        h = (H[r0, c0] * (1 - fc) * (1 - fr) + H[r0, c0 + 1] * fc * (1 - fr) + H[r0 + 1, c0] * (1 - fc) * fr +
             H[r0 + 1, c0 + 1] * fc * fr)
        return (h + 32768.0) * self.sc * 100.0

    def world(self, x, y, z=None):
        return dict(X=(float(x) - self.half) * 100.0, Y=(float(y) - self.half) * 100.0,
                    Z=float(self.z(x, y) if z is None else z))

    def inside(self, x, y, margin):
        return margin <= x <= self.size - margin and margin <= y <= self.size - margin


# ---------------------------------------------------------------- geometry helpers

def _cum(P):
    return np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(P, axis=0).T))])


def _length(P):
    return float(_cum(P)[-1]) if len(P) > 1 else 0.0


def _resample(P, step):
    """points every ~step metres along the polyline P (n x 2), both ends kept"""
    d = _cum(P)
    k = max(1, int(round(d[-1] / step)))
    t = np.linspace(0, d[-1], k + 1)
    return np.column_stack([np.interp(t, d, P[:, 0]), np.interp(t, d, P[:, 1])])


def _at(P, frac):
    d = _cum(P)
    return np.array([np.interp(frac * d[-1], d, P[:, 0]), np.interp(frac * d[-1], d, P[:, 1])])


def _inside_runs(P, terrain, margin):
    """the parts of P (resampled at 10 m) inside the square"""
    Q = _resample(P, 10.0)
    runs, cur = [], []
    for q in Q:
        if terrain.inside(q[0], q[1], margin):
            cur.append(q)
        elif cur:
            runs.append(np.array(cur))
            cur = []
    if cur:
        runs.append(np.array(cur))
    return [r for r in runs if len(r) > 1]


def _bezier(p0, c, p2, step_cm):
    """points along a quadratic curve (world dicts) every ~step_cm: k x 2 array of X, Y"""
    a, b, e = (np.array([p['X'], p['Y']]) for p in (p0, c, p2))
    k = max(1, math.ceil(math.hypot(*(e - a)) / step_cm))
    t = np.linspace(0, 1, k + 1)[:, None]
    return (1 - t) ** 2 * a + 2 * (1 - t) * t * b + t * t * e


# ---------------------------------------------------------------- planning

def upkeep(l):
    """what the game charges for the lift every night, open or not: its daily operating cost x (towers + 4) / 8 (read
    off the game's ledger for 19 lifts)"""
    return int(LIFTS[l['row']][3] * (len(l['towers']) + 4) / 8.0 + 0.5)        # halves up, like the game


def plan(osm, terrain, skip=()):
    """OSM data -> dict(lifts, trails, hubs, skipped): positions in corner metres / world cm, ready for the save.
    skip: OSM ids of lifts to leave out (the player's choice)"""
    nodes, ways, rels = read(osm)
    europe = -25.0 <= terrain.grid.lon0 <= 45.0 and terrain.grid.lat0 >= 34.0
    skipped = Counter()

    def poly(ids):
        pts = [nodes[i] for i in ids if i in nodes]
        if len(pts) < 2:
            return None
        x, y = terrain.xy([p[0] for p in pts], [p[1] for p in pts])
        P = np.column_stack([x, y])
        P = P[np.concatenate([[True], np.hypot(*np.diff(P, axis=0).T) > 0.05])]
        return P if len(P) > 1 else None

    stations = []                                      # (x, y, name) of named aerialway stations
    for la, lo, t in nodes.values():
        if t.get('aerialway') == 'station' and t.get('name'):
            x, y = terrain.xy(la, lo)
            stations.append((float(x), float(y), _clean(t['name'])))
    for ids, t in ways.values():
        if t.get('aerialway') == 'station' and t.get('name'):
            P = poly(ids)
            if P is not None:
                stations.append((float(P[:, 0].mean()), float(P[:, 1].mean()), _clean(t['name'])))

    # ---- lifts: straight from the bottom station to the top one
    lifts = []
    for wid, (ids, t) in ways.items():
        if 'aerialway' not in t or t['aerialway'] in ('station', 'pylon'):
            continue
        row, why = lift_row(t, europe)
        if row is None:
            skipped['lift (%s)' % why] += 1
            continue
        P = poly(ids)
        if P is None:
            skipped['lift without a line'] += 1
            continue
        a, b = P[0], P[-1]
        if not (terrain.inside(a[0], a[1], EDGE_M) and terrain.inside(b[0], b[1], EDGE_M)):
            skipped['lift reaching outside the square'] += 1
            continue
        L = math.hypot(*(b - a))
        if not MIN_LIFT_M <= L <= MAX_LIFT_M:
            skipped['lift too %s' % ('short' if L < MIN_LIFT_M else 'long')] += 1
            continue
        if terrain.z(*a) > terrain.z(*b):
            a, b = b, a
        lifts.append(dict(osm=wid, name=_clean(t.get('name') or t.get('name:en') or t.get('ref')), row=row, why=why,
                          kind=t['aerialway'], a=a, b=b))
    lifts.sort(key=lambda l: -math.hypot(*(l['b'] - l['a'])))
    unnamed = Counter()
    for l in lifts:
        if not l['name']:
            k = KIND_NAME.get(l['kind'], 'Lift')
            unnamed[k] += 1
            l['name'] = '%s %d' % (k, unnamed[k])
        _lift_geometry(l, terrain)
    left_out = [l for l in lifts if l['osm'] in set(skip)]
    if left_out:
        skipped['lift left out by you'] = len(left_out)
        lifts = [l for l in lifts if l['osm'] not in set(skip)]

    # ---- hubs: terminals close together share one, named after an OSM station nearby or after the lift
    hubs = []
    for l in lifts:
        for end, label in (('bottom', 'Base'), ('top', 'Summit')):
            x, y = l['wp'][0][:2] if end == 'bottom' else l['wp'][-1][:2]
            hub = next((h for h in hubs if math.hypot(h['x'] - x, h['y'] - y) < HUB_M and h['id'] != l.get('hub_bottom')),
                       None)                       # (a short lift's top is still another hub than its bottom)
            if hub is None:
                st = min(stations, key=lambda s: math.hypot(s[0] - x, s[1] - y), default=None)
                near = st and math.hypot(st[0] - x, st[1] - y) < STATION_M and st[2] not in {h['name'] for h in hubs}
                hub = dict(id=G.new_guid(), x=x, y=y, name=st[2] if near else '%s %s' % (l['name'], label))
                hubs.append(hub)
            l['hub_' + end] = hub['id']

    # ---- trails
    rel_tags = {}
    for r in rels:
        if r.get('tags', {}).get('type') == 'multipolygon':
            continue                                   # an area: its member ways are its outline, not runs
        for m in r.get('members', ()):
            if m.get('type') == 'way':
                rel_tags.setdefault(m['ref'], r.get('tags', {}))
    lines, areas = [], []
    for wid, (ids, t) in ways.items():
        rt = rel_tags.get(wid, {})
        if t.get('piste:type') != 'downhill' and rt.get('piste:type') != 'downhill':
            continue
        if 'aerialway' in t:
            continue
        name = _trail_name(t) or _trail_name(rt)
        diff = DIFFICULTY.get(str(t.get('piste:difficulty') or rt.get('piste:difficulty') or '').strip().lower())
        tags = dict(rt, **t)
        if tags.get('piste:grooming') == 'backcountry' and not diff:
            skipped['backcountry route without a grade'] += 1
            continue
        w = dict(osm=wid, ids=ids, name=name, diff=diff, tags=tags)
        (areas if len(ids) > 3 and ids[0] == ids[-1] and t.get('area') != 'no' else lines).append(w)
    for r in rels:                                     # piste areas mapped as multipolygons: one area per outer ring
        t = r.get('tags', {})
        if t.get('type') != 'multipolygon' or t.get('piste:type') != 'downhill':
            continue
        diff = DIFFICULTY.get(str(t.get('piste:difficulty') or '').strip().lower())
        if t.get('piste:grooming') == 'backcountry' and not diff:
            skipped['backcountry route without a grade'] += 1
            continue
        rings = _rings([m['ref'] for m in r.get('members', ()) if m.get('type') == 'way' and m.get('role', '') in ('outer', '')], ways)
        if not rings:
            skipped['trail area with an open outline'] += 1
        areas += [dict(osm=r['id'], ids=ring, name=_trail_name(t), diff=diff, tags=t) for ring in rings]
    trails = _chains(lines, poly, terrain, skipped)
    named = {tr['name'].lower() for tr in trails if tr['name']}
    for a in areas:
        if a['name'] and a['name'].lower() in named:
            continue                                       # the same run, also drawn as a line
        b = _bowl(a, poly, terrain)
        if b is None:
            skipped['trail area too small or not inside the square'] += 1
        elif not a['name'] and any(t['shape'] == 'Corridor' and _inside(b['P'], t['P']).sum() >= 10 for t in trails):
            skipped['unnamed piste area drawn round a run'] += 1     # >= 100 m of a run inside it: the same piste
        else:
            trails.append(b)
    k = Counter()
    for tr in sorted(trails, key=lambda t: -t['length_cm']):
        if not tr['name']:
            k[tr['difficulty']] += 1
            tr['name'] = '%s %d' % (RUN_NAME[tr['difficulty']], k[tr['difficulty']])
    return dict(lifts=lifts, trails=trails, hubs=hubs, skipped=dict(skipped))


def _trail_name(t):
    return _clean(t.get('piste:name') or t.get('name') or t.get('piste:ref') or t.get('ref'))


def _rings(way_ids, ways):
    """a multipolygon's outer ways joined end to end into closed rings (node id lists); outlines that don't close are
    dropped"""
    segs = [list(ways[w][0]) for w in way_ids if w in ways and len(ways[w][0]) > 1]
    rings = []
    while segs:
        ring = segs.pop()
        while ring[0] != ring[-1]:
            k = next((i for i, s in enumerate(segs) if ring[-1] in (s[0], s[-1])), None)
            if k is None:
                break
            s = segs.pop(k)
            ring += (s if s[0] == ring[-1] else s[::-1])[1:]
        if len(ring) > 3 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def _inside(poly, pts):
    """which of pts (n x 2) lie inside the closed polygon poly (m x 2, last point = first), even-odd rule"""
    x, y = pts[:, :1], pts[:, 1:]
    x0, y0, x1, y1 = poly[:-1, 0], poly[:-1, 1], poly[1:, 0], poly[1:, 1]
    dy = np.where(y1 != y0, y1 - y0, 1e-12)
    return (((y0 > y) != (y1 > y)) & (x < x0 + (x1 - x0) * (y - y0) / dy)).sum(axis=1) % 2 == 1


def _lift_geometry(l, terrain):
    """terminals, towers evenly spaced at most the class's spacing apart along the 3D line (like the game: a 162 m
    chair gets 3 towers, a 6.4 km gondola 127), heights raised so the cable stays clear of the ground, queue line"""
    spacing, h_lo, h_hi = LIFTS[l['row']][4:7]
    a, b = l['a'], l['b']
    za, zb = float(terrain.z(*a)), float(terrain.z(*b))
    L3 = math.hypot(math.hypot(*(b - a)), (zb - za) / 100.0)
    n = max(1, math.ceil(L3 / spacing))
    towers = []
    for i in range(1, n):
        p = a + (b - a) * (i / n)
        towers.append([float(p[0]), float(p[1]), float(terrain.z(*p)), float(h_lo)])
    ends = [(float(a[0]), float(a[1]), za + 700.0, None)] + [(t[0], t[1], None, t) for t in towers] + \
           [(float(b[0]), float(b[1]), zb + 700.0, None)]
    s = np.linspace(0.1, 0.9, 9)
    for _ in range(6):                      # cable (straight between supports) >= 6 m above the ground
        changed = False
        for (xa, ya, ca, ta), (xb, yb, cb, tb) in zip(ends[:-1], ends[1:]):
            ha = ca if ta is None else ta[2] + ta[3]
            hb = cb if tb is None else tb[2] + tb[3]
            need = 600.0 - float((ha + (hb - ha) * s - terrain.z(xa + (xb - xa) * s, ya + (yb - ya) * s)).min())
            for tw in (ta, tb):
                if need > 1.0 and tw is not None and tw[3] < h_hi:
                    tw[3] = min(float(h_hi), tw[3] + need)
                    changed = True
        if not changed:
            break
    d = (b - a) / math.hypot(*(b - a))
    side = np.array([-d[1], d[0]])
    queue = [a - d * 5.2 + side * 2.8, a - d * 9.9 + side * 2.9]       # where the game puts it: behind, to one side
    l['wp'] = [(float(a[0]), float(a[1]), za), (float(b[0]), float(b[1]), zb)]
    l['towers'] = towers
    l['queue'] = [(float(q[0]), float(q[1]), float(terrain.z(*q))) for q in queue]
    l['length_m'] = math.hypot(*(b - a))


def _chains(lines, poly, terrain, skipped):
    """piste ways with the same name joined where they share a node, each piece pointing downhill"""
    groups = defaultdict(list)
    for w in lines:
        groups[w['name'].lower() if w['name'] else ('#', w['osm'])].append(w)
    out = []
    for ws in groups.values():
        segs = []
        for w in ws:
            P = poly(w['ids'])
            if P is None:
                continue
            a, b = w['ids'][0], w['ids'][-1]
            if terrain.z(*P[0]) < terrain.z(*P[-1]):
                P, a, b = P[::-1], b, a
            segs.append(dict(P=P, a=a, b=b, w=w, L=_length(P)))
        by_start = defaultdict(list)
        for i, s in enumerate(segs):
            by_start[s['a']].append(i)
        ends = {s['b'] for s in segs}
        used = set()
        for i0 in sorted(range(len(segs)), key=lambda i: (segs[i]['a'] in ends, -float(terrain.z(*segs[i]['P'][0])))):
            if i0 in used:
                continue
            chain = [i0]
            used.add(i0)
            while True:
                nxt = [j for j in by_start[segs[chain[-1]]['b']] if j not in used]
                if not nxt:
                    break
                chain.append(max(nxt, key=lambda j: segs[j]['L']))
                used.add(chain[-1])
            P = np.vstack([segs[chain[0]]['P']] + [segs[j]['P'][1:] for j in chain[1:]])
            name = segs[chain[0]]['w']['name']
            least = MIN_TRAIL_M if name else MIN_UNNAMED_M
            dl = Counter()
            for j in chain:
                if segs[j]['w']['diff']:
                    dl[segs[j]['w']['diff']] += segs[j]['L']
            diff = dl.most_common(1)[0][0] if dl else None
            tags = [segs[j]['w']['tags'] for j in chain]
            parts = [p for p in _inside_runs(P, terrain, 25.0) if _length(p) >= least]
            if not parts:
                skipped['short trail piece' if name else 'short unnamed piste'] += 1
            for part in parts:
                out.append(_corridor(name, diff, part, tags, terrain))
    return out


def _corridor(name, diff, P, tags, terrain):
    """a downhill polyline (corner metres) -> the game's chain of quadratic curves"""
    Q = _resample(P, 10.0)
    zq = terrain.z(Q[:, 0], Q[:, 1]) / 100.0
    slope = np.degrees(np.arctan2(np.abs(np.diff(zq)), np.maximum(np.hypot(*np.diff(Q, axis=0).T), 1e-6)))
    max_slope = float(np.percentile(slope, 95)) if len(slope) else 0.0
    if diff is None:                                   # no grade in OSM: from the incline
        diff = 'Beginner' if max_slope < 15 else 'Intermediate' if max_slope < 24 else 'Expert' if max_slope < 32 else 'Pro'
    width = next((w for w in (_num(t.get('width')) for t in tags) if w and 8 <= w <= 150), None) or WIDTH_M[diff]
    S = _resample(P, CURVE_M)
    d, ds = _cum(P), _cum(S)
    curves = []
    for i in range(len(S) - 1):
        p0, p2 = S[i], S[i + 1]
        tm = (ds[i] + ds[i + 1]) / 2 / ds[-1] * d[-1]
        m = np.array([np.interp(tm, d, P[:, 0]), np.interp(tm, d, P[:, 1])])
        c = 2 * m - (p0 + p2) / 2                      # the curve passes through the piste's own midpoint
        z0, z2, zm = (float(v) for v in terrain.z(np.array([p0[0], p2[0], m[0]]), np.array([p0[1], p2[1], m[1]])))
        curves.append((terrain.world(*p0, z0), terrain.world(*c, 2 * zm - (z0 + z2) / 2), terrain.world(*p2, z2),
                       width * 100.0))
    grooming = {t.get('piste:grooming') for t in tags} - {None}
    groomed = (diff in ('Beginner', 'Intermediate') or bool(grooming & {'classic', 'yes'})) and \
        not grooming & {'mogul', 'backcountry', 'no'}
    night = any(t.get('piste:lit') == 'yes' or t.get('lit') == 'yes' for t in tags)
    return dict(name=name, difficulty=diff, shape='Corridor', curves=curves, length_cm=_length(P) * 100.0,
                max_slope=max_slope, mid=terrain.world(*_at(P, 0.5)), groomed=groomed, night=night, P=P, width=width)


def _bowl(a, poly, terrain):
    """a piste area -> a bowl: outline ~every 100 m, centre = area centroid, radius = sqrt(area / pi) (as the game)"""
    P = poly(a['ids'])
    if P is None or len(P) < 4 or not all(terrain.inside(x, y, 25.0) for x, y in P):
        return None
    per = _length(P)
    O = _resample(P, min(max(per / 28.0, 15.0), 150.0))[:-1]
    x, y = O[:, 0], O[:, 1]
    x1, y1 = np.roll(x, -1), np.roll(y, -1)
    cr = x * y1 - x1 * y
    area = cr.sum() / 2.0
    if abs(area) < 15000:                              # 1.5 ha
        return None
    c = np.array([((x + x1) * cr).sum() / (6 * area), ((y + y1) * cr).sum() / (6 * area)])
    z = terrain.z(x, y) / 100.0
    max_slope = math.degrees(math.atan2(float(z.max() - z.min()), 2 * math.sqrt(abs(area) / math.pi))) * 1.6
    diff = a['diff'] or ('Pro' if max_slope > 38 else 'Expert' if max_slope > 26 else 'Intermediate')
    return dict(name=a['name'], difficulty=diff, shape='Bowl', outline=[terrain.world(*p) for p in O],
                center=terrain.world(*c), radius_cm=math.sqrt(abs(area) / math.pi) * 100.0, length_cm=per * 100.0,
                max_slope=min(max_slope, 60.0), mid=terrain.world(*c), groomed=False, night=False,
                P=np.vstack([O, O[:1]]), width=None)


# ---------------------------------------------------------------- for the app: summary, map lines, base, tree mask

def summary(pl):
    return dict(lifts=len(pl['lifts']), trails=len(pl['trails']),
                lift_types=dict(Counter(LIFTS[l['row']][1] for l in pl['lifts']).most_common()),
                difficulties=dict(Counter(t['difficulty'] for t in pl['trails'])),
                lift_list=sorted((dict(id=l['osm'], name=l['name'], real=l['why'], game=LIFTS[l['row']][1], upkeep=upkeep(l))
                                  for l in pl['lifts']), key=lambda r: r['name'].lower()),
                skipped=pl['skipped'])


def overlay(pl, terrain):
    """the lifts and trails as lat/lon lines for the map view"""
    def ll(P):
        la, lo = terrain.latlon(P[:, 0], P[:, 1])
        return [[round(float(a), 6), round(float(b), 6)] for a, b in zip(la, lo)]
    out = [dict(kind='lift', name=l['name'], game=LIFTS[l['row']][1], pts=ll(np.array([p[:2] for p in l['wp']])))
           for l in pl['lifts']]
    out += [dict(kind=t['shape'].lower(), name=t['name'], difficulty=t['difficulty'], pts=ll(_resample(t['P'], 25.0)))
            for t in pl['trails']]
    return out


def suggest_base(pl, terrain, margin_m=120.0):
    """(lat, lon) for the ticket office: by the bottom of the lift with the most other lift bottoms nearby (the main
    base area), a little behind it, at least margin_m inside the square; None without lifts"""
    if not pl['lifts']:
        return None
    bottoms = np.array([l['wp'][0] for l in pl['lifts']])

    def score(i):
        x, y, z = bottoms[i]
        near = np.hypot(bottoms[:, 0] - x, bottoms[:, 1] - y) < 400
        return near.sum() - (z - bottoms[:, 2].min()) / 100.0 / 150.0     # 150 m higher costs one lift
    i = max(range(len(bottoms)), key=score)
    l = pl['lifts'][i]
    a, b = np.array(l['wp'][0][:2]), np.array(l['wp'][-1][:2])
    p = a - (b - a) / math.hypot(*(b - a)) * 50.0
    m = max(120.0, float(margin_m))
    p = np.clip(p, m, terrain.size - m)
    la, lo = terrain.latlon(p[0], p[1])
    return [float(la), float(lo)]


def clear_mask(pl, size_m, res_m=4.0):
    """(bool raster, res_m): True where no tree may stand (trails, bowls, under the lifts); row = y / res_m"""
    n = int(math.ceil(size_m / res_m)) + 1
    img = Image.new('1', (n, n), 0)
    dr = ImageDraw.Draw(img)

    def px(P):
        return [(float(x) / res_m, float(y) / res_m) for x, y in P]

    def disc(x, y, r):
        dr.ellipse([x - r, y - r, x + r, y + r], fill=1)

    for t in pl['trails']:
        if t['shape'] == 'Bowl':
            dr.polygon(px(t['P']), fill=1)
            continue
        w = (t['width'] + 6.0) / res_m
        pts = px(_resample(t['P'], 8.0))
        dr.line(pts, fill=1, width=max(2, int(round(w))))
        for x, y in pts[::3] + pts[-1:]:
            disc(x, y, w / 2)
    for l in pl['lifts']:
        pts = px([p[:2] for p in l['wp']])
        dr.line(pts, fill=1, width=max(2, int(round(2 * LIFTS[l['row']][7] / 100.0 / res_m))))
        for x, y in pts:
            disc(x, y, 22.0 / res_m)
    return np.array(img, dtype=bool), res_m


# ---------------------------------------------------------------- the save

def _templates():
    tags = G.read_props(G.R(paths.resource('data', 'resort_templates.bin').read_bytes(), 0))
    return {t.name: t for t in tags}


def _set(tags, name, value):
    t = G.get(tags, name)
    if t is None:
        raise ResortError('save template field %s missing' % name)
    t.value = value


def _text(s):
    return {'flags': 2, 'history': -1, 'text': s}


def _vec(p):
    return {'X': float(p['X']), 'Y': float(p['Y']), 'Z': float(p['Z'])}


def _office(tags):
    """(hub id, X, Y) of the starting save's ticket office"""
    for b in G.get(tags, 'Buildings').value:
        cls = G.get(b, 'BuildingClassPath') or G.get(b, 'ClassPath')
        if cls is not None and isinstance(cls.value, dict) and 'TicketOffice' in cls.value.get('asset', ''):
            bd = G.get(b, 'BaseData').value
            tr = G.get(G.get(bd, 'Transform').value, 'Translation').value
            return G.get(bd, 'HubId').value, tr['X'], tr['Y']
    raise ResortError('no ticket office in the starting save')


def add_to_save(is_bytes, pl, terrain):
    """the starting save (2 bytes, int32 GVAS length, GVAS, trailer) with the plan's lifts, trails, hub names and
    cleared zones added -> new bytes"""
    n = struct.unpack_from('<i', is_bytes, 2)[0]
    head, tags, tail = G.parse(is_bytes[6:6 + n])
    trailer = is_bytes[6 + n:]
    T = _templates()
    ts = G.get(tags, 'SaveTimestamp')
    ticks = int(ts.value['Ticks']) if ts is not None else 0

    # lift terminals near the ticket office are in its hub, so arriving guests can ride those lifts (a lift's top stays
    # in its own hub when its bottom is there already)
    office, ox, oy = _office(tags)
    near = {h['id'] for h in pl['hubs']
            if math.hypot((h['x'] - terrain.half) * 100.0 - ox, (h['y'] - terrain.half) * 100.0 - oy) < OFFICE_HUB_M * 100.0}
    ends = []
    for l in pl['lifts']:
        b = office if l['hub_bottom'] in near else l['hub_bottom']
        t = office if l['hub_top'] in near and b != office else l['hub_top']
        ends.append((b, t))

    # Lifts: a root array after Buildings (the starting save has none)
    if G.get(tags, 'Lifts') is not None:
        raise ResortError('the starting save already has lifts')
    proto = T['Lifts'].value[0]
    lifts = []
    for l, (hub_bottom, hub_top) in zip(pl['lifts'], ends):
        e = G.clone(proto)
        bd = G.get(e, 'BaseData').value
        _set(bd, 'ID', G.new_guid())
        xf = G.get(bd, 'Transform').value
        _set(xf, 'Rotation', {'X': 0.0, 'Y': 0.0, 'Z': 0.0, 'W': 1.0})
        _set(xf, 'Translation', _vec(terrain.world(*l['wp'][0])))
        _set(xf, 'Scale3D', {'X': 1.0, 'Y': 1.0, 'Z': 1.0})
        _set(bd, 'DisplayName', _text(l['name']))
        _set(bd, 'ConstructionCost', int(LIFTS[l['row']][2]))
        _set(bd, 'HubId', hub_bottom)
        _set(G.get(bd, 'DesignLocks').value, 'bNoRefund', True)     # came free: bulldozing it pays nothing back
        cls = LIFTS[l['row']][0]
        _set(e, 'LiftClassPath', {'pkg': cls, 'asset': cls.rsplit('/', 1)[1] + '_C', 'sub': ''})
        wps = []
        for x, y, z in l['wp']:
            w = G.clone(G.get(proto, 'Waypoints').value[0])
            _set(w, 'position', _vec(terrain.world(x, y, z)))
            _set(w, 'bIsTerminal', True)
            _set(w, 'bIsAngleStation', False)
            wps.append(w)
        _set(e, 'Waypoints', wps)
        tws = []
        for x, y, z, h in l['towers']:
            w = G.clone(G.get(proto, 'Towers').value[0])
            _set(w, 'BasePosition', _vec(terrain.world(x, y, z)))
            _set(w, 'Height', float(h))
            _set(w, 'TowerId', G.new_guid())
            tws.append(w)
        _set(e, 'Towers', tws)
        _set(e, 'TopHubId', hub_top)
        _set(e, 'Condition', 100.0)
        _set(e, 'BuiltSeasonIndex', 1)
        _set(e, 'QueueLinePoints', [_vec(terrain.world(*q)) for q in l['queue']])
        _set(e, 'QueueRopeLiftCm', [0.0])
        _set(e, 'OpenHour', OPEN_HOURS[0])
        _set(e, 'CloseHour', OPEN_HOURS[1])
        lifts.append(e)
    lt = G.clone(T['Lifts'])
    lt.value = lifts
    tags.insert(next(i for i, t in enumerate(tags) if t.name == 'Buildings') + 1, lt)

    # trails: TrailMapData.RegisteredTrails (first field; the starting save has none), CurrentTrailId = last id used
    tmd = G.get(tags, 'TrailMapData').value
    if G.get(tmd, 'RegisteredTrails') is not None:
        raise ResortError('the starting save already has trails')
    reg = G.clone(T['RegisteredTrails'])
    reg.value = []
    tmd.insert(0, reg)
    cur = G.get(tmd, 'CurrentTrailId')
    tproto = T['RegisteredTrails'].value[0][1]
    zero = {'X': 0.0, 'Y': 0.0, 'Z': 0.0}
    for t in pl['trails']:
        cur.value += 1
        e = G.clone(tproto)
        _set(e, 'TrailId', cur.value)
        _set(e, 'DisplayName', _text(t['name']))
        _set(e, 'PlayerRatedDifficulty', 'ETrailDifficulty::' + t['difficulty'])
        _set(e, 'Shape', 'ETrailShape::' + t['shape'])
        cproto = G.get(e, 'Curve').value
        if t['shape'] == 'Corridor':
            cs = [_curve(cproto, c) for c in t['curves']]
            _set(e, 'Curve', cs[0])
            _set(e, 'AdditionalCurves', cs[1:])
        else:
            _set(e, 'Curve', _curve(cproto, (zero, zero, zero, 2000.0)))
            _set(e, 'BowlOutline', [_vec(p) for p in t['outline']])
            _set(e, 'BowlCenter', _vec(t['center']))
            _set(e, 'BowlRadius', float(t['radius_cm']))
        _set(e, 'MaxSlopeAngle', float(t['max_slope']))
        _set(e, 'Length', float(t['length_cm']))
        _set(e, 'MidpointPosition', _vec(t['mid']))
        _set(e, 'bIsGroomed', bool(t['groomed']))
        _set(e, 'bNightSkiing', bool(t['night']))
        _set(e, 'FirstCreatedAt', {'Ticks': ticks})
        _set(e, 'LastModifiedAt', {'Ticks': ticks})
        reg.value.append((cur.value, e))

    # names of the hubs in use (the ticket office's hub has its own already)
    names = G.get(G.get(tags, 'HubData').value, 'HubNames')
    used = {i for pair in ends for i in pair}
    for h in pl['hubs']:
        if h['id'] in used:
            e = G.clone(T['HubNames'].value[0])
            _set(e, 'HubId', h['id'])
            _set(e, 'Name', _text(h['name']))
            names.value.append(e)

    # cleared zones in the map's level entry: capsules along the lifts and trails, circles at the stations, an
    # outline round each bowl
    zones = G.get(G.get(tags, 'ClearedObstaclesByLevel').value[0][1], 'Zones')
    cap, outl = T['Zones'].value
    for l in pl['lifts']:
        a, b = (terrain.world(x, y, 0.0) for x, y, _ in l['wp'])
        zones.value.append(_zone(cap, a, b, LIFTS[l['row']][7]))
        zones.value += [_zone(cap, p, p, 2200.0) for p in (a, b)]
    for t in pl['trails']:
        if t['shape'] == 'Corridor':
            for p0, c, p2, w in t['curves']:
                B = _bezier(p0, c, p2, 2000.0)
                zones.value += [_zone(cap, {'X': u[0], 'Y': u[1]}, {'X': v[0], 'Y': v[1]}, w / 2.0)
                                for u, v in zip(B[:-1], B[1:])]
        else:
            e = G.clone(outl)
            _set(e, 'Center', {'X': t['center']['X'], 'Y': t['center']['Y']})
            _set(e, 'Radius', float(t['radius_cm']))
            _set(e, 'EndOffset', {'X': 0.0, 'Y': 0.0})
            _set(e, 'Outline', [{'X': p['X'], 'Y': p['Y']} for p in t['outline']])
            zones.value.append(e)

    gv = G.build(head, tags, tail)
    return is_bytes[:2] + struct.pack('<i', len(gv)) + gv + trailer


def _zone(proto, a, b, radius_cm):
    e = G.clone(proto)
    _set(e, 'Center', {'X': float(a['X']), 'Y': float(a['Y'])})
    _set(e, 'Radius', float(radius_cm))
    _set(e, 'EndOffset', {'X': float(b['X'] - a['X']), 'Y': float(b['Y'] - a['Y'])})
    _set(e, 'Outline', [])
    return e


def _curve(proto, c):
    p0, cp, p2, w = c
    e = G.clone(proto)
    _set(e, 'StartPoint', _vec(p0))
    _set(e, 'ControlPoint', _vec(cp))
    _set(e, 'Endpoint', _vec(p2))
    _set(e, 'CorridorWidth', float(w))
    _set(e, 'EndCorridorWidth', 0.0)
    return e
