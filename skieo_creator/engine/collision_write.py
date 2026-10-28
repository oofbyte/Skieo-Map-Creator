"""Rewrite LandscapeHeightfieldCollisionComponent heightfields (Chaos cooked data) for edited terrain.

Per component (64x64 samples):
  props: CachedLocalBox z range (doubles, H/128)
  after the '00 10 00 00' marker: 4096 u16 LE heights quantised over [min,max] (signed H units)
  rest[12:20] min (f64), [20:28] max (f64), [32:40] range (f64), [40:48] range/65535 (f64)
  bounds table: u32 count 121, then 11x11 blocks (6-quad blocks, 7x7 samples overlap) of (min,max) u16 row-major
  last 20 bytes: min (f32) at -20.. , max (f32) at -4
"""
import logging
import struct
import sys
from pathlib import Path

import numpy as np

from . import component_bounds as cb

log = logging.getLogger(__name__)

N = 64
HB = N * N * 2


def collision_exports(umap_bytes):
    from . import legacy_header as lh
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
        if cls < 0 and imports[-cls - 1] == 'LandscapeHeightfieldCollisionComponent':
            out.append((size, off - th))
    return out


def quantise(raw, mn, mx):
    if mx == mn:
        return np.zeros_like(raw, dtype=np.int64)
    return np.floor((raw - mn) * 65535.0 / (mx - mn)).astype(np.int64)


def bounds_table(w):
    out = np.zeros((121, 2), dtype='<u2')
    i = 0
    for a in range(11):
        for c in range(11):
            blk = w[a * 6:min(64, (a + 1) * 6 + 1), c * 6:min(64, (c + 1) * 6 + 1)]
            out[i] = (blk.min(), blk.max())
            i += 1
    return out


def patch_collision(umap_bytes, uexp_bytes, Hg_orig, Hg_new, verify=True):
    comps = collision_exports(umap_bytes)
    assert len(comps) == 1024
    Ho = np.asarray(Hg_orig)
    Hn = np.asarray(Hg_new)
    index = {}
    for cy in range(32):
        for cx in range(32):
            blk = Ho[cy * 63:cy * 63 + 64, cx * 63:cx * 63 + 64]
            index.setdefault((int(blk.min()), int(blk.max())), []).append((cx, cy))
    out = bytearray(uexp_bytes)
    bad = 0
    for size, off in comps:
        d = bytes(uexp_bytes[off:off + size])
        zi, za = cb.find_box(d)
        mn = round(struct.unpack_from('<d', d, zi)[0] * 128)
        mx = round(struct.unpack_from('<d', d, za)[0] * 128)
        cand = index[(mn, mx)]
        cx, cy = cand[0]
        hp = d.find(b'\x00\x10\x00\x00', 0x70) + 4
        w_old = np.frombuffer(d[hp:hp + HB], dtype='<u2').astype(np.int64).reshape(N, N)
        blk_o = Ho[cy * 63:cy * 63 + 64, cx * 63:cx * 63 + 64].astype(np.int64)
        if verify and not (np.abs(quantise(blk_o, mn, mx) - w_old).max() <= 1):
            bad += 1
            continue
        blk_n = Hn[cy * 63:cy * 63 + 64, cx * 63:cx * 63 + 64].astype(np.int64)
        nmn, nmx = int(blk_n.min()), int(blk_n.max())
        w = quantise(blk_n, nmn, nmx)
        o = bytearray(d)
        # props box (z in H/128 units)
        struct.pack_into('<d', o, zi, nmn / 128.0)
        struct.pack_into('<d', o, za, nmx / 128.0)
        # heights
        o[hp:hp + HB] = w.astype('<u2').tobytes()
        r0 = hp + HB
        rng = float(nmx - nmn)
        struct.pack_into('<d', o, r0 + 12, float(nmn))
        struct.pack_into('<d', o, r0 + 20, float(nmx))
        struct.pack_into('<d', o, r0 + 32, rng)
        struct.pack_into('<d', o, r0 + 40, rng / 65535.0)
        # bounds table: locate old table bytes
        old_tab = bounds_table(w_old).tobytes()
        tp = d.find(old_tab, r0)
        assert tp > 0, 'bounds table not found'
        o[tp:tp + len(old_tab)] = bounds_table(w).tobytes()
        struct.pack_into('<f', o, len(o) - 20, float(nmn))
        struct.pack_into('<f', o, len(o) - 4, float(nmx))
        out[off:off + size] = o
    log.info('collision components rewritten (skipped %d)' % bad)
    return bytes(out)
