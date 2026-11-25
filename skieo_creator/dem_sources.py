"""The national and regional elevation sources (see dem.py for how they are picked and merged), all bare-earth
terrain models (DTM) that are open, free and need no account or key:

  * Mapterhorn: one worldwide set of terrain tiles that already holds the open national models (swisstopo, IGN
    France and Spain, the Austrian and German states, Kartverket, Lantmaeteriet, MML, LINZ, GSI, Geoscience
    Australia, NRCan and many more, 0.25 to 10 m), Copernicus 30 m elsewhere: its tiles at zoom 13 and finer exist
    only where it has national data, which is what is used here.
  * before it, national services that are finer than what Mapterhorn holds there, or that it lacks: CUZK (Czechia,
    1 m), LidarBC (British Columbia, 1 m), Ontario (0.5 m);
  * after it, each country's own server for what Mapterhorn holds, in case it is down or has a hole: swisstopo,
    IGN France, IGN Spain, Kartverket, GSI Japan, NRCan HRDEM, Bolzano, Piemonte, Lombardia, INGV TINITALY.

VERSION goes up whenever a source is added or changed: windows loaded before then look again (the cache keeps the
elevation per window and source, and which sources a window took).
"""
import io
import json
import math
import re
import urllib.parse

import numpy as np
from PIL import Image

from . import crs, dem, net

VERSION = 4
OVERVIEW = 'https://mapterhorn.com/attribution'


def _box(w, s, e, n):
    return ((w, s, e, n),)


# ---------------------------------------------------------------- Mapterhorn (worldwide, national data)

class Mapterhorn(dem.XyzTiles):
    id = 'mapterhorn'
    name = 'Mapterhorn (national data)'
    credit = ('Elevation: Mapterhorn (mapterhorn.com), from the open national elevation models of the area: see '
              + OVERVIEW)
    url = 'https://tiles.mapterhorn.com/{z}/{x}/{y}.webp'
    tile = 512
    order = 20
    res = 1.0
    per_host = 8
    missing_ok = True
    MIN_Z, MAX_Z = 13, 17                    # 13 and finer: only where it has national data

    def decode(self, data):
        with Image.open(io.BytesIO(data)) as im:
            a = np.asarray(im.convert('RGB')).astype(np.float64)
        return a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768

    def get_tile(self, z, x, y, cancel):
        x %= 2 ** z
        data = dem.cached_get(self.id, self.url.format(z=z, x=x, y=y), cancel, timeout=60, retries=3,
                              per_host=self.per_host, not_found_ok=True)
        return None if data is None else self.decode(data)

    def _exists(self, z, lat, lon, cancel):
        px, py = dem._lonlat_to_px(np.array([lon]), np.array([lat]), z)     # pixels of 256: the tile index is
        x, y = int(px[0]) // 256, int(py[0]) // 256                         # the same whatever the tile size
        data = dem.cached_get(self.id, self.url.format(z=z, x=x, y=y), cancel, timeout=60, retries=3,
                              per_host=self.per_host, not_found_ok=True)
        return data is not None

    def fetch(self, win, cancel, progress):
        # the zoom whose pixel is about one grid step (up to a quarter coarser), else the finest one this area has
        want = self.MAX_Z
        for z in range(self.MIN_Z, self.MAX_Z + 1):
            if 40075016.686 / (self.tile * 2 ** z) * math.cos(math.radians(win.lat)) <= win.spacing * 1.25:
                want = z
                break
        w, s, e, n = win.box
        probes = [(win.lat, win.lon), (s, w), (s, e), (n, w), (n, e)]
        progress(0.02, 'Looking for national elevation data...')
        for z in range(want, self.MIN_Z - 1, -1):
            if any(self._exists(z, la, lo, cancel) for la, lo in probes):
                self.zoom = z
                return super().fetch(win, cancel, progress)
        return None


# ---------------------------------------------------------------- finer than Mapterhorn there

class Cuzk(dem.ImageServer):
    """Czechia: CUZK DMR 5G (lidar, 1 m class), reprojected by the server. After Mapterhorn (its 5 m from CUZK): the
    server's own S-JTSK to WGS84 shift left it ~3 m off Mapterhorn's at Spindleruv Mlyn (2026-11-25)"""
    id = 'cz_dmr5g'
    name = 'CUZK DMR 5G (1 m)'
    credit = 'Elevation: (c) CUZK (Czech Office for Surveying, Mapping and Cadastre), DMR 5G, CC BY 4.0'
    url = 'https://ags.cuzk.cz/arcgis2/rest/services/dmr5g/ImageServer'
    res = 1.0
    order = 30
    boxes = _box(12.0, 48.5, 18.9, 51.1)


