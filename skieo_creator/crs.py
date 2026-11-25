"""The national map projections elevation data comes in, forward only (WGS84 latitude/longitude -> easting/northing in
metres), vectorised with numpy and without pyproj: a terrain window only needs to know where each of its grid points
falls in a source's raster.

ETRS89, NAD83(CSRS), GDA2020, NZGD2000 and JGD2011 are taken as WGS84 (they differ by about a metre, far less than a
grid step); older datums on other ellipsoids (OSGB36, MGI, CH1903+) go through a Helmert shift. Formulas: EPSG
Guidance Note 7-2 (Lambert conic, polar stereographic, Helmert), Karney 2011 (transverse Mercator, Krueger series to
n^6, as geo.py), swisstopo's approximate formulas for LV95 (better than a metre).
"""
import functools
import math

import numpy as np


class Ellipsoid:
    def __init__(self, a, inv_f):
        self.a, self.f = float(a), 1.0 / inv_f
        self.e2 = self.f * (2 - self.f)
        self.e = math.sqrt(self.e2)


WGS84 = Ellipsoid(6378137.0, 298.257223563)
GRS80 = Ellipsoid(6378137.0, 298.257222101)
BESSEL = Ellipsoid(6377397.155, 299.1528128)
AIRY = Ellipsoid(6377563.396, 299.3249646)


def _ecef(lat, lon, ell):
    phi, lam = np.radians(lat), np.radians(lon)
    s = np.sin(phi)
    nu = ell.a / np.sqrt(1 - ell.e2 * s * s)
    return nu * np.cos(phi) * np.cos(lam), nu * np.cos(phi) * np.sin(lam), nu * (1 - ell.e2) * s


def _geodetic(x, y, z, ell):
    p = np.hypot(x, y)
    phi = np.arctan2(z, p * (1 - ell.e2))
    for _ in range(4):                                   # converges to well under a millimetre
        s = np.sin(phi)
        nu = ell.a / np.sqrt(1 - ell.e2 * s * s)
        phi = np.arctan2(z + ell.e2 * nu * s, p)
    return np.degrees(phi), np.degrees(np.arctan2(y, x))


class Helmert:
    """local datum -> WGS84 as published (EPSG position-vector convention: metres, arc-seconds, ppm); used backwards
    here, WGS84 -> local, by applying the opposite shift (exact to millimetres for these small rotations)"""

    def __init__(self, ell, tx, ty, tz, rx=0.0, ry=0.0, rz=0.0, ds=0.0):
        self.ell = ell
        self.t = (-tx, -ty, -tz)
        k = math.pi / 180 / 3600
        self.r = (-rx * k, -ry * k, -rz * k)
        self.s = -ds * 1e-6

    def from_wgs84(self, lat, lon):
        x, y, z = _ecef(lat, lon, WGS84)
        rx, ry, rz = self.r
        m = 1 + self.s
        x2 = self.t[0] + m * (x - rz * y + ry * z)
        y2 = self.t[1] + m * (rz * x + y - rx * z)
        z2 = self.t[2] + m * (-ry * x + rx * y + z)
        return _geodetic(x2, y2, z2, self.ell)


class Projection:
    """forward(lat, lon) -> (x, y) metres; datum: a Helmert from WGS84, or None (the source's datum is taken as WGS84)"""
    datum = None

    def forward(self, lat, lon):
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        if self.datum is not None:
            lat, lon = self.datum.from_wgs84(lat, lon)
        return self._project(lat, lon)

    def _project(self, lat, lon):
        raise NotImplementedError


class Geographic(Projection):
    """x = longitude, y = latitude, degrees (EPSG:4326, 4258)"""

    def _project(self, lat, lon):
        return lon, lat


GEOGRAPHIC = Geographic()


