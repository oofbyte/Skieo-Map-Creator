"""Read and write the game's GVAS saves (UE 5.4+ property tags with a type-name tree).

Every tag keeps its raw value bytes, so untouched parts are written back exactly; values that the reader can't decode
(some FText histories) are carried as raw bytes. New or edited values are serialized from Python values.
A tag: FString name, type tree (FString, int32 n, n child trees), int32 size, uint8 flags (0x01 array index,
0x02 property guid, 0x04 extensions, 0x08 binary/native struct, 0x10 bool true), the optional extras, the value."""
import copy
import struct
import uuid

NATIVE = {  # binary-serialized structs: name -> (format, field names)
    'Vector': ('<3d', 'XYZ'), 'Vector2D': ('<2d', 'XY'), 'Vector4': ('<4d', 'XYZW'), 'Rotator': ('<3d', ('Pitch', 'Yaw', 'Roll')),
    'Quat': ('<4d', 'XYZW'), 'LinearColor': ('<4f', 'RGBA'), 'Color': ('<4B', 'BGRA'), 'IntPoint': ('<2i', 'XY'),
    'IntVector': ('<3i', 'XYZ'), 'DateTime': ('<q', ('Ticks',)), 'Timespan': ('<q', ('Ticks',)),
    'Box': ('<6dB', ('MinX', 'MinY', 'MinZ', 'MaxX', 'MaxY', 'MaxZ', 'IsValid')),
    'Box2D': ('<4dB', ('MinX', 'MinY', 'MaxX', 'MaxY', 'IsValid')),
}
INTS = {'IntProperty': '<i', 'Int64Property': '<q', 'UInt32Property': '<I', 'UInt64Property': '<Q', 'Int16Property': '<h',
        'UInt16Property': '<H', 'Int8Property': '<b', 'FloatProperty': '<f', 'DoubleProperty': '<d'}


class NulStr(str):
    """an empty string stored as a lone terminator (length 1), not as length 0"""


class Raw:
    """bytes we keep as they are"""

    def __init__(self, b, why=''):
        self.b, self.why = bytes(b), why

    def __repr__(self):
        return '<raw %d bytes %s>' % (len(self.b), self.why)


class Tag:
    def __init__(self, name, tree, flags, extra, value, raw):
        self.name, self.tree, self.flags, self.extra, self.value, self.raw = name, tree, flags, extra, value, raw

    def __repr__(self):
        return '<%s %s>' % (self.name, type_str(self.tree))


# ------------------------------------------------------------------------------------------------------------- reading
class R:
    def __init__(self, b, o=0):
        self.b, self.o = b, o

    def take(self, fmt):
        v = struct.unpack_from(fmt, self.b, self.o)
        self.o += struct.calcsize(fmt)
        return v

    def i32(self):
        return self.take('<i')[0]

    def u8(self):
        return self.take('<B')[0]

    def fstr(self):
        n = self.i32()
        if n == 0:
            return ''
        if n == 1:
            self.o += 1
            return NulStr('')
        if n > 0:
            assert n < 1 << 22, ('bad string length', n, self.o)
            s = self.b[self.o:self.o + n - 1].decode('utf-8')
            self.o += n
            return s
        n = -n
        assert n < 1 << 22, ('bad string length', -n, self.o)
        s = self.b[self.o:self.o + 2 * n - 2].decode('utf-16-le')
        self.o += 2 * n
        return s


def read_type(r):
    name = r.fstr()
    n = r.i32()
    assert 0 <= n < 16, ('bad type tree', name, n, r.o)
    return [name] + [read_type(r) for _ in range(n)]


def type_str(t):
    return t[0] + ('(' + ', '.join(type_str(k) for k in t[1:]) + ')' if len(t) > 1 else '')


def read_text(r):
    flags = r.i32()
    ht = struct.unpack_from('<b', r.b, r.o)[0]
    r.o += 1
    if ht == -1:
        has = r.i32()
        return {'flags': flags, 'history': -1, 'text': r.fstr() if has else None}
    if ht == 0:
        return {'flags': flags, 'history': 0, 'ns': r.fstr(), 'key': r.fstr(), 'text': r.fstr()}
    raise ValueError('FText history %d' % ht)