class Ontario(dem.ImageServer):
    """Ontario Digital Terrain Model (lidar-derived), 0.5 m"""
    id = 'ca_on_dtm'
    name = 'Ontario lidar DTM (0.5 m)'
    credit = ('Elevation: contains information licensed under the Open Government Licence - Ontario (Ontario Digital '
              'Terrain Model, Lidar-Derived)')
    url = 'https://ws.geoservices.lrc.gov.on.ca/arcgis5/rest/services/Elevation/Ontario_DTM_LidarDerived/ImageServer'
    extra = '&noDataInterpretation=esriNoDataMatchAny'
    res = 0.5
    order = 10
    boxes = _box(-95.2, 41.6, -74.3, 56.9)


class LidarBC(dem.Tiled):
    """British Columbia: LidarBC 1 m bare-earth DEM tiles (UTM zones 7 to 11), found through its public index"""
    id = 'ca_bc_lidar'
    name = 'LidarBC (1 m)'
    credit = ('Elevation: contains information licensed under the Open Government Licence - British Columbia '
              '(LidarBC, GeoBC)')
    res = 1.0
    order = 10
    boxes = _box(-139.1, 48.2, -114.0, 60.0)
    INDEX = ('https://services6.arcgis.com/ubm4tcTYICKBpist/arcgis/rest/services/LiDAR_BC_S3_Public/FeatureServer/%d/'
             'query?geometry={w},{s},{e},{n}&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel='
             'esriSpatialRelIntersects&outFields=filename,year,s3Url&returnGeometry=false&f=json')

    MAX_TILES = 40                      # more than this many tiles: the 1:2 500 ones are left to Mapterhorn

    def tiles(self, win, cancel):
        """{EPSG: [url]}: the 1:20 000 (layer 6) and, for squares up to 8 km, the 1:2 500 (layer 5) DEM tiles under
        the window, oldest first"""
        w, s, e, n = win.box
        found = {}
        for layer in (6, 5) if win.size_m <= 8000 else (6,):
            r = json.loads(net.get((self.INDEX % layer).format(w=w, s=s, e=e, n=n), timeout=60, retries=2,
                                   cancel=cancel))
            for f in r.get('features') or ():
                a = f.get('attributes') or {}
                url = a.get('s3Url') or ''
                m = re.search(r'_utm(\d+)', url)
                if not (url.startswith('https://nrs.objectstore.gov.bc.ca/') and m):
                    continue
                zone = int(m.group(1))
                found.setdefault(zone, []).append((int(a.get('year') or 0), url))
        out = {}
        for zone, lst in found.items():
            lst.sort(reverse=True)                     # newest first: pasted last wins, so paste oldest first
            out[{7: 3154, 8: 3155, 9: 3156, 10: 3157, 11: 2955}.get(zone, 26900 + zone)] = [u for _, u in reversed(lst)]
        return out

    def fetch(self, win, cancel, progress):
        from concurrent.futures import ThreadPoolExecutor

        from . import cog
        groups = self.tiles(win, cancel)
        if not groups:
            return None
        layers = []
        total = min(sum(len(v) for v in groups.values()), self.MAX_TILES)
        done = [0]
        for code, urls in groups.items():
            proj = crs.epsg(code)
            box = win.native_box(proj)
            k = dem.Mosaic.factor(self.res, win.pixel(self.res, 1.5))
            mos = dem.Mosaic(proj, box, self.res * k, (0.0, 0.0))

            def read(url):
                cancel.check()
                try:
                    c = cog.open_url(url, cancel)
                    return c.read(c.level_for(self.res * k), *box, threads=3)
                except (cog.CogError, net.NetError, ValueError):
                    return None
                finally:
                    done[0] += 1
                    progress(0.05 + 0.75 * min(done[0], total) / total,
                             'Downloading LidarBC (%d of %d tiles)...' % (min(done[0], total), total))

            with ThreadPoolExecutor(4) as ex:
                parts = list(ex.map(read, urls[-self.MAX_TILES:]))
            for part in parts:                          # in order: newer tiles over older ones
                if part is not None:
                    a, x0, y0, dx, dy = part
                    mos.paste(a, x0, y0, dx, dy)
            if np.isfinite(mos.a).any():
                layers.append(mos.sample(win))
        if not layers:
            return None
        out = layers[0]
        for d in layers[1:]:
            out = np.where(np.isfinite(out), out, d)
        return out


