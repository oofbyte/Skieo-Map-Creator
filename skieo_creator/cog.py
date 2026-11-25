"""Read a window of a GeoTIFF over HTTP range requests (Cloud-Optimized GeoTIFFs): the header and tile index, then
only the tiles under the window, from the coarsest overview fine enough for the grid. tifffile reads the structure;
tiles are decoded here with numpy and zlib, so no imagecodecs is needed: uncompressed, Deflate and LZW, with the
horizontal (2) or floating point (3) predictor.
"""
import logging
import math
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import net

log = logging.getLogger(__name__)


class CogError(Exception):
    pass


GAP = 16 << 10                       # tiles this close together in the file are fetched in one request
GROUP = 4 << 20                      # ... up to this many bytes


class RangeFile:
    """a read-only file object over HTTP range requests, in cached blocks (enough for tifffile to read the header)"""

    BLOCK = 1 << 16

    def __init__(self, url, size, cancel=net.NEVER, fetch=None):
        self.url, self.size, self.cancel = url, int(size), cancel
        self._fetch = fetch or (lambda a, b: net.get(url, timeout=60, retries=3, cancel=cancel, byte_range=(a, b)))
        self._blocks = {}
        self._pos = 0
        self._lock = threading.Lock()
        self.name = url

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, off, whence=0):
        self._pos = int(off) if whence == 0 else (self._pos + int(off) if whence == 1 else self.size + int(off))
        return self._pos

    def _block(self, i):
        with self._lock:
            b = self._blocks.get(i)
        if b is None:
            a = i * self.BLOCK
            b = self._fetch(a, min(self.size, a + self.BLOCK) - 1)
            with self._lock:
                self._blocks[i] = b
        return b

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self._pos
        n = max(0, min(n, self.size - self._pos))
        if not n:
            return b''
        if n > 4 * self.BLOCK:                       # big reads (tile index arrays) in one request
            data = self._fetch(self._pos, self._pos + n - 1)
        else:
            first, last = self._pos // self.BLOCK, (self._pos + n - 1) // self.BLOCK
            data = b''.join(self._block(i) for i in range(first, last + 1))
            off = self._pos - first * self.BLOCK
            data = data[off:off + n]
        self._pos += len(data)
        return data

    def readinto(self, buf):
        data = self.read(len(buf))
        buf[:len(data)] = data
        return len(data)

    def close(self):
        pass


def lzw_decode(data):
    """TIFF LZW (MSB-first codes, 9 to 12 bits, early change); pure Python, used only for tiles"""
    out = bytearray()
    table = [bytes((i,)) for i in range(256)] + [b'', b'']
    bitpos, nbits, nbits_total = 0, 9, len(data) * 8
    prev = None
    mv = bytes(data) + b'\0\0\0'
    while bitpos + nbits <= nbits_total:
        byte = bitpos >> 3
        chunk = (mv[byte] << 16) | (mv[byte + 1] << 8) | mv[byte + 2]
        code = (chunk >> (24 - (bitpos & 7) - nbits)) & ((1 << nbits) - 1)
        bitpos += nbits
        if code == 257:                              # end of information
            break
        if code == 256:                              # clear
            del table[258:]
            nbits = 9
            prev = None
            continue
        if prev is None:
            entry = table[code]
            out += entry
            prev = entry
            continue
        if code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        else:
            entry = prev + prev[:1]
            table.append(entry)
        out += entry
        prev = entry
        n = len(table) + 1                           # early change: widen one code early
        if n >= (1 << nbits) and nbits < 12:
            nbits += 1
    return bytes(out)


def _wrap(raw, page, shape, byteorder, tiled):
    """a TIFF holding only this compressed tile (or strip), as it is, for Pillow's libtiff to decode"""
    import struct
    rows, cols = shape
    dtype = np.dtype(page.dtype)
    comp = int(page.compression)
    tags = [(256, 3, 1, cols), (257, 3, 1, rows), (258, 3, 1, dtype.itemsize * 8), (259, 3, 1, comp), (262, 3, 1, 1),
            (277, 3, 1, 1), (284, 3, 1, 1), (317, 3, 1, int(getattr(page, 'predictor', 1) or 1)),
            (339, 3, 1, {'f': 3, 'i': 2, 'u': 1}[dtype.kind])]
    tags += [(322, 3, 1, cols), (323, 3, 1, rows), (324, 4, 1, None), (325, 4, 1, len(raw))] if tiled else \
        [(273, 4, 1, None), (278, 3, 1, rows), (279, 4, 1, len(raw))]
    extra = b''
    lerc = page.tags.get(50674)                       # LercParameters (version, additional compression)
    if comp == 34887 and lerc is not None:
        tags.append((50674, 4, 2, 'extra'))
        extra = struct.pack(byteorder + '2I', *[int(v) for v in lerc.value][:2])
    tags.sort()
    ifd_size = 2 + 12 * len(tags) + 4
    extra_at, data_at = 8 + ifd_size, 8 + ifd_size + len(extra)
    out = bytearray(b'II*\0' if byteorder == '<' else b'MM\0*')
    out += struct.pack(byteorder + 'I', 8)
    out += struct.pack(byteorder + 'H', len(tags))
    for tag, typ, n, val in tags:
        if val is None:
            val = data_at
        if val == 'extra':
            out += struct.pack(byteorder + 'HHII', tag, typ, n, extra_at)
        elif typ == 3:
            out += struct.pack(byteorder + 'HHIHH', tag, typ, n, val, 0)
        else:
            out += struct.pack(byteorder + 'HHII', tag, typ, n, val)
    out += struct.pack(byteorder + 'I', 0)
    out += extra + raw
    return bytes(out)


