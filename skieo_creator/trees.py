"""Forests for generated maps, in the game's baked-obstacle format (DA_BakedObstacles_<map>, 'SKOB' v2).

A SKOB v2 blob holds a species table -- (species data asset, size class 0-3 = 5/10/15/20 m, state 0 healthy /
1 dying / 2 dead / 3 krummholz, trunk mesh) -- and 20-byte tree records: x, y, z (int32 mm in the landscape frame),
yaw / width / height bytes and a u16 index into the table. Every entry of the game's own tables is exactly one
(size, state, trunk mesh) variant of the species' DA_TreeSpecies_* asset, so tables for any biome are built from those
assets (species_variants) and stay consistent with what the game would bake itself.
"""
import logging
import struct

import numpy as np

from . import biomes
from .engine import legacy_header as lh

log = logging.getLogger(__name__)
G = 2017
STRIDE = 20
BANDS = 10
MAX_TREES = 1490000            # the most a map may get (tree limit slider): the game places none at all past
                               # 1.5M (1,494,316 showed, 1,500,011 showed none; tested 2026-10-31), so a margin below
DEFAULT_TREES = 250000         # about what the game's own maps have (Mad River Glen 195k, Sugar Bowl 256k)
MIN_TREES = 25000
# Full forest up to the map's tree slope ("Trees up to" slider), none at or above it. The default is where the game
# draws bare rock ("extreme terrain", ~33 deg). Measured on the slope's steepest value around each vertex, so trees
# don't stand right at a rock face either.
TREE_SLOPE = 33.0
MIN_TREE_SLOPE, MAX_TREE_SLOPE = 20.0, 55.0
TREELINE_FADE, MAX_TREELINE_FADE = 200.0, 1000.0   # m below the treeline over which forest thins out to nothing
CONIFERS = ('Fir', 'Spruce', 'Pine', 'WhitebarkPine', 'Hemlock', 'Larch', 'Sclerophyll')
BROADLEAF = ('Aspen', 'Birch', 'Maple', 'Oak', 'MountainBirch', 'SouthernBeech', 'SnowGum')


# ---------------------------------------------------------------- formats

def read_skob(uexp):
    raw_len = struct.unpack_from('<i', uexp, 2)[0]
    raw = uexp[6:6 + raw_len]
    if raw[:4] != b'SKOB':
        raise ValueError('not a baked obstacle asset')
    ver, count, stride = struct.unpack_from('<iii', raw, 4)
    if (ver, stride) != (2, STRIDE):
        raise ValueError('baked obstacle format %d/%d is not supported' % (ver, stride))
    recs = raw[len(raw) - count * stride:]
    return dict(head2=uexp[:2], hdr=raw[16:48], table=raw[48:len(raw) - count * stride], records=recs, ver=ver,
                stride=stride, tail=uexp[6 + raw_len + 8:], count=count)


def table_entries(table):
    n = struct.unpack_from('<H', table, 0)[0]
    o, out = 2, []
    for _ in range(n):
        ln = struct.unpack_from('<H', table, o)[0]
        sp = table[o + 2:o + 2 + ln].decode()
        o += 2 + ln
        a, b = table[o], table[o + 1]
        o += 2
        ln = struct.unpack_from('<H', table, o)[0]
        mesh = table[o + 2:o + 2 + ln].decode()
        o += 2 + ln
        out.append((sp, a, b, mesh))
    if o != len(table):
        raise ValueError('tree table has trailing bytes')
    return out


def encode_table(ents):
    out = struct.pack('<H', len(ents))
    for sp, a, b, mesh in ents:
        out += struct.pack('<H', len(sp)) + sp.encode() + bytes([a, b]) + struct.pack('<H', len(mesh)) + mesh.encode()
    return out


def species_variants(ua, ue):
    """[(size, state, trunk mesh)] of a DA_TreeSpecies_* asset. Its variant array is unversioned structs: fragment
    0x1180 (8 fields, zero mask follows), mask byte, then Size (u8) and State (u8) unless zero, TrunkMesh (import
    index), CanopyMesh (import index) unless zero, then fields this tool does not need."""
    f, names, _, _ = lh.parse(ua)
    n, off = f['imports_count'][1], f['imports_offset'][1]
    imps = [names[struct.unpack_from('<8i', ua, off + 32 * i)[5]] for i in range(n)]
    out = []
    p = 0
    while True:
        p = ue.find(b'\x80\x11', p)
        if p < 0:
            break
        m = ue[p + 2]
        q = p + 3
        size = 0 if m & 1 else ue[q]
        q += 0 if m & 1 else 1
        state = 0 if m & 2 else ue[q]
        q += 0 if m & 2 else 1
        trunk = struct.unpack_from('<i', ue, q)[0]
        if not (-len(imps) <= trunk < 0) or size > 3 or state > 3:
            raise ValueError('unexpected tree species layout')
        out.append((size, state, imps[-trunk - 1]))
        p += 2
    if not out:
        raise ValueError('no tree variants found')
    return out


