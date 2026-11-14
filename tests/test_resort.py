"""Self-check of the OpenStreetMap import rules (skieo_creator/resort.py) on made-up data: no downloads, no game.
usage: python tests/test_resort.py"""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skieo_creator import builder, net, resort  # noqa: E402

# ---- lift types: the real lift -> the closest stock lift
R = resort.lift_row
assert R({'aerialway': 'chair_lift', 'aerialway:occupancy': '6', 'aerialway:detachable': 'yes'}, False)[0] == 'HighSpeedSix'
assert R({'aerialway': 'chair_lift', 'aerialway:occupancy': '4', 'aerialway:detachable': 'yes'}, False)[0] == 'HighSpeedQuad'
assert R({'aerialway': 'chair_lift', 'aerialway:occupancy': '4', 'name': 'Centennial Express'}, False)[0] == 'HighSpeedQuad'
assert R({'aerialway': 'chair_lift', 'aerialway:occupancy': '4'}, False)[0] == 'FourPersonFixed'     # North America
assert R({'aerialway': 'chair_lift', 'aerialway:occupancy': '4'}, True)[0] == 'HighSpeedQuad'       # Europe
assert R({'aerialway': 'chair_lift', 'aerialway:occupancy': '3'}, False)[0] == 'FourPersonFixed'
assert R({'aerialway': 'chair_lift', 'aerialway:occupancy': '2'}, False)[0] == 'TwoPersonFixed'
assert R({'aerialway': 'chair_lift', 'aerialway:occupancy': '8'}, False)[0] == 'HighSpeedSix'
assert R({'aerialway': 'gondola', 'aerialway:occupancy': '8'}, False)[0] == 'Gondola'
assert R({'aerialway': 'gondola', 'aerialway:occupancy': '30'}, False)[0] == 'AerialTram'
assert R({'aerialway': 'cable_car'}, False)[0] == 'AerialTram'
assert R({'aerialway': 't-bar'}, False)[0] == 'TwoPersonFixed'
assert R({'aerialway': 'magic_carpet'}, False)[0] == 'OnePersonFixed'
assert R({'aerialway': 'goods'}, False)[0] is None
assert R({'aerialway': 'chair_lift', 'disused': 'yes'}, False)[0] is None

# ---- upkeep: what the game's ledger charged (daily operating cost x (towers + 4) / 8)
assert resort.upkeep({'row': 'HighSpeedQuad', 'towers': [0] * 53}) == 13573
assert resort.upkeep({'row': 'Gondola', 'towers': [0] * 13}) == 42713
assert resort.upkeep({'row': 'OnePersonFixed', 'towers': [0]}) == 38

# ---- a made-up resort on a made-up mountain: a 4 km square sloping down to the south (y), 0.25 m per m
size = 4000.0
G = builder.G
rows = np.arange(G, dtype=np.float64) * size / (G - 1)
dem = np.repeat((2000.0 - 0.25 * rows)[:, None], G, axis=1)
H, fr = builder.heights(dem, size)
lat0, lon0 = 46.0, 7.0
terrain = resort.Terrain(H, fr, lat0, lon0, size)
nodes = {}


def node(i, x, y):
    la, lo = terrain.latlon(x, y)
    nodes[i] = dict(type='node', id=i, lat=float(la), lon=float(lo))