def _pillow(raw, page, shape, byteorder, tiled):
    import io
    from PIL import Image
    with Image.open(io.BytesIO(_wrap(raw, page, shape, byteorder, tiled))) as im:
        a = np.asarray(im)
    if a.shape != tuple(shape):
        raise CogError('Pillow decoded %s, not %s' % (a.shape, shape))
    return a.astype(np.dtype(page.dtype).newbyteorder('='))


def decode_tile(raw, page, shape, byteorder='<', tiled=True):
    """one tile's (or strip's) bytes -> array of `shape` (rows, cols) in the page's dtype, native byte order. LZW, ZSTD
    and LERC go to Pillow's libtiff (C, fast; ZSTD/LERC only if its libtiff has them); Deflate and uncompressed, and
    LZW where Pillow can't, are decoded here."""
    comp = int(page.compression)
    # little-endian files only: Pillow labels a big-endian float tile's samples wrongly (garbage, no error)
    if comp in (5, 34887, 50000, 50001) and np.dtype(page.dtype).kind == 'f' and byteorder == '<':
        try:
            return _pillow(raw, page, shape, byteorder, tiled)
        except Exception as e:                        # noqa: BLE001  (then the decoders below, where they can)
            if comp != 5:
                raise CogError('compression %d could not be decoded (%s)' % (comp, e))
    if comp == 1:
        data = raw
    elif comp in (8, 32946):
        data = zlib.decompress(raw)
    elif comp == 5:
        data = lzw_decode(raw)
    else:
        raise CogError('compression %d is not supported' % comp)
    dtype = np.dtype(page.dtype).newbyteorder('=')
    bps = dtype.itemsize
    rows, cols = shape
    need = rows * cols * bps
    if len(data) < need:
        data = bytes(data) + bytes(need - len(data))
    a = np.frombuffer(data, np.uint8, need).reshape(rows, cols * bps)
    pred = int(getattr(page, 'predictor', 1) or 1)
    if pred == 3:                        # floating point: bytes delta coded along the row, then planar, MSB first
        a = np.cumsum(a, axis=1, dtype=np.uint8).reshape(rows, bps, cols).transpose(0, 2, 1).copy()
        return a.view('>' + dtype.str[1:]).reshape(rows, cols).astype(dtype)
    v = a.view(byteorder + dtype.str[1:]).reshape(rows, cols)
    if pred == 2:                        # horizontal differencing on the samples' bits (integer wrap-around)
        u = v.view(byteorder + 'u%d' % bps)
        v = np.cumsum(u, axis=1, dtype=u.dtype).view(v.dtype)
    return v.astype(dtype)


def geokeys(page):
    """(EPSG code or None, (x0, y0, dx, dy)): the top-left corner and pixel size from the GeoTIFF tags"""
    from .dem import georef_tags
    tags = page.tags
    ref = georef_tags(tags)
    if ref is None:
        raise CogError('the GeoTIFF has no georeferencing')
    code = None
    gk = tags.get('GeoKeyDirectoryTag')
    if gk:
        v = list(gk.value)
        keys = {v[k]: v[k + 3] for k in range(4, len(v) - 3, 4) if v[k + 1] == 0}
        if keys.get(3072) not in (None, 0, 32767):               # ProjectedCSTypeGeoKey
            code = int(keys[3072])
        elif 3072 not in keys and keys.get(2048) not in (None, 0, 32767):   # geographic only (a user-defined
            code = int(keys[2048])                                # projection keeps its datum here: not a code)
    return code, ref


