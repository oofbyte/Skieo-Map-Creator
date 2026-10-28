"""Write a complete Ski-E-O! terrain ubulk from full-resolution heights.

Tile blob (335,239 big-endian int16 per tile) -- verified layout:
  mip0   505x505   running sum from 0                         [0, 255025)
  O1     2044      edge normals of mip0 (see normal_ring)
  mip1   249x249   running sum from 0                         [257069, 319070)
  O2     1020      edge normals of mip1
  mip2   121x121   running sum from 0                         [320090, 334731)
  O3     508       edge normals of mip2
Heights are signed 16-bit "T"; world metres = 306.8 + 0.0093628 * T.
Mips are made the way Unreal makes them (mip_chain), exact to rounding against the stock maps.
The game rebuilds a tile's normals from its heights, except along the tile's border (that needs the neighbour tile):
those come from the O blocks. Left at the donor's, they drew a line of wrong lighting along every tile border.
"""
import array

import numpy as np

W = 505
TILE_VALUES = 335239
O1_LEN, W1 = 2044, 249
O2_LEN, W2 = 1020, 121
O3_LEN = 508
MIP1_START = W * W + O1_LEN
MIP2_START = MIP1_START + W1 * W1 + O2_LEN
O1_START = W * W
O2_START = MIP1_START + W1 * W1
O3_START = MIP2_START + W2 * W2
assert O3_START + O3_LEN == TILE_VALUES
LAYOUT = [[0, 10, 14, 4], [1, 11, 15, 5], [8, 12, 2, 6], [9, 13, 3, 7]]   # LAYOUT[tx][ty] -> file tile
S16 = lambda v: v - 65536 if v >= 32768 else v


def raster(vals, start, w):
    acc = 0
    rows = []
    for r in range(w):
        row = []
        for c in range(w):
            acc = (acc + vals[start + r * w + c]) & 0xFFFF
            row.append(S16(acc))
        rows.append(row)
    return rows


def decode_tile(vals):
    """vals: array('h') of one tile blob (already byte-swapped) -> (mip0, mip1, mip2) as signed grids."""
    return raster(vals, 0, W), raster(vals, MIP1_START, W1), raster(vals, MIP2_START, W2)


def bilinear(A, ys, xs):
    """A (h, w) or (h, w, k) sampled at rows ys x columns xs (fractional)"""
    A = np.asarray(A, np.float64)
    y0 = np.clip(np.floor(ys).astype(int), 0, A.shape[0] - 2)
    x0 = np.clip(np.floor(xs).astype(int), 0, A.shape[1] - 2)
    sh = (-1, 1) + (1,) * (A.ndim - 2)
    fy, fx = (ys - y0).reshape(sh), (xs - x0).reshape((1, -1) + (1,) * (A.ndim - 2))
    return (A[np.ix_(y0, x0)] * (1 - fx) * (1 - fy) + A[np.ix_(y0, x0 + 1)] * fx * (1 - fy)
            + A[np.ix_(y0 + 1, x0)] * (1 - fx) * fy + A[np.ix_(y0 + 1, x0 + 1)] * fx * fy)


# (quads per component in the level above, in this level): mip1 .. mip4 of the 8 x 8 components of a tile
MIP_STEPS = ((63, 31), (31, 15), (15, 7), (7, 3))


def mip_chain(A):
    """Unreal's landscape mips: every level samples the one above bilinearly at the same place of each component
    (vertex k of a component with q quads <- position k * q_above / q). Verified on the stock maps: exact to
    rounding. Component borders land on whole vertices, so neighbouring tiles always agree there -- the box
    averages used before differed across tile borders and opened cracks in far-away terrain.
    A: 505 x 505 (heights, or (505, 505, 2) normal bytes). Returns [mip0 .. mip4] (505, 249, 121, 57, 25 wide)."""
    out = [np.asarray(A, np.float64)]
    for qs, qd in MIP_STEPS:
        p = np.array([c * qs + k * qs / qd for c in range(8) for k in range(qd)] + [8 * qs], np.float64)
        out.append(np.rint(bilinear(out[-1], p, p)))
    return out


