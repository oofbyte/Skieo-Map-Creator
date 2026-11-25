"""Elevation for a square terrain window, sampled onto the game's 2017 x 2017 landscape grid (row 0 = north).

Sources (registered in dem_sources.py): national and regional elevation models, 0.5 to 10 m and mostly lidar bare
earth, where they cover the window; USGS 3DEP (10 m) in the US; AWS Terrain Tiles (~30 m) everywhere, for whatever is
left. A window that several cover (a border, patchy lidar, gaps) takes the finest source first and fills its holes from
the next ones, matched to its level along the edge and blended into it (`merge`). Each source's raster is resampled
with cubic convolution onto a local transverse Mercator grid centred on the window (see geo.py), from its own map
projection (crs.py).

A map keeps the id of the sources it was built from ('ch_swissalti3d+terrarium'), so a rebuild makes the same terrain.
"""
import hashlib
import io
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

from . import crs, geo, net, paths

log = logging.getLogger(__name__)
G = 2017
CACHE_VERSION = 2
RAW_CACHE_MB = 1500                  # downloaded source tiles kept for the next window nearby
TDEP_URL = ('https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage?'
            'bbox={w:.7f},{s:.7f},{e:.7f},{n:.7f}&bboxSR=4326&imageSR=4326&size={nx},{ny}&format=tiff&pixelType=F32'
            '&noData=-9999&interpolation=RSP_BilinearInterpolation&f=image')
TERRARIUM_URL = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'
# rough boxes of 3DEP coverage (lon_min, lat_min, lon_max, lat_max): CONUS, Alaska, Hawaii, Puerto Rico
US_BOXES = ((-125.0, 24.4, -66.8, 49.5), (-170.0, 51.0, -129.5, 71.6), (-160.5, 18.8, -154.6, 22.4), (-67.4, 17.8, -65.2, 18.6))


class DemError(Exception):
    pass


def in_us(lat, lon):
    return any(a <= lon <= c and b <= lat <= d for a, b, c, d in US_BOXES)


def grid_latlon(lat, lon, size_m):
    g = geo.LocalGrid(lat, lon)
    s = np.arange(G) * (size_m / (G - 1)) - size_m / 2
    X, Y = np.meshgrid(s, -s)
    return g.to_latlon(X, Y)


class Window:
    """the square being loaded: its grid points' latitude/longitude, grid step and a padded lat/lon box"""

    def __init__(self, lat, lon, size_m):
        self.lat, self.lon, self.size_m = float(lat), float(lon), float(size_m)
        self.spacing = self.size_m / (G - 1)
        self.LA, self.LO = grid_latlon(lat, lon, size_m)
        pad_lat = 6 * self.spacing / 111320.0
        pad_lon = pad_lat / max(math.cos(math.radians(lat)), 0.05)
        self.box = (float(self.LO.min()) - pad_lon, float(self.LA.min()) - pad_lat,
                    float(self.LO.max()) + pad_lon, float(self.LA.max()) + pad_lat)
        self._xy = {}

    def xy(self, proj):
        """every grid point in the projection's coordinates (kept for the next source in the same projection; the
        projection is kept with them, so a later object can't take its id and get its coordinates)"""
        hit = self._xy.get(id(proj))
        if hit is None or hit[0] is not proj:
            hit = self._xy[id(proj)] = (proj, proj.forward(self.LA, self.LO))
        return hit[1]

    def native_box(self, proj):
        """(x_min, y_min, x_max, y_max) of the padded box in the projection's coordinates"""
        w, s, e, n = self.box
        t = np.linspace(0, 1, 65)
        lat = np.concatenate([s + (n - s) * t, s + (n - s) * t, np.full(65, s), np.full(65, n)])
        lon = np.concatenate([np.full(65, w), np.full(65, e), w + (e - w) * t, w + (e - w) * t])
        x, y = proj.forward(lat, lon)
        return float(np.min(x)), float(np.min(y)), float(np.max(x)), float(np.max(y))

    def pixel(self, res, per_step=1.1):
        """the pixel size (metres) to ask a source for: its own resolution, or `per_step` pixels per grid step when the
        grid is coarser (servers resample; finer would only cost downloads)"""
        return max(float(res), self.spacing / per_step)


# ---------------------------------------------------------------- resampling / smoothing without scipy

def _keys(t):
    """cubic convolution weights (a = -0.5) for the four taps at offsets -1, 0, 1, 2"""
    a = -0.5
    t2, t3 = t * t, t * t * t
    return ((a * (t3 - 2 * t2 + t)), ((a + 2) * t3 - (a + 3) * t2 + 1), (-(a + 2) * t3 + (2 * a + 3) * t2 - a * t),
            (a * (t2 - t3)))


def sample_cubic(img, rows, cols):
    """img[rows, cols] by cubic convolution at fractional pixel-centre coordinates (edge clamped)"""
    h, w = img.shape
    out = np.empty(rows.shape, np.float64)
    step = 256
    for a in range(0, rows.shape[0], step):
        r, c = rows[a:a + step], cols[a:a + step]
        r0, c0 = np.floor(r).astype(np.int64), np.floor(c).astype(np.int64)
        wr, wc = _keys(r - r0), _keys(c - c0)
        acc = np.zeros(r.shape, np.float64)
        for i in range(4):
            ri = np.clip(r0 + i - 1, 0, h - 1)
            row_acc = np.zeros(r.shape, np.float64)
            for j in range(4):
                row_acc += wc[j] * img[ri, np.clip(c0 + j - 1, 0, w - 1)]
            acc += wr[i] * row_acc
        out[a:a + step] = acc
    return out


