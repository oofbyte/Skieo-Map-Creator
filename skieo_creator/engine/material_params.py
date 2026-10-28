"""Scalar parameter overrides on the map's landscape material instance (the one big LandscapeMaterialInstanceConstant;
the 1024 per-component instances take it as their parent).

Cooked, unversioned property layout (worked out on the stock assets):
  * LandscapeMaterialInstanceConstant property indices = MaterialInstanceConstant's + 5 (the landscape class's own
    properties come first). Present on the donor: 15 Parent, 19 bHasStaticPermutationResource, 38, 47, 49.
    ScalarParameterValues is 28 (MIC 23; TextureParameterValues is 26 / 31, used by the per-component instances).
  * FScalarParameterValue = header 0x0700 (3 properties) + FMaterialParameterInfo (header 0x0700, FName, u8
    association, i32 index) + f32 value + FGuid ExpressionGUID (left zero, flagged in the zero mask).
Only the material instance's export grows, so the exports after it move by the same amount (they lie back to back).
"""
import struct

from . import legacy_header as lh

SCALAR_VALUES = 28
DONOR_PRESENT = [15, 19, 38, 47, 49]
GLOBAL_PARAMETER = 2


def _exports(umap):
    f, names, _, _ = lh.parse(umap)
    th = f['total_header_size'][1]
    imports = [names[struct.unpack_from('<iiiiiii', umap, f['imports_offset'][1] + i * 32)[5]]
               for i in range(f['imports_count'][1])]
    n_exp, exp_off = f['exports_count'][1], f['exports_offset'][1]
    nxt = f['cell_imports_offset'][1] if f['cell_imports_offset'][1] > 0 else f['depends_offset'][1]
    stride = (nxt - exp_off) // n_exp
    out = []
    for i in range(n_exp):
        cls, sup, tmpl, outer, nm_i, nm_n, flags, size, off = struct.unpack_from('<iiiiiiIqq', umap, exp_off + i * stride)
        out.append(dict(i=i, cls=imports[-cls - 1] if cls < 0 else None, size=size, off=off, entry=exp_off + i * stride))
    return f, names, th, out


def read_header(d):
    """unversioned header -> (present property indices, bytes used); no zero mask expected here"""
    present, idx, o = [], 0, 0
    while True:
        v = struct.unpack_from('<H', d, o)[0]
        o += 2
        if v & 0x80:
            raise ValueError('material instance header has zero-mask fragments')
        idx += v & 0x7f
        num = v >> 9
        present.extend(range(idx, idx + num))
        idx += num
        if v & 0x100:
            return present, o


def write_header(present):
    """sorted property indices -> unversioned header (fragments of skip / run, last flagged)"""
    runs, prev = [], -1
    for p in present:
        if runs and p == prev + 1:
            runs[-1][1] += 1
        else:
            runs.append([p - prev - 1, 1])
        prev = p
    out = b''
    for k, (skip, num) in enumerate(runs):
        assert skip <= 0x7f and num <= 0x7f
        out += struct.pack('<H', skip | num << 9 | (0x100 if k == len(runs) - 1 else 0))
    return out


def _scalar_value(name_index, value):
    info = struct.pack('<H', 0x0700) + struct.pack('<iiBi', name_index, 0, GLOBAL_PARAMETER, -1)
    if value == 0.0:                                    # value and GUID both zero: flagged, not written
        return struct.pack('<HB', 0x0780, 0x06) + info
    return struct.pack('<HB', 0x0780, 0x04) + info + struct.pack('<f', value)


def set_scalars(umap, uexp, values):
    """values {parameter name: float} -> (umap, uexp) with them as overrides on the landscape material instance"""
    if not values:
        return umap, uexp
    f, names, th, ex = _exports(umap)
    add = [n for n in values if n not in names]
    if add:
        umap, _, names = lh.edit(umap, add=add)
        f, names, th, ex = _exports(umap)
    mics = [e for e in ex if e['cls'] == 'LandscapeMaterialInstanceConstant' and e['size'] > 1000]
    if len(mics) != 1:
        raise ValueError('expected one landscape material instance, found %d' % len(mics))
    mic = mics[0]
    o = mic['off'] - th
    d = uexp[o:o + mic['size']]
    present, hlen = read_header(d)
    if present != DONOR_PRESENT:
        raise ValueError('landscape material instance layout changed (%s)' % present)
    parent, flag = struct.unpack_from('<iB', d, hlen)
    if parent >= 0 or flag != 1:
        raise ValueError('landscape material instance data changed')
    body = struct.pack('<i', len(values)) + b''.join(_scalar_value(names.index(n), float(v)) for n, v in values.items())
    new = write_header(sorted(present + [SCALAR_VALUES])) + d[hlen:hlen + 5] + body + d[hlen + 5:]
    delta = len(new) - len(d)
    uexp = uexp[:o] + new + uexp[o + mic['size']:]
    out = bytearray(umap)
    struct.pack_into('<q', out, mic['entry'] + 28, mic['size'] + delta)
    for e in ex:
        if e['off'] > mic['off']:
            struct.pack_into('<q', out, e['entry'] + 36, e['off'] + delta)
    pos, val, _ = f['bulk_data_start']
    struct.pack_into('<q', out, pos, val + delta)
    return bytes(out), uexp


def get_scalars(umap, uexp):
    """the overrides set_scalars wrote -> {name: value} (for checks)"""
    f, names, th, ex = _exports(umap)
    mic = [e for e in ex if e['cls'] == 'LandscapeMaterialInstanceConstant' and e['size'] > 1000][0]
    d = uexp[mic['off'] - th:mic['off'] - th + mic['size']]
    present, o = read_header(d)
    if SCALAR_VALUES not in present:
        return {}
    o += 5
    n = struct.unpack_from('<i', d, o)[0]
    o += 4
    out = {}
    for _ in range(n):
        hdr, mask = struct.unpack_from('<HB', d, o)
        o += 3
        o += 2
        ni, nn, assoc, idx = struct.unpack_from('<iiBi', d, o)
        o += 13
        v = 0.0
        if not mask & 0x02:
            v = struct.unpack_from('<f', d, o)[0]
            o += 4
        out[names[ni]] = v
    return out
