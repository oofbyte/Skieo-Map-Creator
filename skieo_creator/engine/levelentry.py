"""Rebuild entries of DA_LevelRegistry (LevelDefinition, unversioned struct, 27 fields after the Oct-6 update)."""
import struct

FIELDS = [('DisplayName', 'text'), ('Description', 'text'), ('Map', 'soft'), ('BiomeSettings', 'obj'), ('bRetired', 'bool'),
          ('MapCategory', 'byte'), ('DioramaVariantMap', 'soft'), ('DioramaVariantInitialSave', 'obj'), ('DioramaTerrainGeneration', 'int'),
          ('DioramaMapPreview', 'soft'), ('TutorialInitialSave', 'obj'), ('DioramaTutorialInitialSave', 'obj'),
          ('TutorialGuestPopulationScale', 'int'), ('TutorialVariantMap', 'soft'), ('TutorialVariantInitialSave', 'obj'),
          ('Latitude', 'int'), ('Longitude', 'int'), ('PeakElevationFeet', 'int'), ('SnowfallMultiplier', 'int'), ('VerticalDropFeet', 'int'),
          ('SkiableAcres', 'int'), ('TerrainGeneration', 'int'), ('InitialSave', 'obj'), ('MapPreview', 'soft'), ('bIsTutorialMap', 'bool'),
          ('NewField25', 'int'), ('bLocked', 'bool')]


def _fstr_end(b, o):
    n = struct.unpack_from('<i', b, o)[0]
    return o + 4 + (n if n >= 0 else -2 * n)


def _ftext_end(b, o):
    o += 4
    ht = struct.unpack_from('<b', b, o)[0]
    o += 1
    if ht == 0:
        for _ in range(3):
            o = _fstr_end(b, o)
        return o
    if ht == 11:                                   # string-table entry (the game's own names since build 25996759)
        return _fstr_end(b, o + 8)                 # table id FName, key
    has = struct.unpack_from('<I', b, o)[0]
    o += 4
    return _fstr_end(b, o) if has else o


def slices(ue, o):
    """Parse the struct at offset o. Returns (mask, {field: bytes}, end)."""
    frag, = struct.unpack_from('<H', ue, o)
    assert frag >> 9 == 27 and frag & 0x100, hex(frag)
    mask, = struct.unpack_from('<I', ue, o + 2)
    p = o + 6
    data = {}
    for i, (fn, k) in enumerate(FIELDS):
        if mask & (1 << i):
            continue
        s = p
        if k == 'text':
            p = _ftext_end(ue, p)
        elif k == 'soft':
            p = _fstr_end(ue, p + 16)
        elif k in ('obj', 'int'):
            p += 4
        else:
            p += 1
        data[fn] = bytes(ue[s:p])
    return mask, data, p


def encode(data):
    """data: {field: bytes}. A field absent from data is stored as zero (mask bit set)."""
    mask = 0
    body = b''
    for i, (fn, k) in enumerate(FIELDS):
        if fn in data:
            body += data[fn]
        else:
            mask |= 1 << i
    frag = (27 << 9) | 0x100 | 0x80
    return struct.pack('<HI', frag, mask) + body