class TransverseMercator(Projection):
    def __init__(self, ell, lon0, k0=1.0, fe=0.0, fn=0.0, lat0=0.0, datum=None):
        self.ell, self.lon0, self.k0, self.fe, self.fn, self.datum = ell, float(lon0), float(k0), fe, fn, datum
        n = ell.f / (2 - ell.f)
        self.A = ell.a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64 + n ** 6 / 256)
        self.alpha = (n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16 + 41 * n ** 4 / 180 - 127 * n ** 5 / 288 + 7891 * n ** 6 / 37800,
                      13 * n ** 2 / 48 - 3 * n ** 3 / 5 + 557 * n ** 4 / 1440 + 281 * n ** 5 / 630 - 1983433 * n ** 6 / 1935360,
                      61 * n ** 3 / 240 - 103 * n ** 4 / 140 + 15061 * n ** 5 / 26880 + 167603 * n ** 6 / 181440,
                      49561 * n ** 4 / 161280 - 179 * n ** 5 / 168 + 6601661 * n ** 6 / 7257600,
                      34729 * n ** 5 / 80640 - 3418889 * n ** 6 / 1995840,
                      212378941 * n ** 6 / 319334400)
        self.y0 = 0.0
        if lat0:
            self.y0 = float(self._raw(np.float64(lat0), np.float64(self.lon0))[1])

    def _raw(self, lat, lon):
        e = self.ell.e
        phi = np.radians(lat)
        lam = np.radians(lon - self.lon0)
        s = np.sin(phi)
        t = np.sinh(np.arctanh(s) - e * np.arctanh(e * s))
        xi1 = np.arctan2(t, np.cos(lam))
        eta1 = np.arctanh(np.sin(lam) / np.sqrt(1 + t * t))
        xi, eta = xi1.copy(), eta1.copy()
        for j, a in enumerate(self.alpha, 1):
            xi = xi + a * np.sin(2 * j * xi1) * np.cosh(2 * j * eta1)
            eta = eta + a * np.cos(2 * j * xi1) * np.sinh(2 * j * eta1)
        return self.A * eta, self.A * xi

    def _project(self, lat, lon):
        x, y = self._raw(lat, lon)
        return self.fe + self.k0 * x, self.fn + self.k0 * (y - self.y0)


def utm(zone, south=False, ell=GRS80):
    return TransverseMercator(ell, zone * 6 - 183, 0.9996, 500000.0, 10000000.0 if south else 0.0)


def _t(phi, e):
    s = np.sin(phi)
    return np.tan(np.pi / 4 - phi / 2) / ((1 - e * s) / (1 + e * s)) ** (e / 2)


def _m(phi, e2):
    s = np.sin(phi)
    return np.cos(phi) / np.sqrt(1 - e2 * s * s)


class LambertConic(Projection):
    """Lambert conformal conic with two standard parallels (EPSG method 9802), or one (9801) when lat2 is None"""

    def __init__(self, ell, lat0, lon0, lat1, lat2=None, fe=0.0, fn=0.0, k0=1.0, datum=None):
        self.ell, self.lon0, self.fe, self.fn, self.datum = ell, math.radians(lon0), fe, fn, datum
        e, e2 = ell.e, ell.e2
        p0, p1 = math.radians(lat0), math.radians(lat1)
        if lat2 is None:                                 # 1SP: lat1 is the natural origin's latitude, scaled by k0
            self.n = math.sin(p1)
            self.F = float(_m(p1, e2)) / (self.n * float(_t(p1, e)) ** self.n)
            self.k = k0
        else:
            p2 = math.radians(lat2)
            m1, m2, t1, t2 = float(_m(p1, e2)), float(_m(p2, e2)), float(_t(p1, e)), float(_t(p2, e))
            self.n = (math.log(m1) - math.log(m2)) / (math.log(t1) - math.log(t2))
            self.F = m1 / (self.n * t1 ** self.n)
            self.k = 1.0
        self.r0 = ell.a * self.F * self.k * float(_t(p0, e)) ** self.n

    def _project(self, lat, lon):
        r = self.ell.a * self.F * self.k * _t(np.radians(lat), self.ell.e) ** self.n
        th = self.n * (np.radians(lon) - self.lon0)
        return self.fe + r * np.sin(th), self.fn + self.r0 - r * np.cos(th)


class PolarStereographic(Projection):
    """polar stereographic by its latitude of true scale (EPSG method 9829), north or south"""

    def __init__(self, ell, lat_ts, lon0, fe=0.0, fn=0.0):
        self.ell, self.lon0, self.fe, self.fn = ell, math.radians(lon0), fe, fn
        self.south = lat_ts < 0
        pf = math.radians(abs(lat_ts))
        e = ell.e
        tf, mf = float(_t(pf, e)), float(_m(pf, ell.e2))
        self.rho_per_t = ell.a * mf / tf

    def _project(self, lat, lon):
        phi = np.radians(lat)
        lam = np.radians(lon) - self.lon0
        if self.south:
            rho = self.rho_per_t * _t(-phi, self.ell.e)
            return self.fe + rho * np.sin(lam), self.fn + rho * np.cos(lam)
        rho = self.rho_per_t * _t(phi, self.ell.e)
        return self.fe + rho * np.sin(lam), self.fn - rho * np.cos(lam)