class Cog:
    """an open (remote or local) GeoTIFF: its levels (full resolution, then the overviews), georeferencing, nodata"""

    def __init__(self, fh, cancel=net.NEVER, fetch=None):
        import tifffile
        self.fh, self.cancel = fh, cancel
        self._fetch = fetch
        self.tif = tifffile.TiffFile(fh)
        base = self.tif.pages[0]
        self.epsg, (self.x0, self.y0, self.dx, self.dy) = geokeys(base)
        nd = base.tags.get('GDAL_NODATA')
        self.nodata = float(str(nd.value).strip('\0 ')) if nd is not None and str(nd.value).strip('\0 ') else None
        self.byteorder = self.tif.byteorder
        self.levels = [base] + [p for p in self.tif.pages[1:]
                                if int(getattr(p, 'subfiletype', 0)) & 1 and not int(getattr(p, 'subfiletype', 0)) & 4]
        for p in self.levels:
            if p.samplesperpixel != 1:
                raise CogError('the GeoTIFF has %d samples per pixel' % p.samplesperpixel)
        self.width, self.height = base.imagewidth, base.imagelength

    def level_for(self, pixel_m):
        """the coarsest level whose pixels are no bigger than pixel_m (in the file's units)"""
        best = self.levels[0]
        for p in self.levels:
            if self.dx * self.width / p.imagewidth <= pixel_m * 1.0001:
                best = p
        return best

    def read(self, page, x_min, y_min, x_max, y_max, threads=6):
        """the part of `page` covering [x_min, x_max] x [y_min, y_max] (file units) ->
        (array float64 with NaN for nodata/outside, x0, y0, dx, dy of that array)"""
        sx, sy = self.width / page.imagewidth, self.height / page.imagelength
        dx, dy = self.dx * sx, self.dy * sy
        c0 = max(0, int(math.floor((x_min - self.x0) / dx)) - 2)
        c1 = min(page.imagewidth, int(math.ceil((x_max - self.x0) / dx)) + 2)
        r0 = max(0, int(math.floor((self.y0 - y_max) / dy)) - 2)
        r1 = min(page.imagelength, int(math.ceil((self.y0 - y_min) / dy)) + 2)
        if c1 <= c0 or r1 <= r0:
            return None
        out = np.full((r1 - r0, c1 - c0), np.nan, np.float64)
        if page.is_tiled:
            tw, th = page.tilewidth, page.tilelength
        else:
            tw, th = page.imagewidth, page.rowsperstrip or page.imagelength
        across = (page.imagewidth + tw - 1) // tw
        offsets, counts = page.dataoffsets, page.databytecounts
        jobs = []
        for tr in range(r0 // th, (r1 - 1) // th + 1):
            for tc in range(c0 // tw, (c1 - 1) // tw + 1):
                k = tr * across + tc
                if k < len(offsets) and counts[k]:
                    jobs.append((tr, tc, int(offsets[k]), int(counts[k])))
        fetch = self._fetch or (lambda a, b: self._local(a, b))
        # neighbouring tiles (or strips) in the file go in one request: a row of tiles, many one-row strips
        jobs.sort(key=lambda j: j[2])
        groups = []
        for j in jobs:
            if groups and 0 <= j[2] - groups[-1][1] <= GAP and j[2] + j[3] - groups[-1][0] <= GROUP:
                groups[-1][1] = max(groups[-1][1], j[2] + j[3])
                groups[-1][2].append(j)
            else:
                groups.append([j[2], j[2] + j[3], [j]])

        def place(job, raw):
            tr, tc, off, n = job
            rows = th if page.is_tiled else min(th, page.imagelength - tr * th)
            t = decode_tile(raw, page, (rows, tw), self.byteorder, page.is_tiled).astype(np.float64)
            if self.nodata is not None:
                t[t == self.nodata] = np.nan
            t[~np.isfinite(t) | (t < -1e4) | (t > 1e5)] = np.nan
            ya, xa = tr * th, tc * tw
            ys, ye = max(ya, r0), min(ya + rows, r1, page.imagelength)
            xs, xe = max(xa, c0), min(xa + tw, c1, page.imagewidth)
            if ye > ys and xe > xs:
                out[ys - r0:ye - r0, xs - c0:xe - c0] = t[ys - ya:ye - ya, xs - xa:xe - xa]

        def one(group):
            start, end, members = group
            self.cancel.check()
            buf = fetch(start, end - 1)
            for job in members:
                place(job, buf[job[2] - start:job[2] - start + job[3]])

        if len(groups) > 1:
            with ThreadPoolExecutor(threads) as ex:
                list(ex.map(one, groups))
        else:
            for g in groups:
                one(g)
        return out, self.x0 + c0 * dx, self.y0 - r0 * dy, dx, dy

    def _local(self, a, b):
        with self.tif.filehandle.lock:
            self.tif.filehandle.seek(a)
            return self.tif.filehandle.read(b - a + 1)


def open_url(url, cancel=net.NEVER, size=None):
    """a remote COG; size: its length in bytes when known (else asked with a 1-byte range request)"""
    if size is None:
        size = net.content_length(url, cancel=cancel)
    fetch = lambda a, b: net.get(url, timeout=90, retries=3, cancel=cancel, byte_range=(a, b))  # noqa: E731
    return Cog(RangeFile(url, size, cancel, fetch), cancel, fetch)


def open_bytes(data):
    """a GeoTIFF in memory (its tiles are sliced out of the bytes: safe from several threads at once)"""
    import io
    data = bytes(data)
    return Cog(io.BytesIO(data), fetch=lambda a, b: data[a:b + 1])
