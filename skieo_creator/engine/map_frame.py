"""Fix the map frame of a donor map (Mad River Glen) when it carries a Sugar-Bowl-scaled terrain:
  * landscape root transform (location / scale) -> Sugar Bowl values
  * skirt mesh (procedural slab walls): rescale x,y to the new extent, top vertices follow the new edge heights
"""
import logging
import struct
import sys
from pathlib import Path

import numpy as np

from . import legacy_header as lh
from . import frame as FR

log = logging.getLogger(__name__)



def find_exports(umap_bytes, cls_want):
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
        if cls < 0 and imports[-cls - 1] == cls_want:
            out.append((names[nm_i], size, off - th))
    return out


def snap(c, E):
    """a wall vertex coordinate onto the nearest terrain vertex (the wall then meets the edge exactly, see
    builder.fit_edges); the corners (+-E) stay where they are"""
    return -E + round((c + E) / (2 * E) * (FR.G - 1)) * (2 * E) / (FR.G - 1)


def wall_stations(umap_bytes, uexp_bytes):
    """where the edge wall (skirt) has its top vertices along one side, as fractions 0..1 of the side. The wall is
    straight between these, so the terrain edge has to be too (builder.fit_edges)."""
    roots = [e for e in find_exports(umap_bytes, 'SceneComponent') if e[0] == 'RootComponent0']
    _, _, off = roots[0]
    E = abs(struct.unpack_from('<d', uexp_bytes, off + 4)[0])
    (_, size, off), = find_exports(umap_bytes, 'ProceduralMeshComponent')
    d = uexp_bytes[off:off + size]
    xs = set()
    p = 0
    while True:
        p = d.find(b'\x80\x11', p)
        if p < 0:
            break
        x, y, z = struct.unpack_from('<3d', d, p + 3)
        if abs(y + E) < 1.0 and abs(x) <= E + 1.0 and z != -1000.0:
            xs.add(round((x + E) / (2 * E), 6))
        p += 2
    st = sorted(xs)
    if len(st) < 8 or st[0] != 0.0 or st[-1] != 1.0:
        raise ValueError('edge wall layout changed (%d stations)' % len(st))
    return st


def patch_frame(umap_bytes, uexp_bytes, Hn):
    ROOT = FR.root()
    out = bytearray(uexp_bytes)
    # --- landscape root transform
    roots = [e for e in find_exports(umap_bytes, 'SceneComponent') if e[0] == 'RootComponent0']
    assert len(roots) == 1
    _, size, off = roots[0]
    old = struct.unpack_from('<7d', out, off + 4)
    struct.pack_into('<7d', out, off + 4, *ROOT)
    log.info('root transform: loc(%.0f,%.0f,%.0f) scale(%.2f,%.2f,%.2f) -> loc(%.0f,%.0f,%.0f) scale(%.2f,%.2f,%.2f)' % (old[:6] + ROOT[:6]))
    r = ROOT[0] / old[0]
    oldE = abs(old[0])
    newE = abs(ROOT[0])
    # --- skirt
    sk = find_exports(umap_bytes, 'ProceduralMeshComponent')
    assert len(sk) == 1
    _, size, off = sk[0]
    d = bytes(out[off:off + size])
    Hf = np.asarray(Hn, dtype=np.float64)

    def terrain_z(x, y):
        c = min(max((x + newE) / 100 / FR.SP, 0), 2016)
        rr = min(max((y + newE) / 100 / FR.SP, 0), 2016)
        c0, r0 = min(int(c), 2015), min(int(rr), 2015)
        fx, fy = c - c0, rr - r0
        h = Hf[r0, c0] * (1 - fx) * (1 - fy) + Hf[r0, c0 + 1] * fx * (1 - fy) + Hf[r0 + 1, c0] * (1 - fx) * fy + Hf[r0 + 1, c0 + 1] * fx * fy
        return (h + 32768) * FR.SC * 100

    nrec = 0
    tops = []
    p = 0
    while True:
        p = d.find(b'\x80\x11', p)
        if p < 0:
            break
        x, y, z = struct.unpack_from('<3d', d, p + 3)
        if abs(x) < 3e5 and abs(y) < 3e5 and abs(z) < 1e5 and abs(max(abs(x), abs(y)) - oldE) < 1.0:
            nx, ny = snap(x * r, newE), snap(y * r, newE)
            nz = z if z == -1000.0 else terrain_z(nx, ny)
            struct.pack_into('<3d', out, off + p + 3, nx, ny, nz)
            nrec += 1
            if z != -1000.0:
                tops.append(nz)
        p += 2
    log.info('skirt vertices rewritten: %d, top z range %.0f..%.0f cm', nrec, min(tops), max(tops))
    # section box / other occurrences of the old extent or old max z
    pat_pos = struct.pack('<d', oldE)
    pat_neg = struct.pack('<d', -oldE)
    extra = 0
    cur = bytes(out[off:off + size])
    for pat, val in ((pat_pos, newE), (pat_neg, -newE)):
        q = 0
        while True:
            q = cur.find(pat, q)
            if q < 0:
                break
            struct.pack_into('<d', out, off + q, val)
            extra += 1
            q += 8
    log.info('extra extent doubles replaced: %d', extra)
    return bytes(out)
