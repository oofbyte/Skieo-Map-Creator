"""Minimal GVAS (UE5.6+ property-type-tree format) helpers: walk root properties and edit a
StrProperty / IntProperty value with the size fix-ups."""
import logging
import struct

log = logging.getLogger(__name__)


def fstr(b, o):
    n = struct.unpack_from('<i', b, o)[0]
    assert 0 < n < 4000, (n, o)
    return b[o + 4:o + 4 + n - 1].decode('utf-8'), o + 4 + n


def tree(b, o):
    name, o = fstr(b, o)
    cnt = struct.unpack_from('<i', b, o)[0]
    o += 4
    assert 0 <= cnt < 8, cnt
    kids = []
    for _ in range(cnt):
        k, o = tree(b, o)
        kids.append(k)
    return (name, kids), o


def header(b, o):
    """returns (name, type, size, data_start, size_field_pos)"""
    name, o = fstr(b, o)
    if name == 'None':
        return name, None, None, o, None
    t, o = tree(b, o)
    size = struct.unpack_from('<i', b, o)[0]
    return name, t, size, o + 5, o


def root_start(b):
    i = b.find(b'SlotName\0')
    assert i > 4
    return i - 4


def walk(b):
    o = root_start(b)
    while True:
        name, t, size, d, so = header(b, o)
        if name == 'None':
            return
        yield name, t[0], size, d, so, o
        o = d + size


def patch(b, mappath=None, terrain_generation=None):
    """Return new GVAS bytes with MapPath / TerrainGeneration replaced."""
    b = bytearray(b)
    edits = []
    for name, typ, size, d, so, start in walk(bytes(b)):
        if name == 'MapPath' and mappath is not None:
            old, end = fstr(bytes(b), d)
            assert end - d == size
            nb = struct.pack('<i', len(mappath) + 1) + mappath.encode() + b'\0'
            edits.append((d, d + size, nb, so))
            log.info('  MapPath: %r -> %r' % (old, mappath))
        if name == 'TerrainGeneration' and terrain_generation is not None:
            old = struct.unpack_from('<i', b, d)[0]
            assert size == 4
            edits.append((d, d + 4, struct.pack('<i', terrain_generation), None))
            log.info('  TerrainGeneration: %d -> %d' % (old, terrain_generation))
    for d, e, nb, so in sorted(edits, key=lambda x: -x[0]):
        b[d:e] = nb
        if so is not None:
            struct.pack_into('<i', b, so, len(nb))
    return bytes(b)
