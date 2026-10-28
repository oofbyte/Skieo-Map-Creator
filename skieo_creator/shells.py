"""Snow shells without Unreal: the 64 SM_SnowShell_<i>_<j> meshes of a map, written from one cooked template.

The game draws snow depth (piles, banks, drifts) by raising these meshes with world-position offset in M_Snow, so each
must be a copy of its 252 x 252-quad piece of the terrain in world space. Stock shells are Nanite, which only the
Unreal Editor can write. The template is a plain (non-Nanite) static mesh -- the game draws M_Snow on those too --
cooked once by the creator's authors (packaging/make_shell_template.py): a 253 x 253 vertex grid with grid LODs
(every 2nd, 4th, 12th vertex), no Lumen cards, distance field or ray tracing data, full precision UVs, all buffers
inline in the .uexp. Its vertex order and index buffers depend on the grid only, so for any chunk of any map the
creator rewrites, per LOD: positions (float32 world cm), tangent frames (int8 x 4: tangent along +x, normal), UVs
(world m / 100, v flipped), the bounds (per LOD, render, extended by the WPO headroom), the UV density and the asset
GUIDs, then renames the package / object name in the header. Everything else stays byte-identical.
"""
import json
import struct
import uuid

import numpy as np

from . import paths
from .engine import legacy_header as lh

G = 2017
CH = 252
LIFT_CM = 4.0                        # the stock shells sit 3-7 cm above the landscape


class TemplateError(Exception):
    pass