def gaussian(a, sigma):
    if sigma <= 0:
        return a
    rad = int(math.ceil(4 * sigma))
    k = np.exp(-0.5 * (np.arange(-rad, rad + 1) / sigma) ** 2)
    k /= k.sum()
    out = a.astype(np.float64)
    for axis in (0, 1):
        p = np.pad(out, [(rad, rad) if ax == axis else (0, 0) for ax in (0, 1)], mode='edge')
        acc = np.zeros_like(out)
        n = out.shape[axis]
        for i, kv in enumerate(k):
            acc += kv * (p[i:i + n, :] if axis == 0 else p[:, i:i + n])
        out = acc
    return out


def sample_raster(img, x0, y0, dx, dy, proj, win):
    """a north-up raster whose top-left pixel corner is (x0, y0) in proj's coordinates, pixels dx by dy: its values at
    the window's grid points (NaN outside it and next to its holes)"""
    X, Y = win.xy(proj)
    cols = (X - x0) / dx - 0.5
    rows = (y0 - Y) / dy - 0.5
    out = sample_cubic(np.asarray(img, np.float64), rows, cols)
    h, w = img.shape
    out[(rows < -0.25) | (rows > h - 0.75) | (cols < -0.25) | (cols > w - 0.75)] = np.nan
    return out