class LambertAzimuthal(Projection):
    """Lambert azimuthal equal area on the ellipsoid (EPSG method 9820), e.g. ETRS89-LAEA Europe"""

    def __init__(self, ell, lat0, lon0, fe=0.0, fn=0.0):
        self.ell, self.lon0, self.fe, self.fn = ell, math.radians(lon0), fe, fn
        e = ell.e
        self.qp = float(self._q(np.float64(math.pi / 2)))
        p0 = math.radians(lat0)
        self.b0 = math.asin(float(self._q(np.float64(p0))) / self.qp)
        self.rq = ell.a * math.sqrt(self.qp / 2)
        self.d = ell.a * (math.cos(p0) / math.sqrt(1 - ell.e2 * math.sin(p0) ** 2)) / (self.rq * math.cos(self.b0))
        self._e = e

    def _q(self, phi):
        e, e2 = self.ell.e, self.ell.e2
        s = np.sin(phi)
        return (1 - e2) * (s / (1 - e2 * s * s) - (1 / (2 * e)) * np.log((1 - e * s) / (1 + e * s)))

    def _project(self, lat, lon):
        beta = np.arcsin(np.clip(self._q(np.radians(lat)) / self.qp, -1, 1))
        dl = np.radians(lon) - self.lon0
        sb0, cb0 = math.sin(self.b0), math.cos(self.b0)
        b = self.rq * np.sqrt(2 / (1 + sb0 * np.sin(beta) + cb0 * np.cos(beta) * np.cos(dl)))
        return (self.fe + b * self.d * np.cos(beta) * np.sin(dl),
                self.fn + (b / self.d) * (cb0 * np.sin(beta) - sb0 * np.cos(beta) * np.cos(dl)))


class SwissLV95(Projection):
    """CH1903+ / LV95 (EPSG:2056) by swisstopo's approximate formulas from WGS84 (better than a metre)"""

    def _project(self, lat, lon):
        p = (np.asarray(lat) * 3600 - 169028.66) / 10000
        q = (np.asarray(lon) * 3600 - 26782.5) / 10000
        e = 2600072.37 + 211455.93 * q - 10938.51 * q * p - 0.36 * q * p * p - 44.54 * q ** 3
        n = 1200147.07 + 308807.95 * p + 3745.25 * q * q + 76.63 * p * p - 194.56 * q * q * p + 119.79 * p ** 3
        return e, n


class WebMercator(Projection):
    """EPSG:3857, metres"""

    def _project(self, lat, lon):
        phi = np.radians(np.clip(lat, -85.06, 85.06))
        return WGS84.a * np.radians(lon), WGS84.a * np.log(np.tan(np.pi / 4 + phi / 2))


# OSGB36 -> WGS84 (EPSG:1314, about 3 m over Great Britain)
OSGB36 = Helmert(AIRY, 446.448, -125.157, 542.060, 0.1502, 0.2470, 0.8421, -20.4894)


