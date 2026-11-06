"""Backend-neutral native map frame; no Creator imports or executable required.

Forward WGS84 Transverse Mercator, Krueger series through order six.
Formulation: https://proj.org/en/stable/operations/projections/tmerc.html
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TransverseMercator:
    lon0: float
    lat0: float = 0.0
    k0: float = 1.0
    false_easting: float = 0.0
    false_northing: float = 0.0

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        if (not all(math.isfinite(v) for v in (lon, lat, self.lon0, self.lat0, self.k0,
                                               self.false_easting, self.false_northing))
                or abs(lat) >= 80 or abs(self.lat0) >= 80 or abs(lon-self.lon0) > 3 or self.k0 <= 0):
            raise ValueError("Projection outside the validated narrow WGS84 TM domain")
        f = 1 / 298.257223563
        n = f / (2-f)
        e = math.sqrt(f*(2-f))
        scale = 6378137 / (1+n) * (1+n*n/4+n**4/64+n**6/256)
        alpha = (
            n/2-2*n*n/3+5*n**3/16+41*n**4/180-127*n**5/288+7891*n**6/37800,
            13*n*n/48-3*n**3/5+557*n**4/1440+281*n**5/630-1983433*n**6/1935360,
            61*n**3/240-103*n**4/140+15061*n**5/26880+167603*n**6/181440,
            49561*n**4/161280-179*n**5/168+6601661*n**6/7257600,
            34729*n**5/80640-3418889*n**6/1995840,
            212378941*n**6/319334400,
        )

        def raw(longitude: float, latitude: float) -> tuple[float, float]:
            phi, lam = math.radians(latitude), math.radians(longitude-self.lon0)
            s = math.sin(phi)
            tau = math.sinh(math.atanh(s)-e*math.atanh(e*s))
            xi0 = math.atan2(tau, math.cos(lam))
            eta0 = math.asinh(math.sin(lam)/math.hypot(tau, math.cos(lam)))
            xi, eta = xi0, eta0
            for j, coefficient in enumerate(alpha, 1):
                xi += coefficient*math.sin(2*j*xi0)*math.cosh(2*j*eta0)
                eta += coefficient*math.cos(2*j*xi0)*math.sinh(2*j*eta0)
            return eta*scale, xi*scale

        east, north = raw(lon, lat)
        _, north0 = raw(self.lon0, self.lat0)
        return (east*self.k0+self.false_easting,
                (north-north0)*self.k0+self.false_northing)


@dataclass(frozen=True)
class NativeMapFrame:
    package: str
    projection: TransverseMercator
    size_m: tuple[float, float]
    center_projected_m: tuple[float, float] = (0.0, 0.0)
    world_center_cm: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        for name in ("size_m", "center_projected_m", "world_center_cm"):
            values=tuple(getattr(self,name))
            if len(values)!=2:
                raise ValueError("Map frame vectors must have two components")
            object.__setattr__(self,name,values)
        if (not self.package.startswith("/Game/") or "." in self.package or ".." in self.package
                or not all(math.isfinite(v) for v in (*self.size_m, *self.center_projected_m, *self.world_center_cm))
                or len(self.size_m) != 2 or min(self.size_m) <= 0):
            raise ValueError("Invalid native map frame")

    def local(self, lon: float, lat: float) -> tuple[float, float]:
        east, north = self.projection.project(lon, lat)
        return east-self.center_projected_m[0], self.center_projected_m[1]-north

    def contains(self, point: tuple[float, float]) -> bool:
        return abs(point[0]) <= self.size_m[0]/2 and abs(point[1]) <= self.size_m[1]/2

    def world(self, point: tuple[float, float]) -> list[float]:
        if not self.contains(point):
            raise ValueError("Point outside native map; no clipping/scaling is performed")
        return [self.world_center_cm[i]+100*point[i] for i in (0, 1)]

    def descriptor(self) -> dict:
        return {"format": "real-resort-native-map", "version": 1, "terrain_owner": "native",
                "axes": ["east", "south"], "frame": asdict(self)}
