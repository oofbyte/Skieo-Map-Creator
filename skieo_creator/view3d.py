"""Data for the full-window 3D terrain view: a height grid (uint16, every 2nd landscape vertex) and a colour texture
(forest below the treeline, snow and rock above it, rock on steep ground, water on dead-flat ground)."""
import io

import numpy as np
from PIL import Image

STEP = 2                     # 2017 -> 1009 x 1009 vertices (~2M triangles)
TEX = 2048


def _slope(d, sp):
    gy, gx = np.gradient(d, sp)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def _blur(a, r):
    """box blur of radius r (two passes ~ gaussian), edge clamped"""
    out = a.astype(np.float64)
    for _ in range(2):
        for axis in (0, 1):
            p = np.pad(out, [(r + 1, r) if ax == axis else (0, 0) for ax in (0, 1)], mode='edge')
            cs = np.cumsum(p, axis=axis)
            n = out.shape[axis]
            hi = cs[2 * r + 1:2 * r + 1 + n] if axis == 0 else cs[:, 2 * r + 1:2 * r + 1 + n]
            lo = cs[:n] if axis == 0 else cs[:, :n]
            out = (hi - lo) / (2 * r + 1)
    return out


def _noise(n, cells, seed):
    """smooth value noise in [-0.5, 0.5], `cells` features across"""
    rs = np.random.default_rng(seed)
    small = _blur(rs.random((cells + 3, cells + 3)), 1)
    small = (small - small.min()) / max(np.ptp(small), 1e-9)
    img = Image.fromarray((small * 65535).astype(np.uint16)).resize((n, n), Image.BICUBIC)
    return np.asarray(img).astype(np.float64) / 65535.0 - 0.5


def _local_max(a, r):
    out = a.copy()
    p = np.pad(a, r, mode='edge')
    n0, n1 = a.shape
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            np.maximum(out, p[dy:dy + n0, dx:dx + n1], out=out)
    return out


def build(dem, size_m, treeline_m):
    """-> (heights uint16 bytes, meta dict, albedo JPEG bytes)"""
    d = np.asarray(dem, np.float64)
    n0 = d.shape[0]
    sp = size_m / (n0 - 1)
    lo, hi = float(d.min()), float(d.max())
    rng = max(hi - lo, 1.0)
    g = d[::STEP, ::STEP]
    q = np.clip(np.rint((g - lo) / rng * 65535.0), 0, 65535).astype('<u2')
    meta = dict(n=int(g.shape[0]), hmin=lo, hmax=hi, size_m=float(size_m))

    slope = _slope(_blur(d, max(1, int(round(12.0 / sp)))), sp)       # ~25 m scale: no rock on every creek bank
    big, fine = _noise(n0, 24, 5), _noise(n0, 96, 6)
    forest = np.array([60, 86, 62], float)
    meadow = np.array([104, 120, 84], float)
    rock = np.array([124, 118, 110], float)
    snow = np.array([236, 241, 246], float)
    water = np.array([78, 108, 136], float)
    tl = treeline_m if treeline_m and treeline_m > 0 else hi + 1000.0
    # soft treeline band (~120 m) with a ragged edge, meadow patches in the forest
    above = np.clip((d - tl + 60.0 + big * 120.0) / 120.0, 0.0, 1.0)[..., None]
    m = np.clip(fine * 2.0 - 0.3, 0.0, 0.35)[..., None]
    col = (forest[None, None] * (1 - m) + meadow[None, None] * m) * (1 - above) + snow[None, None] * above
    steep = np.clip((slope - 26.0) / 7.0, 0.0, 1.0)[..., None]       # bare rock from ~33 deg, where the forest stops
    col = col * (1 - steep) + rock[None, None] * steep
    flat = (_local_max(_slope(d, sp), max(3, int(round(30.0 / sp)))) < 0.6)[..., None]   # lakes: dead flat over ~60 m
    col = np.where(flat, water[None, None], col)
    col *= (1.0 + fine[..., None] * 0.10)
    img = Image.fromarray(np.clip(col, 0, 255).astype(np.uint8)).resize((TEX, TEX), Image.BILINEAR)
    b = io.BytesIO()
    img.save(b, 'JPEG', quality=88)
    return q.tobytes(), meta, b.getvalue()