# ---------------------------------------------------------------- each country's own server (after Mapterhorn)

class SwissAlti3d(dem.Tiled):
    """Switzerland and Liechtenstein: swissALTI3D 2 m, 1 km COG tiles found through swisstopo's STAC catalog"""
    id = 'ch_swissalti3d'
    name = 'swisstopo swissALTI3D (2 m)'
    credit = 'Elevation: Federal Office of Topography swisstopo, swissALTI3D (Open Government Data)'
    res = 2.0
    order = 30
    tile_px = 500
    whole_below = 0                             # range reads: only the overview the grid needs
    boxes = _box(5.95, 45.81, 10.50, 47.81)
    proj = crs.epsg(2056)
    STAC = 'https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d/items?bbox={w},{s},{e},{n}&limit=100'

    def tiles(self, box, cancel):
        """the newest 2 m tile of every 1 km square under the window"""
        w, s, e, n = self.win_box
        url = self.STAC.format(w=w, s=s, e=e, n=n)
        best = {}
        for _ in range(50):
            r = json.loads(net.get(url, timeout=60, retries=2, cancel=cancel))
            for f in r.get('features') or ():
                m = re.fullmatch(r'swissalti3d_(\d{4})_(\d+-\d+)', f.get('id', ''))
                if not m:
                    continue
                year, sq = int(m.group(1)), m.group(2)
                for a in (f.get('assets') or {}).values():
                    href = a.get('href', '')
                    if href.endswith('_2_2056_5728.tif') and href.startswith('https://data.geo.admin.ch/'):
                        if year > best.get(sq, (0, ''))[0]:
                            best[sq] = (year, href)
            nxt = [l['href'] for l in r.get('links') or () if l.get('rel') == 'next']
            if not nxt:
                break
            url = nxt[0]
        return [(href, None) for _, href in best.values()]

    def fetch(self, win, cancel, progress):
        self.win_box = win.box
        return super().fetch(win, cancel, progress)