node(1, 2000, 3000)        # lift bottom (low, south)
node(2, 2000, 1000)        # lift top
node(3, 1800, 900)         # trail: two named pieces drawn uphill, sharing node 4
node(4, 1820, 2000)
node(5, 1840, 3100)
osm = {'elements': list(nodes.values()) + [
    dict(type='way', id=100, nodes=[2, 1], tags={'aerialway': 'chair_lift', 'aerialway:occupancy': '6', 'name': 'Six'}),
    dict(type='way', id=200, nodes=[4, 3], tags={'piste:type': 'downhill', 'piste:name': 'Long Run', 'piste:difficulty': 'intermediate'}),
    dict(type='way', id=201, nodes=[5, 4], tags={'piste:type': 'downhill', 'piste:name': 'Long Run', 'piste:difficulty': 'intermediate'}),
]}
pl = resort.plan(osm, terrain)
assert len(pl['lifts']) == 1 and len(pl['trails']) == 1, pl['skipped']
lift, trail = pl['lifts'][0], pl['trails'][0]
assert lift['row'] == 'HighSpeedSix' and lift['wp'][0][1] > lift['wp'][1][1], 'the lift goes up the slope (north)'
L3 = math.hypot(2000.0, 500.0)
assert len(lift['towers']) == math.ceil(L3 / 50) - 1, len(lift['towers'])
assert lift['hub_bottom'] != lift['hub_top']
assert trail['name'] == 'Long Run' and trail['difficulty'] == 'Intermediate' and trail['shape'] == 'Corridor'
p0, p_end = trail['curves'][0][0], trail['curves'][-1][2]
assert p0['Y'] < p_end['Y'] and p0['Z'] > p_end['Z'], 'the trail is joined into one and runs downhill'
assert abs(trail['length_cm'] / 100 - 2200) < 30, trail['length_cm']
assert all(abs(a[2]['X'] - b[0]['X']) < 1e-6 for a, b in zip(trail['curves'], trail['curves'][1:])), 'curves chain'
assert resort.plan(osm, terrain, skip={100})['lifts'] == []
mask, res = resort.clear_mask(pl, size)
assert mask[int(2000 / res), int(2000 / res)] and mask[int(2000 / res), int(1820 / res)] and not mask[int(2000 / res), int(3000 / res)]

# ---- found in review: station tags survive the tagless 'out skel' copy; multipolygon piste areas; unnamed areas drawn
# round a run; the base stays inside the square's margin; a timed-out Overpass answer is not accepted
for i, (x, y) in {10: (3000, 1500), 11: (3400, 1500), 12: (3400, 1900), 13: (3000, 1900),
                  20: (1750, 1200), 21: (1900, 1200), 22: (1900, 2600), 23: (1750, 2600)}.items():
    node(i, x, y)
st = dict(nodes[1], tags={'aerialway': 'station', 'name': 'Valley Station'})
osm2 = {'elements': [st] + list(nodes.values()) + osm['elements'][len(nodes) - 8:] + [
    dict(type='way', id=300, nodes=[10, 11, 12]), dict(type='way', id=301, nodes=[12, 13, 10]),
    dict(type='relation', id=400, members=[dict(type='way', ref=300, role='outer'), dict(type='way', ref=301, role='outer')],
         tags={'type': 'multipolygon', 'piste:type': 'downhill', 'name': 'Big Bowl', 'piste:difficulty': 'advanced'}),
    dict(type='way', id=500, nodes=[20, 21, 22, 23, 20], tags={'piste:type': 'downhill'}),
]}
pl2 = resort.plan(osm2, terrain)
by_name = {t['name']: t for t in pl2['trails']}
assert pl2['hubs'][0]['name'] == 'Valley Station', [h['name'] for h in pl2['hubs']]
assert by_name['Big Bowl']['shape'] == 'Bowl' and by_name['Big Bowl']['difficulty'] == 'Expert', by_name.keys()
assert sum(t['name'] == 'Big Bowl' for t in pl2['trails']) == 1
assert len(pl2['trails']) == 2 and pl2['skipped'].get('unnamed piste area drawn round a run') == 1, pl2['skipped']
bx, by = terrain.xy(*resort.suggest_base(pl, terrain, margin_m=1500.0))
assert 1500 - 1e-3 <= by <= size - 1500 + 1e-3, by
try:
    resort._complete(b'{"elements": [], "remark": "runtime error: Query timed out in \\"query\\" at line 2 after 121 seconds."}')
    raise AssertionError('timed-out answer accepted')
except ValueError:
    pass
assert resort._complete(b'{"elements": []}')

# ---- glades: left out when tagged or named so, or when the run goes through the trees with no cleared corridor
G_ = resort._gladed
assert G_({'gladed': 'yes'}) and G_({'name': 'Lower Slalom Glade'}) and G_({'piste:name': 'Tea Cup Glades'})
assert G_({'name': 'Sous-bois du Nord'}) and G_({'natural': 'wood', 'area': 'yes'})
assert G_({'gladed': 'no', 'name': 'Jade Glade'}) is False
assert G_({'name': 'Everglades'}) is None and G_({'name': 'Black Forest'}) is None and G_({}) is None
for i, (x, y) in {30: (2600, 1000), 31: (2600, 2500), 32: (3000, 1000), 33: (3000, 2500),
                  40: (1200, 1500), 41: (1500, 1500), 42: (1500, 1800), 43: (1200, 1800),
                  50: (3400, 2600), 51: (3400, 3300), 60: (600, 2000), 61: (900, 2000), 62: (900, 2300), 63: (600, 2300),
                  70: (3600, 600), 71: (3600, 1400), 72: (3800, 600), 73: (3800, 1400)}.items():
    node(i, x, y)
