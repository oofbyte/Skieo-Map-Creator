"""Decoder / encoder for the Ski-E-O! landscape height storage.

Format (found by reverse engineering, verified against 255k tree heights):
  <map>.ubulk = 16 tile blobs of 335,239 big-endian int16 each.
  Each blob starts with the full-resolution 505 x 505 tile (row-major, 255,025 values),
  followed by the mip chain (not needed for decoding mip 0).
  Mip 0 is a single running sum over the whole tile in reading order:
        T[n] = (T[n-1] + v[n]) mod 65536,   T[-1] = 0
  so the first value of each row is the step from the previous row's last pixel.
  T is the unsigned 16-bit landscape height: world Z = origin + (T - 32768) * ZScale / 128.
  Tiles are laid out in 4 columns (tx) x 4 rows (ty) of 504-quad blocks (505 vertices, 1 shared).
"""
import array
from pathlib import Path

W = 505
TILE_VALUES = 335239
MIP0 = W * W
# LAYOUT[tx][ty] -> file tile index (found by exact shared-edge matching + tree heights)
LAYOUT = [[0, 10, 14, 4], [1, 11, 15, 5], [8, 12, 2, 6], [9, 13, 3, 7]]


def load_tile_stream(ubulk: bytes, t: int) -> array.array:
    a = array.array('h')
    a.frombytes(ubulk[t * TILE_VALUES * 2:(t + 1) * TILE_VALUES * 2])
    a.byteswap()
    return a


def decode_mip0(ubulk: bytes, t: int):
    """Return list of W rows of unsigned 16-bit heights."""
    a = load_tile_stream(ubulk, t)
    out = []
    acc = 0
    for r in range(W):
        row = []
        base = r * W
        for c in range(W):
            acc = (acc + a[base + c]) & 0xFFFF
            row.append(acc)
        out.append(row)
    return out


def encode_mip0(rows):
    """Inverse of decode_mip0: heights (W x W of 0..65535) -> signed deltas, as list of int."""
    vals = []
    prev = 0
    for r in range(W):
        for c in range(W):
            d = (rows[r][c] - prev) & 0xFFFF
            if d >= 0x8000:
                d -= 0x10000
            vals.append(d)
            prev = rows[r][c]
    return vals