def read_value(r, t, size=None, flags=0):
    k = t[0]
    if k == 'BoolProperty':
        return bool(r.u8()) if size is None else bool(flags & 0x10)
    if k in INTS:
        return r.take(INTS[k])[0]
    if k in ('StrProperty', 'NameProperty', 'EnumProperty', 'ObjectProperty', 'ClassProperty', 'WeakObjectProperty',
             'LazyObjectProperty', 'InterfaceProperty'):
        return r.fstr()
    if k == 'ByteProperty':
        enum = len(t) > 1 and t[1][0] != 'None'
        if size is not None:
            return r.u8() if size == 1 else r.fstr()
        return r.fstr() if enum else r.u8()
    if k == 'TextProperty':
        return read_text(r)
    if k in ('SoftObjectProperty', 'SoftClassProperty'):
        return {'pkg': r.fstr(), 'asset': r.fstr(), 'sub': r.fstr()}
    if k == 'StructProperty':
        return read_struct(r, t[1][0] if len(t) > 1 else '?', size, flags)
    if k == 'ArrayProperty':
        n = r.i32()
        inner = t[1]
        if inner[0] in ('ByteProperty', 'Int8Property') and (len(inner) == 1 or inner[1][0] == 'None'):
            v = Raw(r.b[r.o - 4:r.o + n], 'bytes')             # with its count
            r.o += n
            return v
        return [read_value(r, inner) for _ in range(n)]
    if k in ('SetProperty', 'MapProperty'):
        nrem = r.i32()
        assert nrem == 0, ('removed keys in a set/map', nrem)
        n = r.i32()
        if k == 'SetProperty':
            return [read_value(r, t[1]) for _ in range(n)]
        return [(read_value(r, t[1]), read_value(r, t[2])) for _ in range(n)]
    raise ValueError('unknown property type %s' % type_str(t))


def read_struct(r, sname, size=None, flags=0):
    if sname == 'Guid':
        g = r.b[r.o:r.o + 16]
        r.o += 16
        return g.hex().upper()
    if sname in ('SoftClassPath', 'SoftObjectPath'):
        return {'pkg': r.fstr(), 'asset': r.fstr(), 'sub': r.fstr()}
    if sname in NATIVE:
        fmt, names = NATIVE[sname]
        return dict(zip(names, r.take(fmt)))
    if size is not None and flags & 0x08:
        v = Raw(r.b[r.o:r.o + size], 'binary ' + sname)
        r.o += size
        return v
    return read_props(r)


def read_tag(r):
    name = r.fstr()
    if name == 'None':
        return None
    t = read_type(r)
    size = r.i32()
    flags = r.u8()
    e0 = r.o
    if flags & 0x01:
        r.o += 4
    if flags & 0x02:
        r.o += 16
    if flags & 0x04:
        ext = r.u8()
        if ext & 0x02:
            r.o += 1
    extra = r.b[e0:r.o]
    d = r.o
    raw = r.b[d:d + size]
    try:
        v = read_value(r, t, size, flags)
        if t[0] != 'BoolProperty' and r.o != d + size:
            raise ValueError('size mismatch %d != %d' % (r.o - d, size))
    except Exception as e:                                  # undecodable: keep it as raw bytes
        v = Raw(raw, '%s: %s' % (type_str(t), e))
    r.o = d + (0 if t[0] == 'BoolProperty' else size)
    return Tag(name, t, flags, extra, v, raw)


def read_props(r):
    out = []
    while True:
        tag = read_tag(r)
        if tag is None:
            return out
        out.append(tag)