# ---------------------------------------------------------------- planting

def _slope(H, sp, sc):
    gy, gx = np.gradient(H.astype(np.float64) * sc, sp)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def _local_max(a, r):
    out = a.copy()
    p = np.pad(a, r, mode='edge')
    n0, n1 = a.shape
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            np.maximum(out, p[dy:dy + n0, dx:dx + n1], out=out)
    return out


def open_ground(H, hmin, sp, sc, treeline, valley):
    """treeless cells: above the treeline, flat valley floor below `valley` (m), and lakes / sea (dead flat)"""
    elev = (H.astype(np.float64) + 32768) * sc + hmin
    slope = _slope(H, sp, sc)
    mask = np.zeros(H.shape, bool)
    if treeline > 0:
        mask |= elev > treeline
    if valley > 0:
        mask |= (elev < valley) & (slope < 10.0)
    r = max(2, int(round(12.0 / sp)))                       # water: flat to within 0.6 deg over ~25 m
    mask |= _local_max(slope, r) < 0.6
    return mask


def plant(H, sp, sc, density_records, rng, open_mask, max_trees=MAX_TREES, tree_slope=TREE_SLOPE, weight=None):
    """tree positions (x, y in metres from the landscape corner, z in metres above its bottom) at the stock density,
    thinned evenly when that would give more than max_trees; none on ground steeper than tree_slope (deg);
    weight (0..1 per vertex, optional) thins them further"""
    rec = np.frombuffer(density_records, np.uint8).reshape(-1, STRIDE)
    X = rec[:, 0:4].copy().view('<i4').ravel() / 1000.0
    Y = rec[:, 4:8].copy().view('<i4').ravel() / 1000.0
    cell = (X // 30).astype(np.int64) * 1000 + (Y // 30).astype(np.int64)
    dens = np.percentile(np.unique(cell, return_counts=True)[1], 75) / 900.0
    ext = (G - 1) * sp
    # vertices a tree may stand on: gentler than tree_slope, not open ground
    ok = ((_local_max(_slope(H, sp, sc), 1) < tree_slope) & ~open_mask).astype(np.float64)
    if weight is not None:
        ok *= weight
    area_frac = max(float(ok.mean()), 0.01)                 # the share of the map trees can grow on
    n = min(int(ext * ext * dens), int(max_trees / area_frac))
    x = rng.uniform(0.5, ext - 0.5, n)
    y = rng.uniform(0.5, ext - 0.5, n)
    vx, vy = x / sp, y / sp
    x0 = np.clip(vx.astype(int), 0, G - 2)
    y0 = np.clip(vy.astype(int), 0, G - 2)
    fx, fy = vx - x0, vy - y0
    Hf = H.astype(np.float64)
    h = (Hf[y0, x0] * (1 - fx) * (1 - fy) + Hf[y0, x0 + 1] * fx * (1 - fy) + Hf[y0 + 1, x0] * (1 - fx) * fy
         + Hf[y0 + 1, x0 + 1] * fx * fy)
    iy, ix = np.clip(np.rint(vy).astype(int), 0, G - 1), np.clip(np.rint(vx).astype(int), 0, G - 1)
    keep = rng.random(n) < ok[iy, ix]
    return x[keep], y[keep], (h[keep] + 32768) * sc


def _pick_variant(variants, size, state, rng, n):
    """indices into `variants` for n trees that should look like (size, state)"""
    def cands(pred):
        return [i for i, v in enumerate(variants) if pred(v)]
    c = cands(lambda v: v[0] == size and v[1] == state)
    if not c and state == 3:
        c = cands(lambda v: v[0] == 0 and v[1] == 0)
    if not c and state == 1:
        c = cands(lambda v: v[0] == size and v[1] == 0)
    if not c:
        same = [i for i, v in enumerate(variants) if v[1] == state] or [i for i, v in enumerate(variants) if v[1] == 0]
        best = min(abs(variants[i][0] - size) for i in same)
        c = [i for i in same if abs(variants[i][0] - size) == best]
    return np.asarray(c)[rng.integers(0, len(c), n)]


def _target_species(name, rule, available):
    if isinstance(rule, str):
        return rule
    t = rule.get(name, name)
    if t in available:
        return t
    group = CONIFERS if t in CONIFERS else BROADLEAF
    for alt in group:
        if alt in available:
            return alt
    return available[0]


def forest(H, hmin, sp, sc, biome, treeline, valley, donors, species, biome_species, seed, max_trees=DEFAULT_TREES,
           tree_slope=TREE_SLOPE, treeline_fade=0.0):
    """-> (record bytes, table bytes, header tail, {species: count})
    donors: {'mrg': skob, 'sb': skob}; species: {short name: [(size, state, trunk)]}; biome_species: short names"""
    rng = np.random.default_rng(seed)
    spec = biomes.BIOMES[biome]
    hdr_tail = donors['mrg']['hdr'][16:]
    if not spec['donor'] or not biome_species:
        return b'', encode_table([]), hdr_tail, {}
    mask = open_ground(H, hmin, sp, sc, treeline, valley)
    weight = None
    if treeline > 0 and treeline_fade > 0:                 # forest thins out over the last treeline_fade m below it
        weight = np.clip((treeline - ((H.astype(np.float64) + 32768) * sc + hmin)) / treeline_fade, 0.0, 1.0)
    x, y, z = plant(H, sp, sc, donors['mrg']['records'], rng, mask, max_trees=max_trees, tree_slope=tree_slope,
                    weight=weight)
    m = len(x)
    donor = donors[spec['donor']]
    hdr_tail = donor['hdr'][16:]
    d_rec = np.frombuffer(donor['records'], np.uint8).reshape(-1, STRIDE)
    d_ents = table_entries(donor['table'])

    # each tree copies a donor tree from the same relative elevation band (yaw, size, scale, species / state)
    d_z = d_rec[:, 8:12].copy().view('<i4').ravel().astype(np.float64)
    lo, hi = np.percentile(d_z, [1, 99])
    d_band = np.clip(((d_z - lo) / max(hi - lo, 1.0) * BANDS).astype(int), 0, BANDS - 1)
    zl, zh = np.percentile(z, [1, 99]) if m else (0.0, 1.0)
    band = np.clip(((z - zl) / max(zh - zl, 1.0) * BANDS).astype(int), 0, BANDS - 1)
    rec = np.zeros((m, STRIDE), np.uint8)
    rec[:, 0:4] = np.rint(x * 1000).astype('<i4').view(np.uint8).reshape(-1, 4)
    rec[:, 4:8] = np.rint(y * 1000).astype('<i4').view(np.uint8).reshape(-1, 4)
    rec[:, 8:12] = np.rint(z * 1000).astype('<i4').view(np.uint8).reshape(-1, 4)
    for b in range(BANDS):
        idx = np.nonzero(band == b)[0]
        pool = np.nonzero(d_band == b)[0]
        if len(idx):
            rec[idx, 12:20] = d_rec[rng.choice(pool if len(pool) else np.arange(len(d_rec)), len(idx)), 12:20]
    d_var = rec[:, 18:20].copy().view('<u2').ravel()

    # donor species -> biome species, then the matching (size, state) variant of that species
    avail = [s for s in biome_species if s in species]
    if not avail:
        return b'', encode_table([]), hdr_tail, {}
    tgt_sp = np.empty(m, object)
    for i, (sp_name, a, b, _) in enumerate(d_ents):
        sel = d_var == i
        if sel.any():
            tgt_sp[sel] = _target_species(sp_name.replace('DA_TreeSpecies_', ''), spec['map'], avail)
    size = np.array([e[1] for e in d_ents], np.int64)[d_var]
    state = np.array([e[2] for e in d_ents], np.int64)[d_var]
    elev = z + hmin
    if spec.get('larch') and 'Larch' in avail:              # larch stands among the upper conifers
        upper = np.isin(tgt_sp, [s for s in CONIFERS if s != 'Larch']) & (band >= BANDS // 2) & (state != 3)
        tgt_sp[upper & (rng.random(m) < spec['larch'])] = 'Larch'
    if spec.get('oak') and 'Oak' in avail:                  # Gambel oak scrub low down (Utah)
        top, span, pmax = spec['oak']
        p_oak = np.clip((top - elev) / span, 0.0, pmax)
        oak = (rng.random(m) < p_oak) & (state != 3)
        tgt_sp[oak] = 'Oak'
        size[oak] = np.minimum(size[oak], np.where(rng.random(int(oak.sum())) < 0.3, 2, 1))
        state[oak] = np.where(state[oak] == 2, 2, 0)

    table, index = [], {}
    var = np.zeros(m, np.uint16)
    counts = {}
    for sp_name in avail:
        vs = species[sp_name]
        mine = tgt_sp == sp_name
        if not mine.any():
            continue
        counts[sp_name] = int(mine.sum())
        for s in range(4):
            for t in range(4):
                sel = np.nonzero(mine & (size == s) & (state == t))[0]
                if not len(sel):
                    continue
                picks = _pick_variant(vs, s, t, rng, len(sel))
                for vi in np.unique(picks):
                    key = (sp_name,) + tuple(vs[vi])
                    j = index.get(key)
                    if j is None:
                        j = index[key] = len(table)
                        table.append(('DA_TreeSpecies_' + sp_name,) + key[1:])
                    var[sel[picks == vi]] = j
    rec[:, 18:20] = var.astype('<u2').view(np.uint8).reshape(-1, 2)
    log.info('forest: %d trees, %s', m, ', '.join('%s %d' % kv for kv in sorted(counts.items(), key=lambda kv: -kv[1])))
    return rec.tobytes(), encode_table(table), hdr_tail, counts