def normals(H, sp_cm, sc_cm):
    """Unreal's landscape vertex normals (sum of the two triangle normals of each of the 4 quads around a vertex)
    as the B, A bytes of the heightmap texture: (n, n, 2) floats 0..255. sp_cm = vertex spacing, sc_cm = height unit."""
    Z = np.asarray(H, np.float64) * sc_cm
    n0, n1 = Z.shape
    z00, z10, z01, z11 = Z[:-1, :-1], Z[:-1, 1:], Z[1:, :-1], Z[1:, 1:]
    zero, s = np.zeros_like(z00), np.full_like(z00, sp_cm)
    v00, v10 = np.stack([zero, zero, z00], -1), np.stack([s, zero, z10], -1)
    v01, v11 = np.stack([zero, s, z01], -1), np.stack([s, s, z11], -1)
    f1 = np.cross(v00 - v10, v10 - v11)
    f2 = np.cross(v11 - v01, v01 - v00)
    fn = f1 / np.linalg.norm(f1, axis=-1, keepdims=True) + f2 / np.linalg.norm(f2, axis=-1, keepdims=True)
    N = np.zeros((n0, n1, 3))
    for sy, sx in ((0, 0), (0, 1), (1, 0), (1, 1)):
        N[sy:sy + n0 - 1, sx:sx + n1 - 1] += fn
    N /= np.linalg.norm(N, axis=-1, keepdims=True)
    N *= np.sign(N[..., 2:3])                       # facing up
    return np.rint(127.5 * (N[..., :2] + 1.0))


def encode_raster(grid):
    prev = 0
    vals = []
    for row in grid:
        for v in row:
            v = max(-32768, min(32767, int(v))) & 0xFFFF
            d = (v - prev) & 0xFFFF
            vals.append(d - 0x10000 if d >= 0x8000 else d)
            prev = v
    return vals


QUADS = (63,) + tuple(qd for _, qd in MIP_STEPS)          # quads per component at mip0 .. mip4


def normal_ring(D, q):
    """The border of one tile's heightmap texture, clockwise from the top-left corner: top row, right column, bottom
    row (right to left), left column (bottom to top). The texture keeps both copies of a vertex on each component
    border (8 components x (q+1) texels a side), so a ring is 4 * (8(q+1) - 1) texels. D: deduped (8q+1, 8q+1, 2)."""
    w = 8 * (q + 1)
    idx = np.array([(t // (q + 1)) * q + (t % (q + 1)) for t in range(w)])
    return np.concatenate([D[0, idx[:-1]], D[idx[:-1], -1], D[-1, idx[::-1][:-1]], D[idx[::-1][:-1], 0]])


def ring_deltas(ring):
    """byte-wise running deltas of (B, A) pairs, starting from 128 -> (n, 2) uint8"""
    r = np.asarray(ring, np.int64)
    prev = np.vstack([[128, 128], r[:-1]])
    return ((r - prev) & 255).astype(np.uint8)


def tile_blob(H, orig_vals, nrm=None):
    """H: 505x505 signed heights; nrm: the tile's (505, 505, 2) normal bytes (normals(), cut from the whole map so the
    border ones see the neighbour tile), or None to keep the original edge normals. orig_vals: the tile's original
    array('h')."""
    m = mip_chain(H)
    if nrm is not None:
        nm = mip_chain(nrm)
        rings = []
        for k in range(3):
            d = ring_deltas(normal_ring(nm[k], QUADS[k])).astype(np.int64)
            v = d[:, 0] << 8 | d[:, 1]                                # B delta in the high byte
            rings.append(np.where(v >= 0x8000, v - 0x10000, v).tolist())
    else:
        rings = [orig_vals[s:s + n] for s, n in ((O1_START, O1_LEN), (O2_START, O2_LEN), (O3_START, O3_LEN))]
    out = array.array('h')
    out.extend(encode_raster(H))
    out.extend(rings[0])
    out.extend(encode_raster(m[1].astype(np.int64).tolist()))
    out.extend(rings[1])
    out.extend(encode_raster(m[2].astype(np.int64).tolist()))
    out.extend(rings[2])
    assert len(out) == TILE_VALUES, len(out)
    return out


def to_bytes(vals):
    a = array.array('h', vals)
    a.byteswap()
    return a.tobytes()
