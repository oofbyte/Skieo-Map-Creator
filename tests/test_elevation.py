"""Offline checks of the elevation machinery: the map projections (crs.py) against published coordinates, sampling a
raster in a projection onto the grid, merging sources, and the COG reader's tiles and decoders (cog.py).
usage: python tests/test_elevation.py"""
import io
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skieo_creator import cog, crs, dem, geo  # noqa: E402


def near(got, want, tol, what):
    got = [float(v) for v in got]
    assert all(abs(g - w) <= tol for g, w in zip(got, want)), '%s: %s, not %s' % (what, got, want)


# ---- projections: published examples and the origins of the definitions
near(crs.utm(32, ell=crs.WGS84).forward(45.0, 9.0), (500000.0, 4982950.40), 0.02, 'UTM 32N on its meridian at 45N')
near(crs.epsg(2154).forward(46.5, 3.0), (700000.0, 6600000.0), 1e-6, 'Lambert-93 origin')
near(crs.epsg(3416).forward(47.5, 13.333333333333334), (400000.0, 400000.0), 1e-6, 'Austria Lambert origin')
near(crs.epsg(3979).forward(49.0, -95.0), (0.0, 0.0), 1e-6, 'Canada Atlas Lambert origin')
near(crs.epsg(6677).forward(36.0, 139 + 5 / 6), (0.0, 0.0), 1e-6, 'Japan plane rectangular IX origin')
near(crs.epsg(3413).forward(90.0, 0.0), (0.0, 0.0), 1e-6, 'polar stereographic north pole')
near(crs.epsg(3035).forward(50.0, 5.0), (3962799.45, 2999718.85), 0.01, 'ETRS89-LAEA (EPSG Guidance Note 7-2 example)')
near(crs.epsg(3035).forward(52.0, 10.0), (4321000.0, 3210000.0), 1e-6, 'ETRS89-LAEA origin')
# swisstopo's own example for its approximate formulas: 46 02 38.87 N, 8 43 49.79 E -> 2 699 999.76 / 1 099 999.97
near(crs.epsg(2056).forward(46 + 2 / 60 + 38.87 / 3600, 8 + 43 / 60 + 49.79 / 3600), (2699999.76, 1099999.97), 0.05,
     'LV95')
# Ordnance Survey's worked example (OSGB36 latitude/longitude -> National Grid), the projection without the datum shift
bng = crs.TransverseMercator(crs.AIRY, -2.0, 0.9996012717, 400000.0, -100000.0, lat0=49.0)
near(bng.forward(52 + 39 / 60 + 27.2531 / 3600, 1 + 43 / 60 + 4.5177 / 3600), (651409.903, 313177.270), 0.01, 'BNG')
# ... and WGS84 to the National Grid through the Helmert shift: Ben Nevis' trig point (OS: NN 16674 71283)
near(crs.epsg(27700).forward(56.796851, -5.003508), (216674.0, 771283.0), 6.0, 'Ben Nevis')
# the transverse Mercator agrees with the app's own local grid (geo.py) where they are the same projection
g = geo.LocalGrid(46.0, 7.75)
tm = crs.TransverseMercator(crs.WGS84, 7.75, 1.0, 0.0, 0.0, lat0=46.0)
lat, lon = np.array([45.95, 46.0, 46.07]), np.array([7.70, 7.81, 7.75])
assert np.allclose(np.array(g.to_xy(lat, lon)), np.array(tm.forward(lat, lon)), atol=1e-6)