def block_mean(a, k):
    """mean of k x k blocks ignoring NaN (a block of only NaN stays NaN); the edge blocks may be partial"""
    if k <= 1:
        return a
    h, w = a.shape
    H, W = -(-h // k) * k, -(-w // k) * k
    p = np.full((H, W), np.nan)
    p[:h, :w] = a
    p = p.reshape(H // k, k, W // k, k)
    ok = np.isfinite(p)
    n = ok.sum(axis=(1, 3))
    s = np.where(ok, p, 0.0).sum(axis=(1, 3))
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(n > 0, s / np.maximum(n, 1), np.nan)


class Mosaic:
    """a north-up raster in a projection, filled piece by piece (tiles of a source); NaN where nothing came. Its pixels
    sit on the source's own pixel grid (`align`: a corner of any source pixel), k source pixels wide, so tiles of the
    source drop in exactly, averaged k x k."""

    def __init__(self, proj, box, px, align=(0.0, 0.0)):
        x_min, y_min, x_max, y_max = box
        ax, ay = align
        self.proj, self.dx = proj, float(px)
        self.x0 = ax + math.floor((x_min - ax) / px) * px
        self.y0 = ay + math.ceil((y_max - ay) / px) * px
        self.w = int(math.ceil((x_max - self.x0) / px)) + 1
        self.h = int(math.ceil((self.y0 - y_min) / px)) + 1
        if self.w * self.h > 64e6:
            raise DemError('the elevation raster would be too big (%d x %d)' % (self.w, self.h))
        self.a = np.full((self.h, self.w), np.nan)

    @staticmethod
    def factor(native, target, tile_px=None):
        """how many source pixels to a mosaic pixel: the largest power of two that fits in `target` (a COG's
        overviews halve its pixels) and divides the source's tile width when it has one (every tile's edge then
        falls on a mosaic pixel's)"""
        k = 1
        while k * 2 * native <= target * (1 + 1e-9) and (not tile_px or tile_px % (k * 2) == 0):
            k *= 2
        return k

    def paste(self, tile, x0, y0, dx, dy):
        """a north-up tile (NaN = no data): k x k block means into this mosaic's pixels, exactly when the tile sits on
        the mosaic's grid, else each mosaic pixel takes the nearest tile pixel"""
        k = max(1, int(round(self.dx / dx)))
        if k > 1:
            tile = block_mean(tile, k)
            dx, dy = dx * k, dy * k
        ox, oy = (x0 - self.x0) / self.dx, (self.y0 - y0) / self.dx
        if abs(dx - self.dx) < 1e-9 * self.dx and abs(ox - round(ox)) < 1e-6 and abs(oy - round(oy)) < 1e-6:
            ox, oy = int(round(ox)), int(round(oy))
            th, tw = tile.shape
            r0, c0, r1, c1 = max(0, oy), max(0, ox), min(self.h, oy + th), min(self.w, ox + tw)
            if r1 > r0 and c1 > c0:
                sub = tile[r0 - oy:r1 - oy, c0 - ox:c1 - ox]
                blk = self.a[r0:r1, c0:c1]
                ok = np.isfinite(sub)
                blk[ok] = sub[ok]
            return
        th, tw = tile.shape
        # off the mosaic's grid: bilinear from the tile at each mosaic pixel centre between the tile's own pixel
        # centres; the thin seams between tiles are closed once all are in (seal)
        cx = self.x0 + (np.arange(self.w) + 0.5) * self.dx
        cy = self.y0 - (np.arange(self.h) + 0.5) * self.dx
        fc, fr = (cx - x0) / dx - 0.5, (y0 - cy) / dy - 0.5
        cols = np.nonzero((fc >= 0) & (fc <= tw - 1))[0]
        rows = np.nonzero((fr >= 0) & (fr <= th - 1))[0]
        if not len(cols) or not len(rows):
            return
        self.seams = True
        c, r = fc[cols], fr[rows]
        c0, r0 = np.minimum(np.floor(c).astype(np.int64), max(tw - 2, 0)), np.minimum(np.floor(r).astype(np.int64), max(th - 2, 0))
        c1, r1 = np.minimum(c0 + 1, tw - 1), np.minimum(r0 + 1, th - 1)
        tc, tr = (c - c0)[None, :], (r - r0)[:, None]
        sub = ((tile[np.ix_(r0, c0)] * (1 - tc) + tile[np.ix_(r0, c1)] * tc) * (1 - tr)
               + (tile[np.ix_(r1, c0)] * (1 - tc) + tile[np.ix_(r1, c1)] * tc) * tr)
        block = self.a[np.ix_(rows, cols)]
        ok = np.isfinite(sub)
        block[ok] = sub[ok]
        self.a[np.ix_(rows, cols)] = block

    seams = False

    def seal(self):
        """close the one-pixel seams that tiles off the mosaic's grid leave between them (linear across a seam)"""
        a = self.a
        for _ in range(2):
            hole = ~np.isfinite(a)
            if not hole.any():
                return
            lr = np.full(a.shape, np.nan)
            lr[:, 1:-1] = (a[:, :-2] + a[:, 2:]) / 2
            ud = np.full(a.shape, np.nan)
            ud[1:-1, :] = (a[:-2, :] + a[2:, :]) / 2
            fill = np.where(np.isfinite(lr), lr, ud)
            take = hole & np.isfinite(fill)
            a[take] = fill[take]

    def sample(self, win):
        if self.seams:
            self.seal()
        return sample_raster(self.a, self.x0, self.y0, self.dx, self.dx, self.proj, win)


# ---------------------------------------------------------------- merging several sources

def _depth(mask, n):
    """for each True cell, how many steps (up to n) it is from the nearest False cell; the window's own edge is not a
    boundary"""
    d = np.zeros(mask.shape, np.int16)
    m = mask.copy()
    for _ in range(n):
        d += m
        e = m.copy()
        e[1:, :] &= m[:-1, :]
        e[:-1, :] &= m[1:, :]
        e[:, 1:] &= m[:, :-1]
        e[:, :-1] &= m[:, 1:]
        if (e == m).all():
            d[m] = n
            break
        m = e
    return d


FEATHER = 24                         # grid steps over which a source blends into the one filling its holes


def merge(layers):
    """[(source id, dem with NaN where it has no data)], best first -> (dem, ids used, fraction each one filled).
    The holes of the better sources are filled from the next ones, raised or lowered to meet them along the edge
    (vertical datums and resolution differ) and blended into them over FEATHER grid steps."""
    out, used, share = None, [], []
    for sid, d in layers:
        if d is None:
            continue
        ok = np.isfinite(d)
        if out is None:
            if ok.any():
                out = d.astype(np.float64).copy()
                used.append(sid)
                share.append(float(ok.mean()))
            continue
        hole = ~np.isfinite(out)
        if not hole.any():
            break
        fill = hole & ok
        if not fill.any():
            continue
        have = ~hole
        near = have & ok & (_depth(have, 48) < 48)
        if near.sum() < 100:
            near = have & ok
        off = float(np.median((out - d)[near])) if near.sum() >= 100 else 0.0
        d = np.where(d > 0, d + off, d)               # land only: the sea of a coarse source stays at sea level
        w = _depth(have, FEATHER).astype(np.float64) / FEATHER
        blend = have & ok & (w < 1)
        out[blend] = w[blend] * out[blend] + (1 - w[blend]) * d[blend]
        out[fill] = d[fill]
        used.append(sid)
        share.append(float(fill.mean()))
        log.info('elevation %s fills %.1f%% of the window (raised %.1f m to meet the finer data)', sid,
                 100 * fill.mean(), off)
    return out, used, share


# ---------------------------------------------------------------- sources

class Source:
    """An elevation source. fetch(win, cancel, progress) -> (G, G) metres with NaN where it has no data."""
    id = ''
    name = ''                         # short, for the Terrain card's chip: "swissALTI3D (2 m)"
    credit = ''                       # the attribution its license asks for
    res = 30.0                        # metres per pixel of its data
    boxes = ()                        # where it has data: (west, south, east, north) boxes; () = everywhere
    order = 50                        # several cover a window: lower first (finer data, then wider coverage)
    smooth = 0.0                      # Gaussian smoothing in grid steps after sampling (coarse data's stair steps)
    legacy = False                    # kept only for maps made with it (never picked for a new window)
    max_m = None                      # windows wider than this (metres) leave it to the next source

    def usable(self, win):
        return not self.legacy and (self.max_m is None or win.size_m <= self.max_m) and self.covers(win.box)

    def covers(self, box):
        if not self.boxes:
            return True
        w, s, e, n = box
        return any(w < c and e > a and s < d and n > b for a, b, c, d in self.boxes)

    def fetch(self, win, cancel, progress):
        raise NotImplementedError

    def info(self):
        return dict(name=self.name, credit=self.credit)


SOURCES = {}


def register(src):
    assert src.id and '+' not in src.id and src.id not in SOURCES, src.id
    SOURCES[src.id] = src
    return src


def describe(src_id):
    """(name, credit) of a source id from load(), several joined"""
    from . import dem_sources                                     # noqa: F401  (registers the sources)
    parts = [SOURCES[s] for s in src_id.split('+') if s in SOURCES]
    if not parts:
        return src_id, ''
    name = parts[0].name if len(parts) == 1 else '%s + %s' % (parts[0].name, ' + '.join(p.name for p in parts[1:]))
    return name, '\n'.join(dict.fromkeys(p.credit for p in parts))      # one line each (a credit may hold '; ')


def candidates(win):
    """the sources that may cover the window, finest first (the worldwide ones last)"""
    from . import dem_sources                                     # noqa: F401  (registers the sources)
    return sorted((s for s in SOURCES.values() if s.usable(win)), key=lambda s: (s.order, s.res))


def georef_tags(tags):
    """(x of the left edge, y of the top edge, pixel width, pixel height) from GeoTIFF tags (ModelTiepoint and
    ModelPixelScale, or ModelTransformation; 'pixel is point' rasters moved by half a pixel), or None"""
    sc, tp, mt = tags.get('ModelPixelScaleTag'), tags.get('ModelTiepointTag'), tags.get('ModelTransformationTag')
    if sc and tp:
        i, j, x, y = tp.value[0], tp.value[1], tp.value[3], tp.value[4]      # pixel (i, j) corner at (x, y)
        x0, y0, dx, dy = x - i * sc.value[0], y + j * sc.value[1], sc.value[0], sc.value[1]
    elif mt and abs(mt.value[1]) < 1e-12 and abs(mt.value[4]) < 1e-12:
        x0, y0, dx, dy = mt.value[3], mt.value[7], mt.value[0], -mt.value[5]
    else:
        return None
    gk = tags.get('GeoKeyDirectoryTag')
    if gk:
        v = list(gk.value)
        for k in range(4, len(v) - 3, 4):
            if v[k] == 1025 and v[k + 1] == 0 and v[k + 3] == 2:      # GTRasterTypeGeoKey: RasterPixelIsPoint
                x0, y0 = x0 - dx / 2, y0 + dy / 2
    return x0, y0, dx, dy


def _georef(data):
    """(west, north, degrees per pixel x, degrees per pixel y) of a GeoTIFF, or None when the tags are missing"""
    import tifffile
    with tifffile.TiffFile(io.BytesIO(data)) as t:
        return georef_tags(t.pages[0].tags)


def read_tiff(data, what='the elevation server'):
    """a GeoTIFF's first band as float64 (tifffile for what it decodes, else cog.py's decoders: LZW, predictors)"""
    import tifffile
    if data[:2] not in (b'II', b'MM'):
        raise DemError('%s returned no elevation image (%r)' % (what, bytes(data[:80])))
    try:
        img = tifffile.imread(io.BytesIO(data))
    except (KeyError, ValueError, NotImplementedError, RuntimeError):
        from . import cog
        c = cog.open_bytes(data)
        p = c.levels[0]
        img = c.read(p, c.x0, c.y0 - c.dy * c.height, c.x0 + c.dx * c.width, c.y0)[0]
    img = np.asarray(img, np.float64)
    if img.ndim == 3:
        img = img[..., 0] if img.shape[-1] < img.shape[0] else img[0]
    return img


class ImageServer(Source):
    """an ArcGIS ImageServer's exportImage in WGS84 (the server reprojects and resamples): 3DEP and others"""
    url = ''                          # .../ImageServer
    extra = ''                        # more query parameters ('&renderingRule=...')
    max_px = 4000
    nodata = -9999.0
    exact = True                      # False: '3dep' maps (see _Tdep)

    def request(self, w, s, e, n, nx, ny):
        u = (self.url + '/exportImage?bbox={w:.7f},{s:.7f},{e:.7f},{n:.7f}&bboxSR=4326&imageSR=4326&size={nx},{ny}'
             '&format=tiff&pixelType=F32&noData={nd:g}&interpolation=RSP_BilinearInterpolation&f=image').format(
                 w=w, s=s, e=e, n=n, nx=nx, ny=ny, nd=self.nodata)
        return u + ('&adjustAspectRatio=false' if self.exact else '') + self.extra

    def fetch(self, win, cancel, progress):
        w, s, e, n = win.box
        px = win.pixel(self.res)
        coslat = max(math.cos(math.radians(win.lat)), 0.05)
        ny = int(min(self.max_px, max(256, math.ceil((n - s) * 111320.0 / px))))
        nx = int(min(self.max_px, max(256, math.ceil((e - w) * 111320.0 * coslat / px))))
        progress(0.1, 'Downloading %s elevation...' % self.name)
        data = net.get(self.request(w, s, e, n, nx, ny), timeout=300, retries=3, cancel=cancel)
        img = read_tiff(data, self.name)
        img[(img <= self.nodata + 1) | (img < -1000) | ~np.isfinite(img)] = np.nan
        progress(0.7, 'Resampling elevation...')
        ref = _georef(data) if self.exact else None
        if ref:
            x0, y0, dx, dy = ref
        else:                                         # the old reading of '3dep' maps: as if it were the asked box
            ph, pw = img.shape
            x0, y0, dx, dy = w, n, (e - w) / pw, (n - s) / ph
        return sample_raster(img, x0, y0, dx, dy, crs.GEOGRAPHIC, win)


class _Tdep(ImageServer):
    url = 'https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer'
    res = 10.0
    boxes = US_BOXES
    order = 15                         # in the US before anything else, as before (Mapterhorn holds 3DEP too)
    name = 'USGS 3DEP (10 m)'
    credit = 'Elevation: USGS 3D Elevation Program (public domain)'

    def fetch(self, win, cancel, progress):
        """the request of 1.7 and later, unchanged, so maps rebuild to the same terrain: ~1.1 pixels per grid step
        (where 3DEP has 1 m lidar the service then serves it). '3dep' (until 1.7.0) read the server's image as if
        it covered exactly the requested box, but exportImage keeps its pixels square in degrees and widens the
        latitude span instead (by 1/cos(lat), ~38% at 44N): the terrain came out squeezed north-south towards the map
        centre. '3dep2' asks for the exact box and maps the image by its own georeferencing. Maps made with '3dep'
        keep it on Rebuild, so the terrain under their resorts stays where it was (2026-11-14, found from real lifts
        landing off their ridges at Killington). Where 3DEP has no data: NaN ('3dep2'; '3dep' raises as before)."""
        LA, LO = win.LA, win.LO
        pad_lat = 3 * win.spacing / 111320.0
        pad_lon = pad_lat / max(math.cos(math.radians(win.lat)), 0.05)
        s, n = LA.min() - pad_lat, LA.max() + pad_lat
        w, e = LO.min() - pad_lon, LO.max() + pad_lon
        ny = int(min(4000, max(256, math.ceil((n - s) * 111320.0 / win.spacing * 1.1))))
        nx = int(min(4000, max(256, math.ceil((e - w) * 111320.0 * math.cos(math.radians(win.lat)) / win.spacing * 1.1))))
        progress(0.1, 'Downloading USGS elevation data...')
        url = TDEP_URL.format(w=w, s=s, e=e, n=n, nx=nx, ny=ny) + ('&adjustAspectRatio=false' if self.exact else '')
        data = net.get(url, timeout=300, retries=3, cancel=cancel)
        img = read_tiff(data, 'USGS')
        bad = (img < -1000) | ~np.isfinite(img)
        if bad.any():
            if not self.exact:
                raise DemError('USGS has no elevation for part of this area')
            if bad.all():
                return None
            img[bad] = np.nan
        progress(0.7, 'Resampling elevation...')
        ref = _georef(data) if self.exact else None
        if ref:                                          # where the image really is
            x0, y0, dx, dy = ref
            return sample_raster(img, x0, y0, dx, dy, crs.GEOGRAPHIC, win)
        ph, pw = img.shape
        return sample_cubic(img, (n - LA) / (n - s) * ph - 0.5, (LO - w) / (e - w) * pw - 0.5)


def _tdep(sid, exact, legacy):
    s = _Tdep()
    s.id, s.exact, s.legacy = sid, exact, legacy
    return s


def _pieces(n, most):
    """split n pixels into runs of at most `most`: [(start, length)]"""
    k = max(1, -(-n // most))
    step = -(-n // k)
    return [(i, min(step, n - i)) for i in range(0, n, step)]


class Gridded(Source):
    """A server that cuts and resamples its raster to the box and size asked (WCS GetCoverage, WMS GetMap): the window
    in `proj`'s coordinates at about the pixel the grid needs, in pieces of at most max_px, put together and sampled.
    Subclasses give request() and, unless it is a GeoTIFF, decode()."""
    proj = crs.GEOGRAPHIC
    max_px = 2048
    nodata = None                     # its no-data value (besides NaN and anything below -1000 m)
    per_host = 4
    timeout = 180                     # seconds for one piece (slow servers: more)

    def request(self, x_min, y_min, x_max, y_max, nx, ny):
        raise NotImplementedError

    def decode(self, data, nx, ny):
        """-> (2D array, (x0, y0, dx, dy) from the file, or None to take the box asked)"""
        img = read_tiff(data, self.name)
        ref = None
        try:
            ref = _georef(data)
        except Exception:                                 # noqa: BLE001  (no or odd tags: the box asked)
            pass
        return img, ref

    def pixel_units(self, win):
        """the pixel to ask for, in the request's coordinates (degrees for latitude/longitude)"""
        px = win.pixel(self.res)
        coslat = max(math.cos(math.radians(win.lat)), 0.05)
        if isinstance(self.proj, crs.Geographic):
            return px / (111320.0 * coslat), px / 111320.0
        if isinstance(self.proj, crs.WebMercator):          # its metres shrink towards the poles
            return px / coslat, px / coslat
        return px, px

    def fetch(self, win, cancel, progress):
        x_min, y_min, x_max, y_max = win.native_box(self.proj)
        dx, dy = self.pixel_units(win)
        x0, y0 = math.floor(x_min / dx) * dx, math.ceil(y_max / dy) * dy      # whole pixels: pieces line up
        nx, ny = int(math.ceil((x_max - x0) / dx)) + 1, int(math.ceil((y0 - y_min) / dy)) + 1
        out = np.full((ny, nx), np.nan)
        most = min(self.max_px, 2048)                 # also bounds the memory a resampled piece takes
        jobs = [(c, r, w, h) for r, h in _pieces(ny, most) for c, w in _pieces(nx, most)]
        done = [0]

        def one(job):
            c, r, w, h = job
            bx0, by1 = x0 + c * dx, y0 - r * dy
            bx1, by0 = bx0 + w * dx, by1 - h * dy
            cancel.check()
            data = net.get(self.request(bx0, by0, bx1, by1, w, h), timeout=self.timeout, retries=3, cancel=cancel,
                           per_host=self.per_host)
            img, ref = self.decode(data, w, h)
            img = np.asarray(img, np.float64)
            bad = ~np.isfinite(img) | (img < -1000) | (img > 9000)
            if self.nodata is not None:
                bad |= img == self.nodata
            img[bad] = np.nan
            if ref is not None:                       # where the pixels asked for fall in what the server sent
                rx0, ry0, rdx, rdy = ref
                cc = ((bx0 + (np.arange(w) + 0.5) * dx) - rx0) / rdx - 0.5
                rr = (ry0 - (by1 - (np.arange(h) + 0.5) * dy)) / rdy - 0.5
                if not (img.shape == (h, w) and np.abs(cc - np.arange(w)).max() < 1e-3
                        and np.abs(rr - np.arange(h)).max() < 1e-3):     # not exactly as asked: resample
                    R, C = np.meshgrid(rr, cc, indexing='ij')
                    ih, iw = img.shape
                    img2 = sample_cubic(img, R, C)
                    img2[(R < -0.25) | (R > ih - 0.75) | (C < -0.25) | (C > iw - 0.75)] = np.nan
                    img = img2
            elif img.shape != (h, w):
                raise DemError('%s sent a %s image for %s' % (self.name, img.shape, (h, w)))
            out[r:r + h, c:c + w] = img
            done[0] += 1
            progress(0.05 + 0.65 * done[0] / len(jobs), 'Downloading %s (%d of %d)...' % (self.name, done[0], len(jobs)))

        progress(0.05, 'Downloading %s...' % self.name)
        if len(jobs) > 1:
            with ThreadPoolExecutor(min(self.per_host, len(jobs))) as ex:
                list(ex.map(one, jobs))
        else:
            one(jobs[0])
        if not np.isfinite(out).any():
            return None
        progress(0.75, 'Resampling elevation...')
        return sample_raster(out, x0, y0, dx, dy, self.proj, win)


def _raw_cache(sid, key):
    return paths.DEMS / 'src' / sid / (hashlib.sha1(key.encode()).hexdigest()[:24] + '.bin')


def cached_get(sid, url, cancel, **kw):
    """a source file kept on disk (the raw cache, trimmed to RAW_CACHE_MB by age) or downloaded"""
    f = _raw_cache(sid, url)
    if f.is_file():
        try:
            data = f.read_bytes()
            f.touch()
            return data
        except OSError:
            pass
    data = net.get(url, cancel=cancel, **kw)
    if data is not None:
        paths.atomic_write(f, data)
    return data


def trim_raw_cache():
    files = [p for p in (paths.DEMS / 'src').rglob('*.bin')] if (paths.DEMS / 'src').is_dir() else []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    for p in files:
        total += p.stat().st_size
        if total > RAW_CACHE_MB << 20:
            try:
                p.unlink()
            except OSError:
                pass


class Tiled(Source):
    """A source made of GeoTIFF tiles on one pixel grid (national tilings, STAC catalogs, COG buckets): the tiles under
    the window, read whole (small ones, kept on disk) or through range requests (big COGs: the overview the grid needs
    and only its tiles under the window), put together in a mosaic on the source's grid and sampled.
    Subclasses give tiles(box): [(url, size in bytes or None)] for a box in `proj`'s coordinates."""
    proj = None
    tile_px = None                    # pixels across a tile (a divisor keeps the mosaic on the tile edges)
    align = (0.0, 0.0)                # a pixel corner of the source's grid
    whole_below = 6 << 20             # files smaller than this are downloaded whole (and kept)
    threads = 6

    def tiles(self, box, cancel):
        raise NotImplementedError

    def fetch(self, win, cancel, progress):
        from . import cog
        box = win.native_box(self.proj)
        progress(0.03, 'Finding %s tiles...' % self.name)
        tiles = self.tiles(box, cancel)
        if not tiles:
            return None
        k = Mosaic.factor(self.res, win.pixel(self.res, 1.5), self.tile_px)
        mos = Mosaic(self.proj, box, self.res * k, self.align)
        done = [0]

        def one(t):
            url, size = t
            cancel.check()
            if size is not None and size < self.whole_below:
                data = cached_get(self.id, url, cancel, timeout=120, retries=3, not_found_ok=True)
                if data is None:
                    return
                c = cog.open_bytes(data)
            else:
                c = cog.open_url(url, cancel, size)
            page = c.level_for(self.res * k)
            part = c.read(page, *box, threads=3)
            if part is not None:
                a, x0, y0, dx, dy = part
                mos.paste(a, x0, y0, dx, dy)
            done[0] += 1
            progress(0.05 + 0.7 * done[0] / len(tiles), 'Downloading %s (%d of %d tiles)...' % (self.name, done[0],
                                                                                                 len(tiles)))

        with ThreadPoolExecutor(min(self.threads, len(tiles))) as ex:
            list(ex.map(one, tiles))
        if not np.isfinite(mos.a).any():
            return None
        progress(0.8, 'Resampling elevation...')
        return mos.sample(win)


def _lonlat_to_px(lon, lat, z):
    n = 256 * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    s = np.sin(np.radians(np.clip(lat, -85.05, 85.05)))
    y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * np.pi)) * n
    return x, y


class XyzTiles(Source):
    """256 px elevation tiles on the web Mercator grid of one zoom level, each decoded from its PNG (or other) bytes"""
    url = ''                          # with {z} {x} {y}
    zoom = 13
    folder = ''                       # cache folder under DEMS (tiles kept: small)
    tile = 256
    per_host = 6
    missing_ok = False                # a tile the server doesn't have (404) is no data, not an error

    def decode(self, data):
        raise NotImplementedError

    def get_tile(self, z, x, y, cancel):
        x %= 2 ** z
        f = paths.DEMS / self.folder / str(z) / str(x) / ('%d.png' % y)
        data = f.read_bytes() if f.is_file() else None
        if not data:
            data = net.get(self.url.format(z=z, x=x, y=y), timeout=60, retries=4, cancel=cancel,
                           per_host=self.per_host, not_found_ok=self.missing_ok)
            if data is None:
                return None
            paths.atomic_write(f, data)
        return self.decode(data)

    def fetch(self, win, cancel, progress):
        z, T = self.zoom, self.tile
        px, py = _lonlat_to_px(win.LO, win.LA, z)
        px, py = px * T / 256, py * T / 256
        x0, x1 = int(px.min()) // T - 1, int(px.max()) // T + 1
        y0, y1 = int(py.min()) // T - 1, int(py.max()) // T + 1
        tiles = [(tx, ty) for ty in range(y0, y1 + 1) for tx in range(x0, x1 + 1)]
        mosaic = np.full(((y1 - y0 + 1) * T, (x1 - x0 + 1) * T), np.nan, np.float32)   # exact for 1/256 m values
        done = [0]

        def one(t):
            tx, ty = t
            a = self.get_tile(z, tx, ty, cancel)
            if a is not None:
                mosaic[(ty - y0) * T:(ty - y0 + 1) * T, (tx - x0) * T:(tx - x0 + 1) * T] = a
            done[0] += 1
            progress(0.05 + 0.6 * done[0] / len(tiles), 'Downloading %s (%d of %d)...' % (self.name, done[0], len(tiles)))

        with ThreadPoolExecutor(6) as ex:
            list(ex.map(one, tiles))
        progress(0.7, 'Resampling elevation...')
        rows, cols = py - y0 * T - 0.5, px - x0 * T - 0.5
        d = sample_cubic(mosaic, rows, cols)
        if self.smooth:
            ok = np.isfinite(d)
            d = gaussian(np.where(ok, d, np.nanmean(d) if ok.any() else 0.0), self.smooth)
            d[~ok] = np.nan
        return d


class Terrarium(XyzTiles):
    id = 'terrarium'
    name = 'Terrain Tiles (~30 m)'
    credit = 'Elevation: Terrain Tiles on AWS (Mapzen; SRTM, GMTED, ETOPO1, 3DEP and others)'
    url = TERRARIUM_URL
    folder = 'terrarium'
    res = 30.0
    order = 100

    def decode(self, data):
        a = np.asarray(Image.open(io.BytesIO(data)).convert('RGB')).astype(np.float64)
        return a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768

    def fetch(self, win, cancel, progress):
        if abs(win.lat) > 84:
            raise DemError('Elevation tiles do not cover the poles')
        # coarse source (~30 m outside the US): smooth the stair steps of the 16-bit tiles; stronger on small windows
        self.smooth = float(np.clip(3.0 * math.sqrt(8000.0 / win.size_m), 2.0, 5.0))
        return super().fetch(win, cancel, progress)


register(_tdep('3dep2', True, False))
register(_tdep('3dep', False, True))
register(Terrarium())


# ---------------------------------------------------------------- entry point

def cache_key(lat, lon, size_m, source):
    s = '%d|%.6f|%.6f|%.1f|%s' % (CACHE_VERSION, lat, lon, size_m, source)
    return hashlib.sha1(s.encode()).hexdigest()[:20]


def _cached(lat, lon, size_m, src):
    f = paths.DEMS / ('%s.npy' % cache_key(lat, lon, size_m, src))
    if f.is_file():
        try:
            d = np.load(f)
            if d.shape == (G, G):
                f.touch()
                return d
        except (OSError, ValueError):
            pass
    return None


def _store(lat, lon, size_m, src, d):
    buf = io.BytesIO()
    np.save(buf, d)
    paths.atomic_write(paths.DEMS / ('%s.npy' % cache_key(lat, lon, size_m, src)), buf.getvalue())
    _trim_cache()


def _finish(d):
    return np.maximum(d, 0.0).astype(np.float32)          # ocean floor (terrarium bathymetry) -> sea level


def load(lat, lon, size_m, cancel=net.NEVER, progress=lambda f, t: None, prefer=None, notes=None):
    """-> (dem metres float32 (G, G), source id). Cached per window. prefer: an id an earlier load returned (a map's),
    to make the same terrain again from the same sources. notes: a list that gets a line for every finer source that
    could not be used."""
    from . import dem_sources                                     # noqa: F401  (registers the sources)
    notes = notes if notes is not None else []
    if prefer:
        d = _cached(lat, lon, size_m, prefer)
        if d is not None:
            progress(1.0, 'Elevation loaded from cache')
            return d, prefer
        ids = prefer.split('+')
        unknown = [i for i in ids if i not in SOURCES]
        if unknown:
            raise DemError('This map was made from an elevation source this version does not know (%s).'
                           % ', '.join(unknown))
        win = Window(lat, lon, size_m)
        t = time.time()
        layers = []
        for k, sid in enumerate(ids):
            sub = _sub(progress, k, len(ids))
            layers.append((sid, SOURCES[sid].fetch(win, cancel, sub)))
        d, used, _ = merge(layers)
        if d is None or not np.isfinite(d).all():
            raise DemError('%s has no elevation for part of this area' % describe(prefer)[0])
        d = _finish(d)
        _store(lat, lon, size_m, prefer, d)
        log.info('elevation %s for %.5f,%.5f %.0f m in %.1f s: %.0f..%.0f m', prefer, lat, lon, size_m,
                 time.time() - t, d.min(), d.max())
        progress(1.0, 'Elevation ready')
        return d, prefer
    auto = paths.DEMS / ('%s.json' % cache_key(lat, lon, size_m, 'auto%d' % dem_sources.VERSION))
    try:
        src = json.loads(auto.read_text(encoding='utf-8'))['src']
        d = _cached(lat, lon, size_m, src)
        if d is not None:
            auto.touch()
            progress(1.0, 'Elevation loaded from cache')
            return d, src
    except (OSError, ValueError, KeyError):
        pass
    win = Window(lat, lon, size_m)
    t = time.time()
    cands = candidates(win)
    layers, errors, failed = [], [], []
    filled = np.zeros((G, G), bool)
    for k, s in enumerate(cands):
        sub = _sub(progress, k, len(cands))
        try:
            d = s.fetch(win, cancel, sub)
        except net.Cancelled:
            raise
        except (DemError, net.NetError) as e:
            log.warning('%s failed: %s', s.id, e)
            errors.append('%s: %s' % (s.name, e))
            failed.append((s, getattr(e, 'reason', None) or str(e)))
            continue
        except Exception as e:                                    # noqa: BLE001  (a source's format surprise)
            log.exception('%s failed', s.id)
            errors.append('%s: %s' % (s.name, e))
            failed.append((s, str(e)))
            continue
        if d is None or not np.isfinite(d).any():
            continue
        layers.append((s.id, d))
        filled |= np.isfinite(d)
        if filled.all():
            break
    d, used, share = merge(layers)
    if d is None or not np.isfinite(d).all():
        raise DemError(errors[-1] if errors else 'No elevation source covers this area')
    src = '+'.join(used)
    coarsest = max(SOURCES[i].res for i in used)
    missed = [(s, why) for s, why in failed if s.res < coarsest]   # a finer source that may have covered part of it
    for s, why in missed:
        notes.append('%s did not answer (%s): part of this area may have finer elevation than this. Load the terrain '
                     'again later to try it again.' % (s.name, why))
    d = _finish(d)
    _store(lat, lon, size_m, src, d)
    if not missed:                                    # else the next load asks the finer source again
        paths.atomic_write(auto, json.dumps(dict(src=src)).encode('utf-8'))
    log.info('elevation %s (%s) for %.5f,%.5f %.0f m in %.1f s: %.0f..%.0f m', src,
             ', '.join('%.0f%%' % (100 * f) for f in share), lat, lon, size_m, time.time() - t, d.min(), d.max())
    progress(1.0, 'Elevation ready')
    return d, src


def _sub(progress, k, n):
    a, b = k / max(n, 1), (k + 1) / max(n, 1)
    return lambda f, t: progress(a + (b - a) * f, t)


def _trim_cache(keep=24):
    files = sorted(paths.DEMS.glob('*.npy'), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[keep:]:
        try:
            f.unlink()
        except OSError:
            pass
    for f in paths.DEMS.glob('*.json'):
        if time.time() - f.stat().st_mtime > 30 * 86400:
            try:
                f.unlink()
            except OSError:
                pass
    trim_raw_cache()                                  # the sources' tiles (Mapterhorn, GSI, ...): RAW_CACHE_MB
