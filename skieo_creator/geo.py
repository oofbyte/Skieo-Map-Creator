"""Transverse Mercator on WGS84 (Krueger series to n^6, Karney 2011), vectorised with numpy.

The map creator lays every terrain window on a local transverse Mercator grid centred on the window: x east and y
north in metres from the centre, true north up, scale 1 at the central meridian. Over a 12 km window the scale error is
below 1e-6 and grid north differs from true north by less than 0.1 degrees, so the square the player draws on the map
is the square that gets built.
"""
import math

import numpy as np

A_AXIS = 6378137.0
FLAT = 1 / 298.257223563
_N = FLAT / (2 - FLAT)
_E = math.sqrt(FLAT * (2 - FLAT))
_A = A_AXIS / (1 + _N) * (1 + _N ** 2 / 4 + _N ** 4 / 64 + _N ** 6 / 256)
n = _N
_ALPHA = (n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16 + 41 * n ** 4 / 180 - 127 * n ** 5 / 288 + 7891 * n ** 6 / 37800,
          13 * n ** 2 / 48 - 3 * n ** 3 / 5 + 557 * n ** 4 / 1440 + 281 * n ** 5 / 630 - 1983433 * n ** 6 / 1935360,
          61 * n ** 3 / 240 - 103 * n ** 4 / 140 + 15061 * n ** 5 / 26880 + 167603 * n ** 6 / 181440,
          49561 * n ** 4 / 161280 - 179 * n ** 5 / 168 + 6601661 * n ** 6 / 7257600,
          34729 * n ** 5 / 80640 - 3418889 * n ** 6 / 1995840,
          212378941 * n ** 6 / 319334400)
_BETA = (n / 2 - 2 * n ** 2 / 3 + 37 * n ** 3 / 96 - n ** 4 / 360 - 81 * n ** 5 / 512 + 96199 * n ** 6 / 604800,
         n ** 2 / 48 + n ** 3 / 15 - 437 * n ** 4 / 1440 + 46 * n ** 5 / 105 - 1118711 * n ** 6 / 3870720,
         17 * n ** 3 / 480 - 37 * n ** 4 / 840 - 209 * n ** 5 / 4480 + 5569 * n ** 6 / 90720,
         4397 * n ** 4 / 161280 - 11 * n ** 5 / 504 - 830251 * n ** 6 / 7257600,
         4583 * n ** 5 / 161280 - 108847 * n ** 6 / 3991680,
         20648693 * n ** 6 / 638668800)
del n


def _forward(lat, lon, lon0):
    phi = np.radians(np.asarray(lat, np.float64))
    lam = np.radians(np.asarray(lon, np.float64) - lon0)
    s = np.sin(phi)
    t = np.sinh(np.arctanh(s) - _E * np.arctanh(_E * s))
    xi1 = np.arctan2(t, np.cos(lam))
    eta1 = np.arctanh(np.sin(lam) / np.sqrt(1 + t * t))
    xi, eta = xi1.copy(), eta1.copy()
    for j, a in enumerate(_ALPHA, 1):
        xi += a * np.sin(2 * j * xi1) * np.cosh(2 * j * eta1)
        eta += a * np.cos(2 * j * xi1) * np.sinh(2 * j * eta1)
    return _A * eta, _A * xi


def _inverse(x, y, lon0):
    xi = np.asarray(y, np.float64) / _A
    eta = np.asarray(x, np.float64) / _A
    xi1, eta1 = xi.copy(), eta.copy()
    for j, b in enumerate(_BETA, 1):
        xi1 -= b * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
        eta1 -= b * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
    tau1 = np.sin(xi1) / np.sqrt(np.sinh(eta1) ** 2 + np.cos(xi1) ** 2)
    lam = np.arctan2(np.sinh(eta1), np.cos(xi1))
    tau = tau1.copy()
    e2 = _E * _E
    for _ in range(5):                                   # Newton on the conformal latitude; converges in 2-3 steps
        sig = np.sinh(_E * np.arctanh(_E * tau / np.sqrt(1 + tau * tau)))
        ti = tau * np.sqrt(1 + sig * sig) - sig * np.sqrt(1 + tau * tau)
        tau = tau + (tau1 - ti) * (1 + (1 - e2) * tau * tau) / ((1 - e2) * np.sqrt((1 + ti * ti) * (1 + tau * tau)))
    return np.degrees(np.arctan(tau)), np.degrees(lam) + lon0


class LocalGrid:
    """Local transverse Mercator frame centred on (lat0, lon0): x east, y north, metres."""

    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = float(lat0), float(lon0)
        self._y0 = float(_forward(self.lat0, self.lon0, self.lon0)[1])

    def to_xy(self, lat, lon):
        x, y = _forward(lat, lon, self.lon0)
        return x, y - self._y0

    def to_latlon(self, x, y):
        return _inverse(x, np.asarray(y, np.float64) + self._y0, self.lon0)


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))
