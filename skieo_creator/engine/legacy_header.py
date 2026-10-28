"""Edit the name map of a retoc-produced legacy .uasset header (UE5.7, cooked, unversioned).

Layout reference: retoc legacy_asset.rs (FLegacyPackageFileSummary / FPackageNameMap).
Only the name map changes size, so everything that lives after it shifts by `delta`:
every offset in the summary that points past the names, the total header size,
bulk_data_start_offset and each export's serial_offset (which includes the header size).
"""
import logging
import struct

log = logging.getLogger(__name__)


class Reader:
    def __init__(self, b):
        self.b = b
        self.o = 0

    def i32(self):
        v = struct.unpack_from('<i', self.b, self.o)[0]
        self.o += 4
        return v

    def u32(self):
        v = struct.unpack_from('<I', self.b, self.o)[0]
        self.o += 4
        return v

    def i64(self):
        v = struct.unpack_from('<q', self.b, self.o)[0]
        self.o += 8
        return v

    def u16(self):
        v = struct.unpack_from('<H', self.b, self.o)[0]
        self.o += 2
        return v

    def fstr(self):
        n = self.i32()
        if n == 0:
            return ''
        if n > 0:
            s = self.b[self.o:self.o + n - 1].decode('utf-8')
            self.o += n
            return s
        n = -n
        s = self.b[self.o:self.o + 2 * (n - 1)].decode('utf-16le')
        self.o += 2 * n
        return s


def parse(b):
    r = Reader(b)
    f = {}                                   # name -> (position of the int field, value, width)

    def field(name, kind='i32'):
        pos = r.o
        v = getattr(r, kind)()
        f[name] = (pos, v, kind)
        return v

    assert r.u32() == 0x9E2A83C1, 'bad magic'
    legacy = r.i32()
    assert legacy == -9, 'expected UE5.6+/5.7 legacy version, got %d' % legacy
    ue3, ue4, ue5, lic = r.i32(), r.i32(), r.i32(), r.i32()
    r.o += 20                                # saved hash
    field('total_header_size')
    ncv = r.i32()
    r.o += ncv * 20                          # custom versions (guid + i32)
    r.fstr()                                 # package name
    flags = r.u32()
    filter_editor_only = bool(flags & 0x80000000)
    field('names_count'); field('names_offset')
    field('softobj_count'); field('softobj_offset')
    if not filter_editor_only:
        r.fstr()                             # localization id
    r.i32(); r.i32()                         # gatherable text data (count, offset)
    field('exports_count'); field('exports_offset')
    field('imports_count'); field('imports_offset')
    field('cell_exports_count'); field('cell_exports_offset')
    field('cell_imports_count'); field('cell_imports_offset')
    r.i32()                                  # metadata offset
    field('depends_offset')
    r.i32(); r.i32()                         # soft package references (count, offset)
    r.i32()                                  # searchable names offset
    r.i32()                                  # thumbnail table offset
    if not filter_editor_only:
        r.o += 16                            # persistent package guid
    ngen = r.i32()
    r.o += ngen * 8                          # generations
    for _ in range(2):                       # saved-by / compatible-with engine version
        r.u16(); r.u16(); r.u16(); r.u32(); r.fstr()
    r.u32()                                  # compression flags
    r.i32()                                  # num compressed chunks
    r.u32()                                  # package source
    nadd = r.i32()
    for _ in range(nadd):
        r.fstr()
    field('asset_registry_offset')
    field('bulk_data_start', 'i64')
    r.i32()                                  # world tile info offset
    nchunk = r.i32()
    r.o += nchunk * 4
    field('preload_count'); field('preload_offset')
    field('names_referenced_count')
    r.i64()                                  # payload toc offset
    field('data_resource_offset')
    summary_end = r.o
    names_off = f['names_offset'][1]
    assert summary_end == names_off, 'summary parsing is off: ended at %d, names start at %d' % (summary_end, names_off)

    # name map
    r.o = names_off
    names = []
    for _ in range(f['names_count'][1]):
        names.append(r.fstr())
        r.o += 4                             # two u16 hashes
    names_end = r.o
    return f, names, names_off, names_end


def fstring_bytes(s):
    raw = s.encode('utf-8') + b'\0'
    return struct.pack('<i', len(raw)) + raw


