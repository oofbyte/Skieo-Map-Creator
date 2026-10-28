"""Recompute LandscapeComponent CachedLocalBox z ranges (local z = H/128) after editing terrain heights."""
import logging
import struct
import sys
from pathlib import Path

from . import legacy_header as lh

log = logging.getLogger(__name__)

G = 2017


def global_grid(heights_by_tile, layout):
    """heights_by_tile {file_tile: 505x505}; layout[tx][ty] -> file tile. Returns G x G list of rows (y, x)."""
    Hg = [[0] * G for _ in range(G)]
    for tx in range(4):
        for ty in range(4):
            H = heights_by_tile[layout[tx][ty]]
            for r in range(505):
                row = Hg[ty * 504 + r]
                row[tx * 504:tx * 504 + 505] = H[r]
    return Hg


def comp_range(Hg, cx, cy):
    vals = [v for r in range(64) for v in Hg[cy * 63 + r][cx * 63:cx * 63 + 64]]
    return min(vals), max(vals)


def find_box(d):
    for o in range(16, len(d) - 30):
        a, b = struct.unpack_from('<dd', d, o)
        if a == 63.0 and b == 63.0 and d[o + 24] == 1:
            return o - 8, o + 16
    raise ValueError('box not found')


def component_exports(umap_bytes):
    f, names, _, _ = lh.parse(umap_bytes)
    th = f['total_header_size'][1]
    n_imp, imp_off = f['imports_count'][1], f['imports_offset'][1]
    imports = [names[struct.unpack_from('<iiiiiii', umap_bytes, imp_off + i * 32)[5]] for i in range(n_imp)]
    n_exp, exp_off = f['exports_count'][1], f['exports_offset'][1]
    nxt = f['cell_imports_offset'][1] if f['cell_imports_offset'][1] > 0 else f['depends_offset'][1]
    stride = (nxt - exp_off) // n_exp
    out = []
    for i in range(n_exp):
        o = exp_off + i * stride
        cls, sup, tmpl, outer, nm_i, nm_n, flags, size, off = struct.unpack_from('<iiiiiiIqq', umap_bytes, o)
        if cls < 0 and imports[-cls - 1] == 'LandscapeComponent':
            out.append((size, off - th))
    return out


def patch_boxes(umap_bytes, uexp_bytes, Hg_orig, Hg_new):
    comps = component_exports(umap_bytes)
    assert len(comps) == 1024
    index = {}
    for cy in range(32):
        for cx in range(32):
            index.setdefault(comp_range(Hg_orig, cx, cy), []).append((cx, cy))
    out = bytearray(uexp_bytes)
    changed = amb = miss = 0
    for size, off in comps:
        d = bytes(uexp_bytes[off:off + size])
        zi, za = find_box(d)
        mn = struct.unpack_from('<d', d, zi)[0]
        mx = struct.unpack_from('<d', d, za)[0]
        cand = index.get((round(mn * 128), round(mx * 128)))
        if not cand:
            miss += 1
            continue
        if len(cand) > 1:
            amb += 1
            # choose the candidate whose new range is widest (conservative)
        rngs = [comp_range(Hg_new, cx, cy) for cx, cy in cand]
        nmn = min(r[0] for r in rngs)
        nmx = max(r[1] for r in rngs)
        struct.pack_into('<d', out, off + zi, nmn / 128.0)
        struct.pack_into('<d', out, off + za, nmx / 128.0)
        if (nmn, nmx) != (round(mn * 128), round(mx * 128)):
            changed += 1
    log.info('component boxes: %d changed, %d ambiguous, %d unmatched' % (changed, amb, miss))
    return bytes(out)