class BevAustria(dem.Tiled):
    """Austria: BEV's national lidar terrain model (ALS-DGM, 1 m), 50 km COGs in ETRS89-LAEA read by range requests"""
    id = 'at_bev_als'
    name = 'BEV ALS-DGM (1 m)'
    credit = 'Elevation: BEV - Bundesamt fuer Eich- und Vermessungswesen, ALS-DGM Hoehenraster 1 m (CC BY 4.0)'
    res = 1.0
    order = 30
    whole_below = 0
    proj = crs.epsg(3035)
    align = (-0.5, 0.5)                          # its pixel corners sit on half metres
    boxes = _box(9.4, 46.3, 17.2, 49.1)
    DATES = ('20250915', '20240915', '20230915', '20220915', '20210915', '20190915')
    URL = 'https://data.bev.gv.at/download/ALS/DTM/%s/ALS_DTM_CRS3035RES50000mN%dE%d.tif'

    def tiles(self, box, cancel):
        x_min, y_min, x_max, y_max = box
        out = []
        for e0 in range(int(x_min // 50000) * 50000, int(x_max // 50000) * 50000 + 1, 50000):
            for n0 in range(int(y_min // 50000) * 50000, int(y_max // 50000) * 50000 + 1, 50000):
                for date in self.DATES:                # the newest edition that has this tile (404: not in it;
                    url = self.URL % (date, n0, e0)    # any other failure is the server's, and fails the source)
                    size = net.content_length(url, cancel=cancel, not_found_ok=True)
                    if size is not None:
                        out.append((url, size))
                        break
        return out


class IgnFrance(dem.Gridded):
    """France: IGN LiDAR HD terrain model (1 m), reprojected by the Geoplateforme's WMS (no data outside it)"""
    id = 'fr_ign_lidarhd'
    name = 'IGN LiDAR HD (1 m)'
    credit = 'Elevation: IGN, LiDAR HD MNT (Licence Ouverte Etalab 2.0)'
    res = 1.0
    order = 30
    max_px = 4000
    nodata = -9999.0
    boxes = _box(-5.3, 41.3, 9.7, 51.2)

    def request(self, x_min, y_min, x_max, y_max, nx, ny):
        return ('https://data.geopf.fr/wms-r/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS='
                'IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.WGS84G&STYLES=&CRS=EPSG:4326&BBOX=%.8f,%.8f,%.8f,'
                '%.8f&WIDTH=%d&HEIGHT=%d&FORMAT=image/geotiff' % (y_min, x_min, y_max, x_max, nx, ny))


class IgnSpain(dem.Gridded):
    """Spain: IGN MDT05 (5 m lidar terrain model, whole metres), its WCS in ETRS89 latitude/longitude"""
    id = 'es_ign_mdt05'
    name = 'IGN Espana MDT05 (5 m)'
    credit = 'Elevation: (c) Instituto Geografico Nacional de Espana, MDT05, CC BY 4.0 scne.es'
    res = 5.0
    order = 30
    max_px = 2500
    boxes = ((-9.4, 35.9, 4.4, 43.9),)

    def request(self, x_min, y_min, x_max, y_max, nx, ny):
        return ('https://servicios.idee.es/wcs-inspire/mdt?service=WCS&version=2.0.1&request=GetCoverage&coverageId='
                'Elevacion4258_5&format=image/tiff&subset=Lat(%.8f,%.8f)&subset=Long(%.8f,%.8f)&scalesize=Lat(%d),'
                'Long(%d)' % (y_min, y_max, x_min, x_max, ny, nx))

    def decode(self, data, nx, ny):
        img, ref = super().decode(data, nx, ny)
        img = img.astype(np.float64)
        img[img == 0] = np.nan                  # outside Spain: 0, not a no-data value
        return img, ref

    def fetch(self, win, cancel, progress):
        d = super().fetch(win, cancel, progress)
        if d is not None:                       # whole metres: smooth the 1 m terraces a 5 m grid leaves
            ok = np.isfinite(d)
            d = dem.gaussian(np.where(ok, d, np.nanmean(d)), max(0.6, 3.0 / win.spacing))
            d[~ok] = np.nan
        return d


class Kartverket(dem.Gridded):
    """Norway: Kartverket's national terrain model (lidar, 1 m), WCS 1.0 in latitude/longitude"""
    id = 'no_kartverket'
    name = 'Kartverket DTM (1 m)'
    credit = 'Elevation: (c) Kartverket (Norwegian Mapping Authority), CC BY 4.0'
    res = 1.0
    order = 30
    max_px = 2048
    boxes = _box(4.0, 57.8, 31.5, 71.3)

    def request(self, x_min, y_min, x_max, y_max, nx, ny):
        return ('https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage'
                '&COVERAGE=nhm_dtm_topo_25833&CRS=EPSG:4326&BBOX=%.8f,%.8f,%.8f,%.8f&WIDTH=%d&HEIGHT=%d&FORMAT=GeoTIFF'
                '&INTERPOLATION=bilinear' % (x_min, y_min, x_max, y_max, nx, ny))

    def decode(self, data, nx, ny):
        img, ref = super().decode(data, nx, ny)
        img = img.astype(np.float64)
        img[img == 0.0] = np.nan                # outside Norway (and the sea): 0.0, not a no-data value
        return img, ref


class Hrdem(dem.Gridded):
    """Canada: NRCan's HRDEM mosaic (lidar, 1 to 2 m, where it has data), WCS 1.1.1 in latitude/longitude"""
    id = 'ca_hrdem'
    name = 'NRCan HRDEM (1 m)'
    credit = ('Elevation: contains information licensed under the Open Government Licence - Canada (NRCan, '
              'High Resolution Digital Elevation Model)')
    res = 1.0
    order = 30
    max_px = 3000
    nodata = -32767.0
    boxes = _box(-141.1, 41.6, -52.5, 70.0)

    def request(self, x_min, y_min, x_max, y_max, nx, ny):
        # EPSG::4326 is latitude first: the box, the origin and the offsets (-lat step, lon step); the origin is the
        # first grid point, the north-west pixel's centre (WCS 1.1; measured ~0.25 step closer to Mapterhorn's copy)
        dx, dy = (x_max - x_min) / nx, (y_max - y_min) / ny
        return ('https://datacube.services.geo.ca/wrapper/ogc/elevation-hrdem-mosaic?service=WCS&version=1.1.1&request='
                'GetCoverage&identifier=dtm&format=image/geotiff&BoundingBox=%.8f,%.8f,%.8f,%.8f,urn:ogc:def:crs:EPSG::'
                '4326&GridBaseCRS=urn:ogc:def:crs:EPSG::4326&GridCS=urn:ogc:def:cs:OGC:0:Gridded2DIndex&GridType=urn:'
                'ogc:def:method:WCS:1.1:2dGridIn2dCrs&GridOrigin=%.8f,%.8f&GridOffsets=%.10f,%.10f'
                % (y_min, x_min, y_max, x_max, y_max - dy / 2, x_min + dx / 2, -dy, dx))


class GsiJapan(dem.XyzTiles):
    """Japan: GSI's elevation tiles (PNG, 0.01 m); a missing tile is no data"""
    credit = 'Elevation: Geospatial Information Authority of Japan (GSI) elevation tiles'
    order = 30
    missing_ok = True
    # Hokkaido; Honshu, Shikoku, Kyushu; the Ryukyus (not Korea or Russia's coast: there every tile is a 404)
    boxes = ((139.3, 41.3, 146.0, 45.6), (129.5, 30.9, 142.2, 41.7), (122.9, 24.0, 131.4, 30.9))
    layers = ()                               # tried in turn for each tile

    def decode(self, data):
        a = np.asarray(Image.open(io.BytesIO(data)).convert('RGB')).astype(np.int64)
        v = (a[..., 0] << 16) | (a[..., 1] << 8) | a[..., 2]
        h = np.where(v >= 1 << 23, v - (1 << 24), v) * 0.01
        return np.where(v == 1 << 23, np.nan, h.astype(np.float64))

    def get_tile(self, z, x, y, cancel):
        for layer in self.layers:
            data = dem.cached_get(self.id, 'https://cyberjapandata.gsi.go.jp/xyz/%s/%d/%d/%d.png' % (layer, z, x, y),
                                  cancel, timeout=60, retries=3, per_host=6, not_found_ok=True)
            if data is not None:
                return self.decode(data)
        return None

    def fetch(self, win, cancel, progress):
        # the square's centre and corners first: no tile there, no fan-out over hundreds of 404s (the sea, abroad)
        w, s, e, n = win.box
        for la, lo in ((win.lat, win.lon), (s, w), (s, e), (n, w), (n, e)):
            px, py = dem._lonlat_to_px(np.array([lo]), np.array([la]), self.zoom)
            if self.get_tile(self.zoom, int(px[0]) // 256, int(py[0]) // 256, cancel) is not None:
                return super().fetch(win, cancel, progress)
        return None


class Gsi5(GsiJapan):
    id = 'jp_gsi_dem5'
    name = 'GSI DEM5 (5 m)'
    res = 5.0
    zoom = 15
    layers = ('dem5a_png', 'dem5b_png', 'dem5c_png')


class Gsi10(GsiJapan):
    id = 'jp_gsi_dem10'
    name = 'GSI DEM10 (10 m)'
    res = 10.0
    zoom = 14
    order = 31
    layers = ('dem_png',)


class NswElevation(dem.Gridded):
    """New South Wales (Thredbo, Perisher, Charlotte Pass, Selwyn): Spatial Services' 5 m elevation, asked for in its
    own web Mercator (the server is slow: ~1 minute an 8 km window; reprojected by it, eight times slower)"""
    id = 'au_nsw_5m'
    name = 'NSW Spatial Services (5 m)'
    credit = 'Elevation: (c) State of New South Wales (Spatial Services, Department of Customer Service NSW), CC BY 4.0'
    res = 5.0
    order = 30
    max_px = 1600
    per_host = 2
    timeout = 420
    nodata = -9999.0
    proj = crs.WebMercator()
    boxes = _box(140.9, -37.6, 153.7, -28.1)

    def request(self, x_min, y_min, x_max, y_max, nx, ny):
        return ('https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_5M_Elevation/ImageServer/exportImage?bbox='
                '%.2f,%.2f,%.2f,%.2f&bboxSR=3857&imageSR=3857&size=%d,%d&format=tiff&pixelType=F32&noData=-9999'
                '&interpolation=RSP_BilinearInterpolation&adjustAspectRatio=false&f=image'
                % (x_min, y_min, x_max, y_max, nx, ny))


class GeoServerWcs(dem.Gridded):
    """a GeoServer/MapServer WCS 2.0.1 coverage, asked for in its own UTM coordinates (no edge wedges)"""
    base = ''
    coverage = ''
    utm = 32

    def __init__(self):
        self.proj = crs.utm(self.utm, ell=crs.WGS84 if self.wgs84 else crs.GRS80)

    wgs84 = False

    def request(self, x_min, y_min, x_max, y_max, nx, ny):
        code = (32600 if self.wgs84 else 25800) + self.utm
        return ('%s?service=WCS&version=2.0.1&request=GetCoverage&coverageId=%s&format=image/tiff&subset=E(%.3f,%.3f)'
                '&subset=N(%.3f,%.3f)&subsettingcrs=http://www.opengis.net/def/crs/EPSG/0/%d&scalesize=i(%d),j(%d)'
                % (self.base, self.coverage, x_min, x_max, y_min, y_max, code, nx, ny))


class Bolzano(GeoServerWcs):
    """South Tyrol: the Province of Bolzano's lidar terrain model, 2.5 m (its 0.5 m model has holes: 45% at Ortisei)"""
    id = 'it_bz_dtm'
    name = 'Provincia di Bolzano DTM (2.5 m)'
    credit = 'Elevation: Autonome Provinz Bozen - Suedtirol / Provincia autonoma di Bolzano (CC0)'
    base = 'https://geoservices9.civis.bz.it/geoserver/ows'
    coverage = 'p_bz-Elevation__DigitalTerrainModel-2.5m'
    res = 2.5
    order = 30
    max_px = 2500
    nodata = -9999.0
    boxes = _box(10.37, 46.21, 12.48, 47.10)


class Piemonte(dem.Gridded):
    """Piemonte: the Region's lidar terrain model (ICE 2009-2011, 5 m), MapServer WCS 1.0 in UTM 32N"""
    id = 'it_piemonte'
    name = 'Regione Piemonte DTM (5 m)'
    credit = 'Elevation: Regione Piemonte - Geoportale Piemonte, DTM 5 m ICE 2009-2011 (CC BY 4.0)'
    res = 5.0
    order = 30
    max_px = 2100
    nodata = -99.0
    boxes = _box(6.27, 43.88, 9.48, 46.64)
    proj = crs.utm(32, ell=crs.WGS84)

    def request(self, x_min, y_min, x_max, y_max, nx, ny):
        return ('https://geomap.reteunitaria.piemonte.it/ws/taims/rp-01/taimsdtmwcs/wcs_ice_2009_2011_dtm?service=WCS'
                '&version=1.0.0&request=GetCoverage&coverage=DTM&crs=EPSG:32632&bbox=%.3f,%.3f,%.3f,%.3f&width=%d'
                '&height=%d&format=image/tiff' % (x_min, y_min, x_max, y_max, nx, ny))


class Lombardia(dem.ImageServer):
    """Lombardia: the Region's 5 m terrain model (ed. 2015)"""
    id = 'it_lombardia'
    name = 'Regione Lombardia DTM (5 m)'
    credit = 'Elevation: Regione Lombardia - Geoportale della Lombardia, DTM 5x5 (ed. 2015)'
    url = 'https://www.cartografia.servizirl.it/arcgis2/rest/services/BaseMap/DTM5_RL_img/ImageServer'
    res = 5.0
    order = 30
    boxes = _box(8.49, 44.67, 11.43, 46.64)


class Tinitaly(GeoServerWcs):
    """Italy: INGV's TINITALY 1.1 (10 m), its WCS in UTM 32N"""
    id = 'it_tinitaly'
    name = 'INGV TINITALY (10 m)'
    credit = ('Elevation: Tarquini S. et al. (2023), TINITALY 1.1, Istituto Nazionale di Geofisica e Vulcanologia '
              '(INGV), CC BY 4.0, doi:10.13127/tinitaly/1.1')
    base = 'https://tinitaly.pi.ingv.it/TINItaly_1_1/wcs'
    coverage = 'TINItaly_1_1__tinitaly_dem'
    wgs84 = True
    res = 10.0
    order = 35
    max_px = 2500
    nodata = -9999.0
    boxes = ((6.6, 36.6, 18.6, 47.1),)

    def decode(self, data, nx, ny):
        img, ref = super().decode(data, nx, ny)
        img = img.astype(np.float64)
        img[img == 0.0] = np.nan                # outside Italy (and the sea): 0.0, not its no-data value
        return img, ref


for _s in (Mapterhorn(), Cuzk(), Ontario(), LidarBC(), SwissAlti3d(), BevAustria(), IgnFrance(), IgnSpain(),
           Kartverket(), Hrdem(), Gsi5(), Gsi10(), NswElevation(), Bolzano(), Piemonte(), Lombardia(), Tinitaly()):
    dem.register(_s)
del _s