# ---- a raster in a projection lands where it belongs: a tilted plane in UTM metres, sampled onto the grid
win = dem.Window(46.0, 7.75, 3000.0)
p = crs.utm(32)
x_min, y_min, x_max, y_max = win.native_box(p)
px = 2.0
x0, y0 = np.floor(x_min) - 10, np.ceil(y_max) + 10
w, h = int((x_max - x0) / px) + 20, int((y0 - y_min) / px) + 20
cx = x0 + (np.arange(w) + 0.5) * px
cy = y0 - (np.arange(h) + 0.5) * px
plane = lambda X, Y: 1000.0 + 0.3 * (X - x0) - 0.2 * (y0 - Y)                     # noqa: E731
img = plane(cx[None, :], cy[:, None])
d = dem.sample_raster(img, x0, y0, px, px, p, win)
X, Y = win.xy(p)
assert np.isfinite(d).all() and np.abs(d - plane(X, Y)).max() < 1e-6, np.abs(d - plane(X, Y)).max()
# outside the raster: no data
d2 = dem.sample_raster(img[:h // 2], x0, y0, px, px, p, win)
assert np.isnan(d2[-1, :]).all() and np.isfinite(d2[0, :]).all()
# the mosaic (tiles pasted at a coarser pixel on the source's grid) gives the same plane
assert dem.Mosaic.factor(2.0, 4.5, 300) == 2 and dem.Mosaic.factor(2.0, 9.0, 300) == 4 and dem.Mosaic.factor(1, 7, 500) == 4
assert dem.Mosaic.factor(2.0, 9.0, 250) == 2 and dem.Mosaic.factor(0.5, 13.0) == 16 and dem.Mosaic.factor(2.0, 3.0) == 1
m = dem.Mosaic(p, win.native_box(p), 4.0, align=(x0, y0))
for i in range(0, h, 300):
    for j in range(0, w, 300):
        t = img[i:i + 300, j:j + 300]
        m.paste(t, x0 + j * px, y0 - i * px, px, px)
dm = m.sample(win)
assert np.isfinite(dm).all() and np.abs(dm - plane(X, Y)).max() < 0.02, np.abs(dm - plane(X, Y)).max()
# ... and off the source's grid (an overview whose pixels don't line up): bilinear, still in place
m = dem.Mosaic(p, win.native_box(p), 4.0)
for i in range(0, h, 300):
    for j in range(0, w, 300):
        m.paste(img[i:i + 300, j:j + 300], x0 + j * px, y0 - i * px, px, px)
dm = m.sample(win)
assert np.isfinite(dm).all() and np.abs(dm - plane(X, Y)).max() < 0.05, np.abs(dm - plane(X, Y)).max()

# ---- merging: the finer source keeps its values, the coarser fills its hole at its level and blends in
G = dem.G
yy, xx = np.mgrid[0:G, 0:G]
truth = 500.0 + 0.5 * xx + 0.25 * yy
fine = truth.copy()
fine[:, 1500:] = np.nan                                  # a border: the fine source stops here
coarse = truth + 7.0                                     # another datum: 7 m higher everywhere
out, used, share = dem.merge([('fine', fine), ('coarse', coarse)])
assert used == ['fine', 'coarse'] and np.isfinite(out).all()
assert np.abs(out - truth).max() < 1e-6, 'the coarse source is brought to the fine one\'s level'
assert abs(share[1] - (G - 1500) / G) < 1e-9
assert np.array_equal(out[:, :1500 - dem.FEATHER], fine[:, :1500 - dem.FEATHER]), 'away from the edge: untouched'
# a source that covers nothing new is not used; a window the first one covers whole takes only it
out, used, _ = dem.merge([('a', truth), ('b', coarse)])
assert used == ['a'] and np.array_equal(out, truth)
out, used, _ = dem.merge([('a', fine), ('b', np.full((G, G), np.nan)), ('c', coarse)])
assert used == ['a', 'c']
assert dem.describe('3dep2') == (dem.SOURCES['3dep2'].name, dem.SOURCES['3dep2'].credit)
assert dem.describe('3dep2+terrarium')[0] == 'USGS 3DEP (10 m) + Terrain Tiles (~30 m)'
assert dem.describe('3dep2+terrarium')[1].count('\n') == 1, 'one credit a line (a credit may hold "; ")'
# a coarse source filling a coast: its sea stays at sea level, however its land is raised to meet the finer data
sea = np.where(xx > 1700, -5.0, truth + 7.0)
land = np.where(xx > 1500, np.nan, truth)
out, used, _ = dem.merge([('fine', land), ('coarse', sea)])
assert np.all(out[:, 1701:] == -5.0) and np.abs(out[:, 1500:1700] - truth[:, 1500:1700]).max() < 1e-6

# the projected grids a window keeps: a later projection that takes a freed one's id doesn't get its coordinates
win2 = dem.Window(46.0, 7.75, 3000.0)
stale = object()
win2._xy[id(crs.GEOGRAPHIC)] = (stale, (np.zeros(1), np.zeros(1)))
X, Y = win2.xy(crs.GEOGRAPHIC)
assert X.shape == (G, G) and np.allclose(X, win2.LO) and crs.epsg(25832) is crs.epsg(25832)

# Cancel goes straight out of a load (it is no source's failure)
from skieo_creator import dem_sources, net, paths  # noqa: E402


class Cancelling(dem.Source):
    id, name = 'test_cancel', 'Cancelling'

    def fetch(self, win, cancel, progress):
        raise net.Cancelled()


real_candidates, real_dems = dem.candidates, paths.DEMS
with tempfile.TemporaryDirectory() as t:
    dem.candidates, paths.DEMS = (lambda win: [Cancelling()]), Path(t)
    try:
        dem.load(46.0, 7.75, 3000.0)
        raise AssertionError('Cancel was taken for a failed source')
    except net.Cancelled:
        pass
    finally:
        dem.candidates, paths.DEMS = real_candidates, real_dems

# HRDEM's WCS 1.1 in EPSG::4326: latitude first, the offsets (-latitude step, longitude step)
u = dem.SOURCES['ca_hrdem'].request(-74.6, 46.2, -74.5, 46.3, 100, 200)
assert 'BoundingBox=46.20000000,-74.60000000,46.30000000,-74.50000000' in u and 'GridOffsets=-0.0005000000,0.0010000000' in u
# TINITALY's 0.0 outside Italy is no data
z = io.BytesIO()
tifffile.imwrite(z, np.array([[0.0, 812.5]], np.float32))
img, _ = dem.SOURCES['it_tinitaly'].decode(z.getvalue(), 2, 1)
assert np.isnan(img[0, 0]) and img[0, 1] == 812.5


# ---- COG tiles: the decoders, then a tiled GeoTIFF with an overview read through range requests
def floatpred_encode(a):
    """TIFF floating point predictor (3) of a float32 tile, the inverse of cog.decode_tile's"""
    rows, cols = a.shape
    b = a.astype('>f4').view(np.uint8).reshape(rows, cols, 4).transpose(0, 2, 1).reshape(rows, cols * 4)
    out = b.copy()
    out[:, 1:] = b[:, 1:] - b[:, :-1]
    return out.tobytes()


def lzw_encode(data):
    """TIFF LZW as libtiff's encoder writes it (tif_lzw.c: wider codes once the next free entry passes the width's
    largest code, a clear code when the table is full), for checking the decoder"""
    table = {bytes((i,)): i for i in range(256)}
    nxt, nbits, out, bits, nb = 258, 9, bytearray(), 0, 0

    def put(code):
        nonlocal bits, nb
        bits = (bits << nbits) | code
        nb += nbits
        while nb >= 8:
            nb -= 8
            out.append((bits >> nb) & 0xFF)
        bits &= (1 << nb) - 1
    put(256)
    w = b''
    for c in data:
        wc = w + bytes((c,))
        if wc in table:
            w = wc
            continue
        put(table[w])
        table[wc] = nxt
        nxt += 1
        if nxt == 4094:
            put(256)
            table = {bytes((i,)): i for i in range(256)}
            nxt, nbits = 258, 9
        elif nxt > (1 << nbits) - 1:
            nbits += 1
        w = bytes((c,))
    if w:
        put(table[w])
    put(257)
    if nb:
        out.append((bits << (8 - nb)) & 0xFF)
    return bytes(out)


class FakePage:
    def __init__(self, compression, predictor, dtype):
        self.compression, self.predictor, self.dtype = compression, predictor, np.dtype(dtype)


rng = np.random.default_rng(7)
tile = (1500 + np.cumsum(rng.normal(0, 0.4, (32, 48)), axis=1)).astype(np.float32)
got = cog.decode_tile(zlib.compress(floatpred_encode(tile)), FakePage(8, 3, 'float32'), (32, 48))
assert np.array_equal(got, tile), 'deflate + floating point predictor'
ints = rng.integers(0, 3000, (16, 40)).astype('<i2')
diff = ints.copy()
diff[:, 1:] = ints[:, 1:] - ints[:, :-1]
got = cog.decode_tile(cog.lzw_decode(lzw_encode(diff.tobytes())) and lzw_encode(diff.tobytes()), FakePage(5, 2, 'int16'),
                      (16, 40))
assert np.array_equal(got, ints), 'LZW + horizontal predictor'
big = bytes(rng.integers(0, 4, 200000).astype(np.uint8))
assert cog.lzw_decode(lzw_encode(big)) == big, 'LZW past the 12-bit table (clear codes)'

H, W = 700, 900
full = (2000 + 50 * np.sin(np.arange(W)[None, :] / 40.0) + 30 * np.cos(np.arange(H)[:, None] / 25.0)).astype(np.float32)
full[:5, :5] = -9999
ov = full.reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3)).astype(np.float32)
buf = io.BytesIO()
geo_tags = [(33550, 'd', 3, (2.0, 2.0, 0.0), False), (33922, 'd', 6, (0, 0, 0, 2600000.0, 1100000.0, 0), False),
            (34735, 'H', 16, (1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, 2056), False),
            (42113, 's', 0, '-9999', False)]
