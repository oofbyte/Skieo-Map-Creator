"""Elevation for a square terrain window, sampled onto the game's 2017 x 2017 landscape grid (row 0 = north).

Sources:
  * USGS 3DEP 1/3 arc-second (~10 m) inside the United States, via the public 3DEPElevation ImageServer
  * AWS Terrain Tiles (terrarium PNG, open data, global) everywhere else, and as the fallback
Both are resampled with cubic convolution onto a local transverse Mercator grid centred on the window (see geo.py).
"""
import hashlib
import io
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

from . import geo, net, paths

log = logging.getLogger(__name__)
G = 2017
CACHE_VERSION = 2
TDEP_URL = ('https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage?'
            'bbox={w:.7f},{s:.7f},{e:.7f},{n:.7f}&bboxSR=4326&imageSR=4326&size={nx},{ny}&format=tiff&pixelType=F32'
            '&noData=-9999&interpolation=RSP_BilinearInterpolation&f=image')
TERRARIUM_URL = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'
SOURCES = {
    '3dep': dict(name='USGS 3DEP (10 m)', credit='Elevation: USGS 3D Elevation Program (public domain)'),
    'terrarium': dict(name='Terrain Tiles (~30 m)', credit='Elevation: Terrain Tiles on AWS (Mapzen; SRTM, GMTED, ETOPO1, 3DEP and others)'),
}
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


# ---------------------------------------------------------------- sources

def _fetch_3dep(lat, lon, size_m, cancel, progress):
    import tifffile
    LA, LO = grid_latlon(lat, lon, size_m)
    pad_lat = 3 * size_m / (G - 1) / 111320.0
    pad_lon = pad_lat / max(math.cos(math.radians(lat)), 0.05)
    s, n = LA.min() - pad_lat, LA.max() + pad_lat
    w, e = LO.min() - pad_lon, LO.max() + pad_lon
    spacing = size_m / (G - 1)
    ny = int(min(4000, max(256, math.ceil((n - s) * 111320.0 / spacing * 1.1))))
    nx = int(min(4000, max(256, math.ceil((e - w) * 111320.0 * math.cos(math.radians(lat)) / spacing * 1.1))))
    progress(0.1, 'Downloading USGS elevation data...')
    data = net.get(TDEP_URL.format(w=w, s=s, e=e, n=n, nx=nx, ny=ny), timeout=300, retries=3, cancel=cancel)
    if data[:2] not in (b'II', b'MM'):
        raise DemError('USGS returned no elevation image (%r)' % data[:80])
    img = tifffile.imread(io.BytesIO(data)).astype(np.float64)
    if img.ndim != 2:
        img = img[..., 0]
    if (img < -1000).any() or not np.isfinite(img).all():
        raise DemError('USGS has no elevation for part of this area')
    progress(0.7, 'Resampling elevation...')
    ph, pw = img.shape
    rows = (n - LA) / (n - s) * ph - 0.5
    cols = (LO - w) / (e - w) * pw - 0.5
    return sample_cubic(img, rows, cols)


def _lonlat_to_px(lon, lat, z):
    n = 256 * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    s = np.sin(np.radians(np.clip(lat, -85.05, 85.05)))
    y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * np.pi)) * n
    return x, y


def _terrarium_tile(z, x, y, cancel):
    x %= 2 ** z
    f = paths.DEMS / 'terrarium' / str(z) / str(x) / ('%d.png' % y)
    data = None
    if f.is_file():
        data = f.read_bytes()
    if not data:
        data = net.get(TERRARIUM_URL.format(z=z, x=x, y=y), timeout=60, retries=4, cancel=cancel, per_host=6)
        paths.atomic_write(f, data)
    a = np.asarray(Image.open(io.BytesIO(data)).convert('RGB')).astype(np.float64)
    return a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768


def _fetch_terrarium(lat, lon, size_m, cancel, progress):
    LA, LO = grid_latlon(lat, lon, size_m)
    if abs(lat) > 84:
        raise DemError('Elevation tiles do not cover the poles')
    z = 13
    px, py = _lonlat_to_px(LO, LA, z)
    x0, x1 = int(px.min()) // 256 - 1, int(px.max()) // 256 + 1
    y0, y1 = int(py.min()) // 256 - 1, int(py.max()) // 256 + 1
    tiles = [(tx, ty) for ty in range(y0, y1 + 1) for tx in range(x0, x1 + 1)]
    mosaic = np.zeros(((y1 - y0 + 1) * 256, (x1 - x0 + 1) * 256), np.float64)
    done = [0]

    def one(t):
        tx, ty = t
        tile = _terrarium_tile(z, tx, ty, cancel)
        mosaic[(ty - y0) * 256:(ty - y0 + 1) * 256, (tx - x0) * 256:(tx - x0 + 1) * 256] = tile
        done[0] += 1
        progress(0.05 + 0.6 * done[0] / len(tiles), 'Downloading elevation tiles (%d of %d)...' % (done[0], len(tiles)))

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, tiles))
    progress(0.7, 'Resampling elevation...')
    dem = sample_cubic(mosaic, py - y0 * 256 - 0.5, px - x0 * 256 - 0.5)
    # coarse source (~30 m outside the US): smooth the stair steps of the 16-bit tiles; stronger on small windows
    sigma = float(np.clip(3.0 * math.sqrt(8000.0 / size_m), 2.0, 5.0))
    return gaussian(dem, sigma)


# ---------------------------------------------------------------- entry point

def cache_key(lat, lon, size_m, source):
    s = '%d|%.6f|%.6f|%.1f|%s' % (CACHE_VERSION, lat, lon, size_m, source)
    return hashlib.sha1(s.encode()).hexdigest()[:20]


def load(lat, lon, size_m, cancel=net.NEVER, progress=lambda f, t: None, prefer=None):
    """-> (dem metres float32 (G, G), source id). Cached per window."""
    order = [prefer] if prefer else (['3dep', 'terrarium'] if in_us(lat, lon) else ['terrarium'])
    if prefer is None and 'terrarium' not in order:
        order.append('terrarium')
    errors = []
    for src in order:
        f = paths.DEMS / ('%s.npy' % cache_key(lat, lon, size_m, src))
        if f.is_file():
            try:
                dem = np.load(f)
                if dem.shape == (G, G):
                    f.touch()
                    progress(1.0, 'Elevation loaded from cache')
                    return dem, src
            except (OSError, ValueError):
                pass
        t = time.time()
        try:
            dem = (_fetch_3dep if src == '3dep' else _fetch_terrarium)(lat, lon, size_m, cancel, progress)
        except (DemError, net.NetError) as e:
            log.warning('%s failed: %s', src, e)
            errors.append(str(e))
            continue
        dem = np.maximum(dem, 0.0).astype(np.float32)          # ocean floor (terrarium bathymetry) -> sea level
        buf = io.BytesIO()
        np.save(buf, dem)
        paths.atomic_write(f, buf.getvalue())
        _trim_cache()
        log.info('elevation %s for %.5f,%.5f %.0f m in %.1f s: %.0f..%.0f m', src, lat, lon, size_m, time.time() - t,
                 dem.min(), dem.max())
        progress(1.0, 'Elevation ready')
        return dem, src
    raise DemError(errors[-1] if errors else 'No elevation source covers this area')


def _trim_cache(keep=24):
    files = sorted(paths.DEMS.glob('*.npy'), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[keep:]:
        try:
            f.unlink()
        except OSError:
            pass