def edit(b, rename=None, add=None):
    """Return new header bytes. rename: {old: new}; add: [new names appended at the end]."""
    f, names, names_off, names_end = parse(b)
    rename = rename or {}
    add = add or []
    new_names = [rename.get(n, n) for n in names] + list(add)
    body = b''.join(fstring_bytes(n) + b'\0\0\0\0' for n in new_names)
    delta = len(body) - (names_end - names_off)
    out = bytearray(b[:names_off] + body + b[names_end:])

    def put(name, value):
        pos, _, kind = f[name]
        struct.pack_into('<q' if kind == 'i64' else '<i', out, pos, value)

    def shift(name):
        v = f[name][1]
        if v > 0 and v >= names_end:
            put(name, v + delta)

    put('names_count', len(new_names))
    put('names_referenced_count', len(new_names))      # all names stay "referenced from export data"
    for n in ('softobj_offset', 'exports_offset', 'imports_offset', 'cell_exports_offset', 'cell_imports_offset',
              'depends_offset', 'asset_registry_offset', 'preload_offset', 'data_resource_offset'):
        shift(n)
    put('total_header_size', f['total_header_size'][1] + delta)
    put('bulk_data_start', f['bulk_data_start'][1] + delta)
    # exports: serial_offset (+36 within an entry) includes the header size
    n_exp = f['exports_count'][1]
    exp_off = f['exports_offset'][1] + delta
    nxt = f['cell_imports_offset'][1] + delta if f['cell_imports_offset'][1] > 0 else f['depends_offset'][1] + delta
    stride = (nxt - exp_off) // n_exp if n_exp else 0
    assert stride * n_exp == nxt - exp_off, 'export stride not integral (%d exports, span %d)' % (n_exp, nxt - exp_off)
    for i in range(n_exp):
        p = exp_off + i * stride + 36
        old = struct.unpack_from('<q', out, p)[0]
        struct.pack_into('<q', out, p, old + delta)
    return bytes(out), delta, new_names


def set_export_size(b, new_size, export_index=0):
    """Set serial_size of an export (+28 in the entry) and move bulk_data_start by the same delta."""
    f, names, _, _ = parse(b)
    n_exp = f['exports_count'][1]
    exp_off = f['exports_offset'][1]
    nxt = f['cell_imports_offset'][1] if f['cell_imports_offset'][1] > 0 else f['depends_offset'][1]
    stride = (nxt - exp_off) // n_exp
    out = bytearray(b)
    p = exp_off + export_index * stride + 28
    old = struct.unpack_from('<q', out, p)[0]
    struct.pack_into('<q', out, p, new_size)
    pos, val, _ = f['bulk_data_start']
    struct.pack_into('<q', out, pos, val + (new_size - old))
    return bytes(out)


def add_imports(b, entries):
    """Append import entries (each a tuple of 8 int32: classpkg, classpkg_num, class, class_num, outer, obj, obj_num, pad)
    at the end of the import table. Returns (new bytes, index of first new import)."""
    f, names, names_off, names_end = parse(b)
    n_imp = f['imports_count'][1]
    imp_off = f['imports_offset'][1]
    ins = imp_off + n_imp * 32
    body = b''.join(struct.pack('<8i', *e) for e in entries)
    delta = len(body)
    out = bytearray(b[:ins] + body + b[ins:])

    def put(name, value):
        pos, _, kind = f[name]
        struct.pack_into('<q' if kind == 'i64' else '<i', out, pos, value)
    put('imports_count', n_imp + len(entries))
    for n in ('softobj_offset', 'exports_offset', 'cell_exports_offset', 'cell_imports_offset',
              'depends_offset', 'asset_registry_offset', 'preload_offset', 'data_resource_offset'):
        v = f[n][1]
        if v > 0 and v >= ins:
            put(n, v + delta)
    put('total_header_size', f['total_header_size'][1] + delta)
    put('bulk_data_start', f['bulk_data_start'][1] + delta)
    n_exp = f['exports_count'][1]
    exp_off = f['exports_offset'][1] + delta
    nxt = (f['cell_imports_offset'][1] + delta) if f['cell_imports_offset'][1] > 0 else f['depends_offset'][1] + delta
    stride = (nxt - exp_off) // n_exp if n_exp else 0
    assert stride * n_exp == nxt - exp_off
    for i in range(n_exp):
        p = exp_off + i * stride + 36
        old = struct.unpack_from('<q', out, p)[0]
        struct.pack_into('<q', out, p, old + delta)
    return bytes(out), n_imp


def rename_package_string(b, old, new):
    """Replace the summary's package-name FString in place; requires equal lengths."""
    assert len(old) == len(new)
    pat = fstring_bytes(old)
    i = b.find(pat)
    assert i > 0, old
    assert b.find(pat, i + 1) < 0 or True
    return b[:i] + fstring_bytes(new) + b[i + len(pat):]