@functools.lru_cache(maxsize=None)                    # one object per code: windows key their projected grids on it
def epsg(code):
    """the projection of an EPSG code this app knows (KeyError otherwise)"""
    code = int(code)
    if code in (4326, 4258, 4269, 4617, 4283, 7844, 4167, 6668, 4612):
        return GEOGRAPHIC
    if 25828 <= code <= 25838:                           # ETRS89 / UTM 28N..38N
        return utm(code - 25800)
    if 32601 <= code <= 32660:
        return utm(code - 32600, ell=WGS84)
    if 32701 <= code <= 32760:
        return utm(code - 32700, south=True, ell=WGS84)
    if 26907 <= code <= 26923:                           # NAD83 / UTM 7N..23N
        return utm(code - 26900)
    if 2955 <= code <= 2962:                             # NAD83(CSRS) / UTM 11N..18N (and 19N..22N at 2960..2962)
        return utm(code - 2944)
    if 3154 <= code <= 3160:                             # NAD83(CSRS) / UTM 7N..10N, 14N; 3159 = 15N
        return utm({3154: 7, 3155: 8, 3156: 9, 3157: 10, 3158: 14, 3159: 15, 3160: 16}[code])
    if 6669 <= code <= 6687:                             # JGD2011 / Japan Plane Rectangular CS I..XIX
        return _japan(code - 6668)
    known = {
        2056: SwissLV95(),
        2154: LambertConic(GRS80, 46.5, 3.0, 49.0, 44.0, 700000.0, 6600000.0),             # RGF93 / Lambert-93
        3416: LambertConic(GRS80, 47.5, 13.333333333333334, 49.0, 46.0, 400000.0, 400000.0),  # ETRS89 / Austria Lambert
        31287: LambertConic(BESSEL, 47.5, 13.333333333333334, 49.0, 46.0, 400000.0, 400000.0,
                            datum=Helmert(BESSEL, 577.326, 90.129, 463.919, 5.137, 1.474, 5.297, 2.4232)),  # MGI
        3979: LambertConic(GRS80, 49.0, -95.0, 49.0, 77.0),                              # NAD83(CSRS) Canada Atlas
        3978: LambertConic(GRS80, 49.0, -95.0, 49.0, 77.0),                              # NAD83 Canada Atlas
        3347: LambertConic(GRS80, 63.390675, -91.86666666666666, 49.0, 77.0, 6200000.0, 3000000.0),  # StatCan
        3005: None,
        2193: TransverseMercator(GRS80, 173.0, 0.9996, 1600000.0, 10000000.0),          # NZGD2000 / NZTM 2000
        3067: utm(35),                                                                  # ETRS-TM35FIN
        3006: TransverseMercator(GRS80, 15.0, 0.9996, 500000.0, 0.0),                   # SWEREF99 TM
        5972: utm(32), 5973: utm(33), 5975: utm(35),                                    # ETRS89 / UTM + NN2000
        2180: TransverseMercator(GRS80, 19.0, 0.9993, 500000.0, -5300000.0),            # Poland CS92
        3794: TransverseMercator(GRS80, 15.0, 0.9999, 500000.0, -5000000.0),            # Slovenia D96/TM
        3765: TransverseMercator(GRS80, 16.5, 0.9999, 500000.0, 0.0),                   # Croatia HTRS96/TM
        3057: LambertConic(GRS80, 65.0, -19.0, 64.25, 65.75, 500000.0, 500000.0),       # ISN93 / Lambert 1993
        5325: LambertConic(GRS80, 65.0, -19.0, 64.25, 65.75, 1700000.0, 300000.0),     # ISN2004 / Lambert 2004
        8088: LambertConic(GRS80, 65.0, -19.0, 64.25, 65.75, 2700000.0, 300000.0),     # ISN2016 / Lambert 2016
        27700: TransverseMercator(AIRY, -2.0, 0.9996012717, 400000.0, -100000.0, lat0=49.0, datum=OSGB36),
        3413: PolarStereographic(WGS84, 70.0, -45.0),                                   # NSIDC Sea Ice Polar Stereo N
        3031: PolarStereographic(WGS84, -71.0, 0.0),                                    # Antarctic Polar Stereographic
        3857: WebMercator(),
        3035: LambertAzimuthal(GRS80, 52.0, 10.0, 4321000.0, 3210000.0),                 # ETRS89-LAEA Europe
    }
    del known[3005]                                     # NAD83 / BC Albers: an equal-area conic, not offered
    p = known.get(code)
    if p is None:
        raise KeyError('EPSG:%d is not a projection this app knows' % code)
    return p


_JAPAN_ORIGINS = {1: (33, 129.5), 2: (33, 131), 3: (36, 132 + 1 / 6), 4: (33, 133.5), 5: (36, 134 + 1 / 3),
                  6: (36, 136), 7: (36, 137 + 1 / 6), 8: (36, 138.5), 9: (36, 139 + 5 / 6), 10: (40, 140 + 5 / 6),
                  11: (44, 140.25), 12: (44, 142.25), 13: (44, 144.25), 14: (26, 142), 15: (26, 127.5),
                  16: (26, 124), 17: (26, 131), 18: (20, 136), 19: (26, 154)}


def _japan(zone):
    lat0, lon0 = _JAPAN_ORIGINS[zone]
    return TransverseMercator(GRS80, lon0, 0.9999, 0.0, 0.0, lat0=lat0)