with tifffile.TiffWriter(buf) as tw:
    tw.write(full, tile=(256, 256), compression='zlib', extratags=geo_tags)
    tw.write(ov, tile=(256, 256), compression='zlib', subfiletype=1)
data = buf.getvalue()
reads = []


def fetch(a, b):
    reads.append(b - a + 1)
    return data[a:b + 1]


c = cog.Cog(cog.RangeFile('mem://x', len(data), fetch=fetch), fetch=fetch)
assert c.epsg == 2056 and (c.x0, c.y0, c.dx, c.dy) == (2600000.0, 1100000.0, 2.0, 2.0) and c.nodata == -9999.0
assert len(c.levels) == 2 and c.level_for(3.0) is c.levels[0] and c.level_for(4.0) is c.levels[1]
a, x0, y0, dx, dy = c.read(c.levels[0], 2600100.0, 1099000.0, 2600700.0, 1099600.0)
r, k = int((1100000.0 - y0) / 2), int((x0 - 2600000.0) / 2)
assert np.array_equal(a, full[r:r + a.shape[0], k:k + a.shape[1]].astype(np.float64))
a, x0, y0, dx, dy = c.read(c.levels[1], 2600000.0, 1098600.0, 2601800.0, 1100000.0)
assert dx == 4.0 and (x0, y0) == (2600000.0, 1100000.0)
assert np.allclose(a[3:, 3:], ov[3:a.shape[0], 3:a.shape[1]]), 'the overview (its corner holds averaged nodata)'
a, *_ = c.read(c.levels[0], 2600000.0, 1099990.0, 2600008.0, 1100000.0)
assert np.isnan(a[0, 0]), 'nodata -> NaN'
# a user-defined projection (ProjectedCSType 32767) keeps its datum in GeographicType: no EPSG code for it
buf = io.BytesIO()
tifffile.imwrite(buf, np.zeros((4, 4), np.float32), extratags=[
    (33550, 'd', 3, (1.0, 1.0, 0.0), False), (33922, 'd', 6, (0, 0, 0, 1000.0, 2000.0, 0), False),
    (34735, 'H', 16, (1, 1, 0, 3, 1024, 0, 1, 1, 2048, 0, 1, 4269, 3072, 0, 1, 32767), False)])
assert cog.open_bytes(buf.getvalue()).epsg is None
print('elevation: all checks passed')
