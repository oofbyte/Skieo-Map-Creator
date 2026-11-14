"""Tree canopy heights from the Meta / WRI global canopy height map v2 (about 1 m, from 2018-2020 satellite images;
CC BY 4.0), read straight from its cloud-optimized GeoTIFFs on AWS Open Data.

Each file is one zoom-10 Web Mercator tile (named by its quadkey) of 32768 x 32768 pixels, heights in whole metres, in
512 x 512 pixel DEFLATE blocks, with overviews. Only the blocks under the points asked for are downloaded (at the 2x
overview: about 1.7 m on the ground, enough to see an 8 m cat track through the forest) and kept on disk."""
import io
import logging
import math
import zlib
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import net, paths

log = logging.getLogger(__name__)

URL = 'https://dataforgood-fb-data.s3.amazonaws.com/forests/v2/global/dinov3_global_chm_v2_ml3/chm/%s.tif'
CACHE = paths.APP_DIR / 'cache' / 'canopy'
CACHE_MB = 400                      # older blocks are deleted beyond this
Z = 10                              # the files are zoom-10 tiles
LEVEL = 1                           # 0 = full resolution (1.19 m Mercator pixels), 1 = the 2x overview, ...
HEAD = 131072                       # all the files' directories are in their first ~91 KB
R = 6378137.0
WORLD = 2 * math.pi * R


class CanopyError(Exception):
    pass


def _quadkey(tx, ty):
    return ''.join(str(((tx >> b) & 1) + 2 * ((ty >> b) & 1)) for b in range(Z - 1, -1, -1))


def _get(key, part, byte_range, cancel):
    f = CACHE / ('%s_%s.bin' % (key, part))
    if f.is_file():
        return f.read_bytes()
    b = net.get(URL % key, timeout=60, retries=2, cancel=cancel, not_found_ok=True, byte_range=byte_range, per_host=8)
    b = b or b''                                     # no file: open sea
    paths.atomic_write(f, b)
    return b


class _File:
    """one zoom-10 tile's image at LEVEL: block table and where its pixels are"""

    def __init__(self, tx, ty, cancel):
        self.key = _quadkey(tx, ty)
        head = _get(self.key, 'head', (0, HEAD - 1), cancel)
        self.ok = bool(head)
        if not self.ok:
            return
        import tifffile
        try:
            with tifffile.TiffFile(io.BytesIO(head)) as t:
                full = t.pages[0]
                scale = full.tags['ModelPixelScaleTag'].value[0]
                tie = full.tags['ModelTiepointTag'].value
                want = full.imagewidth >> LEVEL
                p = next(p for p in t.pages if not p.is_mask and p.imagewidth == want)
                good = p.compression == 8 and p.predictor == 1 and p.dtype == np.uint8 and p.is_tiled
                self.offsets, self.counts = list(p.dataoffsets), list(p.databytecounts)
                self.bw, self.bh, self.w = p.tilewidth, p.tilelength, p.imagewidth
        except (tifffile.TiffFileError, KeyError, IndexError, StopIteration, ValueError) as ex:
            raise CanopyError('unreadable canopy file %s: %s' % (self.key, ex))
        if not good:
            raise CanopyError('unexpected canopy file layout in %s' % self.key)
        self.x0, self.y0 = tie[3], tie[4]                # top-left corner, Web Mercator metres
        self.px = scale * (1 << LEVEL)
        self.across = -(-self.w // self.bw)

    def block(self, i, cancel):
        if not self.counts[i]:
            return np.zeros((self.bh, self.bw), np.uint8)
        b = _get(self.key, '%d_%d' % (LEVEL, i), (self.offsets[i], self.offsets[i] + self.counts[i] - 1), cancel)
        try:
            return np.frombuffer(zlib.decompress(b), np.uint8).reshape(self.bh, self.bw)
        except (zlib.error, ValueError) as ex:
            (CACHE / ('%s_%d_%d.bin' % (self.key, LEVEL, i))).unlink(missing_ok=True)
            raise CanopyError('damaged canopy block %s/%d: %s' % (self.key, i, ex))


def heights(lat, lon, cancel=net.NEVER, progress=lambda f, t: None):
    """canopy height in metres at each lat/lon point (0 = no trees, or no data)"""
    lat = np.clip(np.asarray(lat, np.float64).ravel(), -85.0, 85.0)
    lon = np.asarray(lon, np.float64).ravel()
    mx = np.radians(lon) * R + WORLD / 2                               # metres from the west edge of the world map
    my = WORLD / 2 - np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * R   # ... from its north edge
    n = 1 << Z
    tx = np.clip((mx / WORLD * n).astype(np.int64), 0, n - 1)
    ty = np.clip((my / WORLD * n).astype(np.int64), 0, n - 1)
    out = np.zeros(lat.shape, np.uint8)
    files, jobs = {}, {}
    for t in set(zip(tx.tolist(), ty.tolist())):
        files[t] = f = _File(t[0], t[1], cancel)
        if not f.ok:
            continue
        sel = np.nonzero((tx == t[0]) & (ty == t[1]))[0]
        col = np.clip(((mx[sel] - WORLD / 2 - f.x0) / f.px).astype(np.int64), 0, f.w - 1)
        row = np.clip(((f.y0 - (WORLD / 2 - my[sel])) / f.px).astype(np.int64), 0, f.w - 1)
        blk = (row // f.bh) * f.across + col // f.bw
        for b in np.unique(blk).tolist():
            m = blk == b
            jobs[(t, b)] = (sel[m], row[m] % f.bh, col[m] % f.bw)
    done = [0]

    def fetch(key):
        a = files[key[0]].block(key[1], cancel)
        done[0] += 1
        progress(done[0] / len(jobs), 'Downloading the satellite tree heights...')
        return key, a
    with ThreadPoolExecutor(8) as ex:
        for key, a in ex.map(fetch, list(jobs)):
            sel, r, c = jobs[key]
            out[sel] = a[r, c]
    _trim()
    return out


def _trim():
    """keep the block cache under CACHE_MB (oldest first)"""
    try:
        fs = sorted(CACHE.glob('*.bin'), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in fs)
        for p in fs:
            if total <= CACHE_MB << 20:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
    except OSError as ex:
        log.warning('canopy cache trim: %s', ex)