# ------------------------------------------------------------------------------------------------------------- writing
def w_fstr(s):
    if isinstance(s, NulStr):
        return struct.pack('<i', 1) + b'\0'
    if s is None or s == '':
        return struct.pack('<i', 0)
    try:
        e = s.encode('ascii')
        return struct.pack('<i', len(e) + 1) + e + b'\0'
    except UnicodeEncodeError:
        e = s.encode('utf-16-le')
        return struct.pack('<i', -(len(e) // 2 + 1)) + e + b'\0\0'


def w_type(t):
    return w_fstr(t[0]) + struct.pack('<i', len(t) - 1) + b''.join(w_type(k) for k in t[1:])


def w_text(v):
    if isinstance(v, Raw):
        return v.b
    if v['history'] == -1:
        b = struct.pack('<ib', v['flags'], -1)
        return b + (struct.pack('<i', 1) + w_fstr(v['text']) if v['text'] is not None else struct.pack('<i', 0))
    return struct.pack('<ib', v['flags'], 0) + w_fstr(v['ns']) + w_fstr(v['key']) + w_fstr(v['text'])


def w_value(t, v, top=False):
    if isinstance(v, Raw):
        return v.b
    k = t[0]
    if k == 'BoolProperty':
        return b'' if top else struct.pack('<B', 1 if v else 0)
    if k in INTS:
        return struct.pack(INTS[k], v)
    if k in ('StrProperty', 'NameProperty', 'EnumProperty', 'ObjectProperty', 'ClassProperty', 'WeakObjectProperty',
             'LazyObjectProperty', 'InterfaceProperty'):
        return w_fstr(v)
    if k == 'ByteProperty':
        return struct.pack('<B', v) if isinstance(v, int) else w_fstr(v)
    if k == 'TextProperty':
        return w_text(v)
    if k in ('SoftObjectProperty', 'SoftClassProperty'):
        return w_fstr(v['pkg']) + w_fstr(v['asset']) + w_fstr(v['sub'])
    if k == 'StructProperty':
        sname = t[1][0] if len(t) > 1 else '?'
        if sname == 'Guid':
            return bytes.fromhex(v)
        if sname in ('SoftClassPath', 'SoftObjectPath'):
            return w_fstr(v['pkg']) + w_fstr(v['asset']) + w_fstr(v['sub'])
        if sname in NATIVE:
            fmt, names = NATIVE[sname]
            return struct.pack(fmt, *[v[n] for n in names])
        return w_props(v)
    if k == 'ArrayProperty':
        return struct.pack('<i', len(v)) + b''.join(w_value(t[1], x) for x in v)
    if k == 'SetProperty':
        return struct.pack('<ii', 0, len(v)) + b''.join(w_value(t[1], x) for x in v)
    if k == 'MapProperty':
        return struct.pack('<ii', 0, len(v)) + b''.join(w_value(t[1], a) + w_value(t[2], b) for a, b in v)
    raise ValueError('cannot write %s' % type_str(t))


def w_tag(tag):
    vb = w_value(tag.tree, tag.value, top=True)
    size = 0 if tag.tree[0] == 'BoolProperty' else len(vb)
    flags = tag.flags
    if tag.tree[0] == 'BoolProperty':
        flags = (flags | 0x10) if tag.value else (flags & ~0x10)
    return w_fstr(tag.name) + w_type(tag.tree) + struct.pack('<iB', size, flags) + tag.extra + vb


def w_props(tags):
    return b''.join(w_tag(t) for t in tags) + w_fstr('None')


# ------------------------------------------------------------------------------------------------------------- helpers
def root_start(gv):
    i = gv.find(b'SlotName\0')
    assert i > 4
    return i - 4


def parse(gv):
    """GVAS bytes -> (header bytes, root tags, trailer bytes after the root 'None')"""
    s = root_start(gv)
    r = R(gv, s)
    tags = read_props(r)
    return gv[:s], tags, gv[r.o:]


def build(head, tags, tail):
    return head + w_props(tags) + tail


def get(tags, name):
    for t in tags:
        if t.name == name:
            return t
    return None


def new_guid():
    return uuid.uuid4().bytes.hex().upper()


def clone(x):
    return copy.deepcopy(x)


def plain(v):
    if isinstance(v, Tag):
        return plain(v.value)
    if isinstance(v, list):
        if v and all(isinstance(x, Tag) for x in v):
            return {t.name: plain(t) for t in v}
        return [plain(x) for x in v]
    if isinstance(v, tuple):
        return [plain(x) for x in v]
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, Raw):
        return repr(v)
    return v