pistes = osm['elements'][len(osm['elements']) - 2:]
osm3 = {'elements': list(nodes.values()) + pistes + [
    dict(type='way', id=600, nodes=[30, 31], tags={'piste:type': 'downhill', 'name': 'Tree Run', 'piste:difficulty': 'advanced'}),
    dict(type='way', id=601, nodes=[32, 33], tags={'piste:type': 'downhill', 'name': 'Green Path', 'piste:difficulty': 'novice'}),
    dict(type='way', id=602, nodes=[40, 41, 42, 43, 40], tags={'piste:type': 'downhill', 'name': 'Tree Bowl',
                                                               'piste:difficulty': 'expert'}),
    dict(type='way', id=603, nodes=[50, 51], tags={'piste:type': 'downhill', 'name': 'Moon Dance', 'gladed': 'yes'}),
    dict(type='way', id=604, nodes=[60, 61, 62, 63, 60], tags={'piste:type': 'downhill', 'name': 'Moon Dance'}),
    dict(type='way', id=605, nodes=[70, 71], tags={'piste:type': 'downhill', 'name': 'Star Dust', 'highway': 'track',
                                                   'piste:difficulty': 'intermediate'}),     # a road: kept
    dict(type='way', id=606, nodes=[72, 73], tags={'piste:type': 'downhill', 'name': 'Star Dust', 'gladed': 'yes'}),
]}


def woods(la, lo):
    """trees 20 m tall everywhere but a cleared strip 4-16 m east of Long Run's line (drawn a little off its run)"""
    x, y = terrain.xy(la, lo)
    line_x = 1800 + (np.clip(y, 900, 3100) - 900) * 40 / 2200
    return np.where((x > line_x + 4) & (x < line_x + 16), 0, 20).astype(np.uint8)


pl3 = resort.plan(osm3, terrain, canopy=woods)
kept, gone = {t['name'] for t in pl3['trails']}, {g['name'] for g in pl3['glades']}
assert {'Long Run', 'Green Path', 'Star Dust'} <= kept and not kept & {'Tree Run', 'Tree Bowl', 'Moon Dance'}, kept
assert [t['road'] for t in pl3['trails'] if t['name'] == 'Star Dust'] == [True]      # a road named like a glade
assert gone == {'Tree Run', 'Tree Bowl', 'Moon Dance', 'Star Dust'}, gone
assert pl3['skipped'][resort.GLADE] == 4 and sum(o['kind'] == 'glade' for o in resort.overlay(pl3, terrain)) == 5
tagged_only = resort.plan(osm3, terrain)                 # no satellite check: only the marked glades, and their areas
assert {t['name'] for t in tagged_only['trails']} >= {'Tree Run', 'Tree Bowl', 'Long Run', 'Star Dust'}
assert not tagged_only['notes']
assert {g['name'] for g in tagged_only['glades']} == {'Moon Dance', 'Star Dust'} and len(tagged_only['glades']) == 3
assert tagged_only['skipped'][resort.GLADE] == 2


def offline(la, lo):
    raise net.NetError('Could not download from example (timed out). Check your internet connection and try again.')


pl4 = resort.plan(osm3, terrain, canopy=offline)
assert 'Tree Run' in {t['name'] for t in pl4['trails']} and len(pl4['notes']) == 1, pl4['notes']
assert (pl3['tree_check'], tagged_only['tree_check'], pl4['tree_check']) == (True, None, False)
assert builder.recipe({'osm': True}, dict(tree_check=False)) < builder.recipe({'osm': True})    # asks to be rebuilt
assert resort.chm._quadkey(304, 375) == '0302330222'     # the canopy file over Stratton (zoom-10 tile 304, 375)
print('resort: all checks passed')