class Template:
    def __init__(self, ua, ue, info):
        self.ua, self.ue, self.info = ua, ue, info
        self.pkg = info['package']                         # '/Game/Meshes/SnowShell/Map_template_00000_real/SM_SnowShell_0_0'
        self.lods = []
        for lod in info['lods']:
            n, pos, tan, uv = lod['n'], lod['pos'], lod['tan'], lod['uv']
            if (struct.unpack_from('<4i', ue, pos - 16) != (12, n, 12, n) or struct.unpack_from('<2i', ue, tan - 8) != (8, n)
                    or struct.unpack_from('<2i', ue, uv - 8) != (8, n)):
                raise TemplateError('snow shell template is damaged')
            p = np.frombuffer(ue, '<f4', n * 3, pos).reshape(-1, 3).astype(np.float64)
            fr = info['frame']
            col = np.rint((p[:, 0] - fr['lx']) / fr['sx']).astype(np.int64)
            row = np.rint((p[:, 1] - fr['ly']) / fr['sy']).astype(np.int64)
            if col.min() < 0 or col.max() > CH or row.min() < 0 or row.max() > CH:
                raise TemplateError('snow shell template grid does not match')
            tw = np.frombuffer(ue, np.uint8, n * 8, tan).reshape(-1, 8)[:, [3, 7]].copy()
            idx = np.frombuffer(ue, '<u2', lod['tris'] * 3, lod['idx']).reshape(-1, 3) if lod.get('idx') else None
            self.lods.append(dict(lod, col=col, row=row, tw=tw, faces=idx))

    @classmethod
    def load(cls):
        d = paths.resource('data')
        try:
            info = json.loads((d / 'shell_template.json').read_text(encoding='utf-8'))
            return cls((d / 'shell_template.uasset').read_bytes(), (d / 'shell_template.uexp').read_bytes(), info)
        except OSError:
            return None

    # ------------------------------------------------------------ one chunk

    def chunk(self, H, frame, i, j, mapn):
        """(uasset, uexp) of SM_SnowShell_<i>_<j> for heights H (G x G signed units) in landscape frame
        frame = (loc_x, loc_y, loc_z, scale_x, scale_y, scale_z) of the landscape root (cm, cm per vertex, cm per 128)"""
        lx, ly, lz, sx, sy, sz = frame
        ue = bytearray(self.ue)
        Hf = H.astype(np.float64)
        zc = lz + Hf * (sz / 128.0)                                       # landscape height in cm
        gy, gx = np.gradient(zc)                                          # cm per vertex step
        lod0_bounds = None
        for lod in self.lods:
            c = lod['col'] + CH * i
            r = lod['row'] + CH * j
            x = lx + c * sx
            y = ly + r * sy
            z = zc[r, c] + LIFT_CM
            P = np.stack([x, y, z], 1)
            n = lod['n']
            ue[lod['pos']:lod['pos'] + n * 12] = P.astype('<f4').tobytes()
            dzdx, dzdy = gx[r, c] / sx, gy[r, c] / sy
            t = np.stack([np.ones(n), np.zeros(n), dzdx], 1)
            t /= np.linalg.norm(t, axis=1)[:, None]
            nrm = np.stack([-dzdx, -dzdy, np.ones(n)], 1)
            nrm /= np.linalg.norm(nrm, axis=1)[:, None]
            tb = np.empty((n, 8), np.int8)
            tb[:, 0:3] = np.clip(np.rint(t * 127), -127, 127)
            tb[:, 4:7] = np.clip(np.rint(nrm * 127), -127, 127)
            tb = tb.view(np.uint8)
            tb[:, 3], tb[:, 7] = lod['tw'][:, 0], lod['tw'][:, 1]            # w: tangent sign / binormal sign as cooked
            ue[lod['tan']:lod['tan'] + n * 8] = tb.tobytes()
            uv = np.stack([x / 10000.0, 1.0 - y / 10000.0], 1)
            ue[lod['uv']:lod['uv'] + n * 8] = uv.astype('<f4').tobytes()
            b = _sphere_bounds(P)                                         # each LOD: box + farthest vertex
            struct.pack_into('<7d', ue, lod['bounds'], *b)
            if lod0_bounds is None:
                lod0_bounds = b
                faces = lod['faces']
                render = b
            else:
                render = _union(render, b)
        for off in self.info['render_bounds']:                                # render bounds: union of the LODs
            struct.pack_into('<7d', ue, off, *render)
        c, e = np.array(lod0_bounds[:3]), np.array(lod0_bounds[3:6])
        mn, mx = c - e, c + e
        mx[2] += self.info['ext_z']                                            # extended upwards by the WPO headroom
        for off in self.info['ext_bounds']:
            cc, ee = (mn + mx) / 2.0, (mx - mn) / 2.0
            struct.pack_into('<7d', ue, off, *cc, *ee, float(np.linalg.norm(ee)))
        dens = self._uv_density(self.lods[0], faces, i, j, frame, zc)
        for off, k in zip(self.info['uv_density'], self.info['uv_density_k']):
            struct.pack_into('<f', ue, off, dens * k)
        for off in self.info['guids']:
            ue[off:off + 16] = uuid.uuid4().bytes
        return self._header(i, j, mapn, len(ue)), bytes(ue)

    def _uv_density(self, lod, faces, i, j, frame, zc):
        lx, ly, lz, sx, sy, sz = frame
        c = lod['col'] + CH * i
        r = lod['row'] + CH * j
        P = np.stack([c * sx, r * sy, zc[r, c]], 1)
        a, b, cc = P[faces[:, 0]], P[faces[:, 1]], P[faces[:, 2]]
        a3 = 0.5 * np.linalg.norm(np.cross(b - a, cc - a), axis=1)
        axy = 0.5 * np.abs((b[:, 0] - a[:, 0]) * (cc[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (cc[:, 0] - a[:, 0]))
        return float(np.sqrt(a3.sum() / max(axy.sum(), 1e-9)) * 10000.0)

    def _header(self, i, j, mapn, uexp_len):
        new_pkg = '/Game/Meshes/SnowShell/%s/SM_SnowShell_%d_%d' % (mapn, i, j)
        if len(new_pkg) != len(self.pkg):
            raise TemplateError('map name %s does not fit the snow shell template' % mapn)
        old_base = self.pkg.rsplit('_', 1)[0]                              # '.../SM_SnowShell_0'
        ua = lh.edit(self.ua, rename={old_base: new_pkg.rsplit('_', 1)[0], 'SM_SnowShell_0': 'SM_SnowShell_%d' % i})[0]
        ua = lh.rename_package_string(ua, self.pkg, new_pkg)
        f, names, _, _ = lh.parse(ua)
        eo, ec = f['exports_offset'][1], f['exports_count'][1]
        nxt = f['cell_imports_offset'][1] if f['cell_imports_offset'][1] > 0 else f['depends_offset'][1]
        st = (nxt - eo) // ec
        hits = 0
        out = bytearray(ua)
        for k in range(ec):
            o = eo + k * st + 16
            ni = struct.unpack_from('<i', out, o)[0]
            if names[ni] == 'SM_SnowShell_%d' % i:
                struct.pack_into('<i', out, o + 4, j + 1)                  # FName number: SM_SnowShell_<i>_<j>
                hits += 1
        if hits != 1:
            raise TemplateError('snow shell template header layout changed')
        return bytes(out)


def _sphere_bounds(P):
    """FBoxSphereBounds from points: box centre / half extent, radius = farthest point (float32 like the cook)"""
    mn, mx = P.min(0), P.max(0)
    c, e = (mn + mx) / 2.0, (mx - mn) / 2.0
    r = float(np.float32(np.sqrt(((P - c) ** 2).sum(1)).max()))
    return (*c, *e, r)


def _union(a, b):
    """FBoxSphereBounds a + b: box union, radius covers both spheres around the new centre"""
    ca, ea, cb, eb = np.array(a[:3]), np.array(a[3:6]), np.array(b[:3]), np.array(b[3:6])
    mn, mx = np.minimum(ca - ea, cb - eb), np.maximum(ca + ea, cb + eb)
    c, e = (mn + mx) / 2.0, (mx - mn) / 2.0
    r = max(a[6] + float(np.linalg.norm(ca - c)), b[6] + float(np.linalg.norm(cb - c)))
    return (*c, *e, r)


def shell_files(H, frame, mapn, cancel=None, progress=lambda f, t: None):
    """{legacy path: bytes} of all 64 shells, or {} when the template is not available"""
    tpl = Template.load()
    if tpl is None:
        return {}
    out = {}
    k = 0
    for i in range(8):
        for j in range(8):
            if cancel is not None:
                cancel.check()
            ua, ue = tpl.chunk(H, frame, i, j, mapn)
            rel = 'SkiResortSimulator/Content/Meshes/SnowShell/%s/SM_SnowShell_%d_%d' % (mapn, i, j)
            out[rel + '.uasset'] = ua
            out[rel + '.uexp'] = ue
            k += 1
            if k % 8 == 0:
                progress(k / 64.0, 'Building snow layer (%d of 64)...' % k)
    return out
