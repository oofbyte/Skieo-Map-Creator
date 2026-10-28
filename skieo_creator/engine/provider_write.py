"""Storage-provider blocks (one per terrain texture, 10,082 bytes each, inside the map .uexp).

Layout (verified on all 16):
  [0,118)      header, copied, except the landscape grid scale: f64 x / y spacing (cm per vertex) at +26 / +34 and
               f64 z scale (cm per 128 height units) at +42, the same three numbers as the landscape root's scale.
               The game rebuilds the tile's normals from its heights with these; left at the donor's, every tile was
               lit as if the ground were steeper / flatter, and only its border (normal_ring) was right: a line.
  [118,6616)   mip3: 57x57 big-endian int16 running sum from 0
  [6616,7120)  edge normals of mip3: terrain_write.normal_ring, (B, A) byte deltas from 128
  [7120,7136)  mip4 header (32, 32, 1, id)                      copied
  [7136,8386)  mip4: 25x25 big-endian int16 running sum from 0
  [8386,8634)  edge normals of mip4
  mips 5..9:   16-byte header (w,h,0,id) + w*h*4 raw BGRA bytes: R,G = height as u16 (T+32768), B,A = normal x,y
"""
import struct
import sys
from pathlib import Path

import numpy as np

from . import terrain_write as tw

PROV_SIZE = 10082
M3_START, M3_W = 118, 57
A3_START = M3_START + M3_W * M3_W * 2          # 6616
M4_START, M4_W = 7136, 25
A4_START = M4_START + M4_W * M4_W * 2          # 8386
CHUNKS = [(5, 16, 8634), (6, 8, 9674), (7, 4, 9946), (8, 2, 10026), (9, 1, 10058)]   # (mip, dim, header offset)
SCALE_AT = (26, 34, 42)                         # landscape grid scale x, y, z (f64, cm) in the header


def decode_raster_be(d, start, w):
    vals = struct.unpack_from('>%dh' % (w * w), d, start)
    acc = 0
    rows = []
    for r in range(w):
        row = []
        for c in range(w):
            acc = (acc + vals[r * w + c]) & 0xFFFF
            row.append(tw.S16(acc))
        rows.append(row)
    return rows


def encode_raster_be(grid):
    prev = 0
    out = []
    for row in grid:
        for v in row:
            v = max(-32768, min(32767, int(v))) & 0xFFFF
            dlt = (v - prev) & 0xFFFF
            out.append(dlt - 0x10000 if dlt >= 0x8000 else dlt)
            prev = v
    return struct.pack('>%dh' % len(out), *out)


MIP5_POS = np.array([c * 3 + k * 3 for c in range(8) for k in range(2)])   # mip5: each component's two ends in mip4


def build_provider(orig, H, nrm=None, scale=None):
    """orig: original 10,082-byte payload; H: this tile's 505x505 signed heights; nrm: its (505, 505, 2) normal bytes
    (terrain_write.normals) or None to keep the original normals; scale: the landscape's (x, y, z) scale in cm (the
    root transform's) or None to keep the original. Returns the new payload bytes.
    mip3/4 continue terrain_write.mip_chain; mip5 (2 px per component) takes each component's corners from mip4;
    mips 6..8 are byte-wise averages (rounded down) of the level above, like a plain texture; the 1 px mip9 is kept.
    All of it matches the stock maps exactly (heights) or within a few steps (normals)."""
    out = bytearray(orig)
    if scale is not None:
        old = [struct.unpack_from('<d', orig, o)[0] for o in SCALE_AT]
        if not (old[0] == old[1] and 10.0 < old[0] < 10000.0 and 1.0 < old[2] < 100000.0):
            raise ValueError('terrain texture header layout changed (%r)' % (old,))
        for o, v in zip(SCALE_AT, scale):
            struct.pack_into('<d', out, o, float(v))
    m = tw.mip_chain(H)
    m3 = encode_raster_be(m[3].astype(np.int64).tolist())
    out[M3_START:M3_START + len(m3)] = m3
    m4 = encode_raster_be(m[4].astype(np.int64).tolist())
    out[M4_START:M4_START + len(m4)] = m4
    (_, dim5, ho5) = CHUNKS[0]
    px = np.frombuffer(orig, np.uint8, dim5 * dim5 * 4, ho5 + 16).reshape(dim5, dim5, 4).astype(np.int64)
    u = np.clip(m[4][np.ix_(MIP5_POS, MIP5_POS)].astype(np.int64) + 32768, 0, 65535)
    px[..., 2], px[..., 1] = u >> 8, u & 255                     # R, G = height; B, A = normal x, y
    if nrm is not None:
        nm = tw.mip_chain(nrm)
        for k, start in ((3, A3_START), (4, A4_START)):
            ring = tw.ring_deltas(tw.normal_ring(nm[k], tw.QUADS[k])).tobytes()
            out[start:start + len(ring)] = ring
        n4 = nm[4]
        px[..., 0], px[..., 3] = n4[np.ix_(MIP5_POS, MIP5_POS)][..., 0], n4[np.ix_(MIP5_POS, MIP5_POS)][..., 1]
    out[ho5 + 16:ho5 + 16 + px.size] = px.astype(np.uint8).tobytes()
    for mip, dim, ho in CHUNKS[1:-1]:
        px = px.reshape(dim, 2, dim, 2, 4).sum(axis=(1, 3)) // 4
        out[ho + 16:ho + 16 + px.size] = px.astype(np.uint8).tobytes()
    return bytes(out)


def patch_providers(umap_bytes, uexp_bytes, heights_by_tile, normals_by_tile=None, scale=None):
    """heights_by_tile: {file_tile_index: 505x505 signed heights}; normals_by_tile: {same: (505, 505, 2) normal bytes}
    or None; scale: the landscape's (x, y, z) scale in cm or None. Patches the 16 storage-provider exports in place."""
    from . import legacy_header as lh
    f, names, _, _ = lh.parse(umap_bytes)
    th = f['total_header_size'][1]
    n_imp, imp_off = f['imports_count'][1], f['imports_offset'][1]
    imports = []
    for i in range(n_imp):
        cpk, cpn, cnm, cnn, outer, obj, objn = struct.unpack_from('<iiiiiii', umap_bytes, imp_off + i * 32)
        imports.append(names[obj])
    n_exp, exp_off = f['exports_count'][1], f['exports_offset'][1]
    nxt = f['cell_imports_offset'][1] if f['cell_imports_offset'][1] > 0 else f['depends_offset'][1]
    stride = (nxt - exp_off) // n_exp
    provs = []
    for i in range(n_exp):
        o = exp_off + i * stride
        cls, sup, tmpl, outer, nm_i, nm_n, flags, size, off = struct.unpack_from('<iiiiiiIqq', umap_bytes, o)
        if cls < 0 and imports[-cls - 1] == 'LandscapeTextureStorageProviderFactory':
            provs.append((size, off - th))
    assert len(provs) == 16 and all(p[0] == PROV_SIZE for p in provs)
    out = bytearray(uexp_bytes)
    for k, (size, off) in enumerate(provs):
        if k in heights_by_tile:
            out[off:off + size] = build_provider(bytes(uexp_bytes[off:off + size]), heights_by_tile[k],
                                                 (normals_by_tile or {}).get(k), scale)
    return bytes(out)
