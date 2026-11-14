"""Make data/resort_templates.bin: the save-format templates the OpenStreetMap import fills in (skieo_creator/resort.py).

The game's own starting save has no lifts and no trails, so the property layouts for them (type trees, flags, field
order) come from a save of the current game version that has at least one lift, one trail and cleared zones (a capsule
and an outline). Every value is blanked here: names, positions, ids, statistics; only the structure ships.
usage: python packaging/make_resort_templates.py <a .sav with a lift and a trail>"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from skieo_creator.engine import gvas_tree as G  # noqa: E402

ZERO_GUID = '0' * 32
ZERO_V = {'X': 0.0, 'Y': 0.0, 'Z': 0.0}


def field(tags, name):
    t = G.get(tags, name)
    assert t is not None, name
    return t


def blank(tags):
    """zero every number, empty every array / map, blank strings and texts, recursively (structure stays)"""
    for t in tags:
        k = t.tree[0]
        v = t.value
        if isinstance(v, G.Raw):
            raise SystemExit('template field %s could not be decoded: %s' % (t.name, v.why))
        if k in G.INTS:
            t.value = 0.0 if isinstance(v, float) else 0
        elif k == 'BoolProperty':
            t.value = False
        elif k == 'TextProperty':
            t.value = {'flags': 2, 'history': -1, 'text': 'x'}
        elif k == 'StructProperty':
            s = t.tree[1][0]
            if s == 'Guid':
                t.value = ZERO_GUID
            elif s in G.NATIVE:
                t.value = {n: 0 for n in G.NATIVE[s][1]}
                if s in ('Vector', 'Vector2D', 'Quat', 'Vector4'):
                    t.value = {n: 0.0 for n in G.NATIVE[s][1]}
            elif s in ('SoftClassPath', 'SoftObjectPath'):
                pass                                       # set by the importer
            elif isinstance(v, list):
                blank(v)
        elif k in ('ArrayProperty', 'MapProperty', 'SetProperty'):
            if isinstance(v, list) and v and isinstance(v[0], list) and t.name in KEEP_ONE:
                blank(v[0])
                t.value = v[:1]
            elif isinstance(v, list) and v and isinstance(v[0], tuple) and t.name in KEEP_ONE:
                key, val = v[0]
                if isinstance(val, list):
                    blank(val)
                t.value = [(0 if isinstance(key, int) else key, val)]
            elif isinstance(v, list) and v and isinstance(v[0], dict) and t.name in KEEP_ONE:
                t.value = [{n: 0.0 for n in v[0]}]
            else:
                t.value = []
    return tags


KEEP_ONE = {'Lifts', 'Waypoints', 'Towers', 'QueueLinePoints', 'RegisteredTrails', 'HubNames', 'QueueRopeLiftCm'}


def main(path):
    gv = Path(path).read_bytes()
    head, tags, tail = G.parse(gv)
    lifts = field(tags, 'Lifts')
    trails = field(field(tags, 'TrailMapData').value, 'RegisteredTrails')
    hubs = field(field(tags, 'HubData').value, 'HubNames')
    zones = field(field(tags, 'ClearedObstaclesByLevel').value[0][1], 'Zones')
    caps = [z for z in zones.value if G.plain(field(z, 'EndOffset'))['X'] != 0 and not G.plain(field(z, 'Outline'))]
    outl = [z for z in zones.value if G.plain(field(z, 'Outline'))]
    assert lifts.value and trails.value and hubs.value and caps and outl, 'the save needs a lift, a trail, hub names, ' \
                                                                          'a capsule zone and an outline zone'
    blank([lifts, trails, hubs])
    lift = lifts.value[0]
    field(lift, 'LiftClassPath').value = {'pkg': '/Game/Blueprints/Lifts/BP_FixedChair2', 'asset': 'BP_FixedChair2_C', 'sub': ''}
    for z in (caps[0], outl[0]):
        blank(z)
    field(outl[0], 'Outline').value = [{'X': 0.0, 'Y': 0.0}]
    zones.value = [caps[0], outl[0]]
    out = G.w_props([lifts, trails, hubs, zones])
    (ROOT / 'data' / 'resort_templates.bin').write_bytes(out)
    print('resort_templates.bin: %d bytes (lift, trail, hub name, capsule + outline zone)' % len(out))


if __name__ == '__main__':
    main(sys.argv[1])
