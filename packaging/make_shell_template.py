r"""Authors only (needs the mod kit cook): turn the cooked snow shell template into skieo_creator/data/shell_template.*

Input: the mod kit cook of /Game/Meshes/SnowShell/Map_template_00000_real/SM_SnowShell_0_0 (Scripts/import_plain_shells.py:
grid chunk 0_0 of Aspen Snowmass, LODs every 1/2/4/12 vertices, no Lumen cards) and, for the self test, the same chunk
from different heights (/Game/Meshes/SnowShellTemplate/Var). Finds every field shells.py rewrites, writes the template
and checks that rebuilding the Var chunk from the template reproduces the editor's cook.

Set first:
  SKIEO_MODKIT_COOKED      the mod kit's ...\Saved\Cooked\Windows\SkiResortSimulator\Content\Meshes
  SKIEO_UE58_TOOLS         folder with ue58_to_57.py (cooked UE 5.8 header -> the game's 5.7 layout)
  SKIEO_SHELL_VAR_HEIGHTS  the .npy landscape heights the Var chunk was cooked from
"""
import json
import os
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.environ.get('SKIEO_UE58_TOOLS', ''))
import ue58_to_57 as conv  # noqa: E402

from skieo_creator import shells  # noqa: E402

COOKED = Path(os.environ.get('SKIEO_MODKIT_COOKED', 'cooked-meshes-not-set'))
PKG = '/Game/Meshes/SnowShell/Map_template_00000_real/SM_SnowShell_0_0'
FRAME = (-450000.0, -450000.0, 80766.6982024598, 446.42857142857144, 446.42857142857144, 315.4949148533586)  # Snowmass
LODS = [(64009, 127008, 1), (16129, 31752, 2), (4096, 7938, 4), (484, 882, 12)]
GUIDS = [22, 393]                      # 16 bytes that differ between two imports of the same file
EXT_Z = 3100.0                         # positive_bounds_extension z (r.Nanite.MaxWPOExtent)


def find_all(b, pat):
    out, k = [], b.find(pat)
    while k >= 0:
        out.append(k)
        k = b.find(pat, k + 1)
    return out


def main():
    ua = conv.convert((COOKED / 'SnowShell/Map_template_00000_real/SM_SnowShell_0_0.uasset').read_bytes())
    ue = (COOKED / 'SnowShell/Map_template_00000_real/SM_SnowShell_0_0.uexp').read_bytes()
    lods = []
    for n, tris, step in LODS:
        m = find_all(ue, struct.pack('<4i', 12, n, 12, n))
        assert len(m) == 1, (n, m)
        pos = m[0] + 16
        q = ue.find(struct.pack('<2i', 1, n), pos + n * 12)      # vertex buffer header: NumTexCoords, NumVertices, flags
        assert 0 <= q - (pos + n * 12) < 32, (n, q - (pos + n * 12))
        tan = ue.find(struct.pack('<2i', 8, n), q) + 8
        assert 0 < tan - q < 40
        assert struct.unpack_from('<2i', ue, tan - 8) == (8, n)
        uv = tan + n * 8 + 8
        assert struct.unpack_from('<2i', ue, uv - 8) == (8, n)
        ib = ue.find(struct.pack('<2i', 1, tris * 6), uv + n * 8)
        assert 0 < ib - (uv + n * 8) < 64, ib
        P = np.frombuffer(ue, '<f4', n * 3, pos).reshape(-1, 3).astype(np.float64)
        mn, mx = P.min(0), P.max(0)
        c = (mn + mx) / 2
        bpos = pos - 90
        got = struct.unpack_from('<7d', ue, bpos)
        assert abs(got[0] - c[0]) < 0.01 and abs(got[2] - c[2]) < 0.01, (n, got, c)
        f32 = got[6] == float(np.float32(got[6]))
        lods.append(dict(n=n, tris=tris, step=step, pos=pos, tan=tan, uv=uv, idx=ib + 8, bounds=bpos, bounds_f32=f32))
    P0 = np.frombuffer(ue, '<f4', LODS[0][0] * 3, lods[0]['pos']).reshape(-1, 3).astype(np.float64)
    mn, mx = P0.min(0), P0.max(0)
    c, e = (mn + mx) / 2, (mx - mn) / 2
    render = [k for k in find_all(ue, struct.pack('<d', c[0])) if k != lods[0]['bounds']
              and abs(struct.unpack_from('<d', ue, k + 16)[0] - c[2]) < 0.01 and abs(struct.unpack_from('<d', ue, k + 40)[0] - e[2]) < 0.01]
    ext = [k for k in find_all(ue, struct.pack('<d', c[0])) if abs(struct.unpack_from('<d', ue, k + 16)[0] - (c[2] + EXT_Z / 2)) < 0.01]
    assert len(render) == 1 and len(ext) == 1, (render, ext)
    # UV density: the cooked value(s) and my estimate of the template, to carry Unreal's own estimator over
    tpl = shells.Template(ua, ue, dict(package=PKG, lods=lods, frame=dict(lx=FRAME[0], ly=FRAME[1], sx=FRAME[3], sy=FRAME[4]),
                                       render_bounds=render, ext_bounds=ext, ext_z=EXT_Z, uv_density=[], uv_density_k=[], guids=[]))
    zc = np.zeros((shells.G, shells.G))
    zc[:shells.CH + 1, :shells.CH + 1] = 0                       # only the chunk's vertices are used
    lod = tpl.lods[0]
    zc[lod['row'], lod['col']] = P0[:, 2] - shells.LIFT_CM
    mine = tpl._uv_density(lod, lod['faces'], 0, 0, FRAME, zc)
    dens = [o for o in range(0, len(ue) - 4) if 9000 < struct.unpack_from('<f', ue, o)[0] < 12000
            and abs(struct.unpack_from('<f', ue, o)[0] / mine - 1) < 0.01 and not any(l['pos'] <= o < l['idx'] + l['tris'] * 6 for l in lods)]
    k = [struct.unpack_from('<f', ue, o)[0] / mine for o in dens]
    print('uv density: mine %.2f, cooked %s at %s' % (mine, [round(mine * x, 2) for x in k], dens))
    info = dict(package=PKG, lods=lods, frame=dict(lx=FRAME[0], ly=FRAME[1], sx=FRAME[3], sy=FRAME[4]), render_bounds=render,
                ext_bounds=ext, ext_z=EXT_Z, uv_density=dens, uv_density_k=k, guids=GUIDS)
    out = ROOT / 'data'
    out.mkdir(exist_ok=True)
    (out / 'shell_template.uasset').write_bytes(ua)
    (out / 'shell_template.uexp').write_bytes(ue)
    (out / 'shell_template.json').write_text(json.dumps(info, indent=1), encoding='utf-8')
    print('template written: %d + %d bytes, lods %s' % (len(ua), len(ue), [l['n'] for l in lods]))
    selftest(info, ua, ue)


def selftest(info, ua, ue):
    """rebuild the Var cook (same chunk, other heights) from the template"""
    Hv = np.load(os.environ['SKIEO_SHELL_VAR_HEIGHTS'])
    want = (COOKED / 'SnowShellTemplate/Var/SM_SnowShell_0_0.uexp').read_bytes()
    tpl = shells.Template(ua, ue, info)
    got_ua, got = tpl.chunk(Hv, FRAME, 0, 0, 'Map_template_00000_real')
    assert len(got) == len(want)
    a, b = np.frombuffer(got, np.uint8).copy(), np.frombuffer(want, np.uint8)
    for lod in info['lods']:
        n = lod['n']
        pg = np.frombuffer(got, '<f4', n * 3, lod['pos']).reshape(-1, 3)
        pw = np.frombuffer(want, '<f4', n * 3, lod['pos']).reshape(-1, 3)
        ug = np.frombuffer(got, '<f4', n * 2, lod['uv'])
        uw = np.frombuffer(want, '<f4', n * 2, lod['uv'])
        tg = np.frombuffer(got, np.int8, n * 8, lod['tan']).astype(int)
        tw = np.frombuffer(want, np.int8, n * 8, lod['tan']).astype(int)
        print('LOD n=%5d: pos max err %.4f cm, uv max err %.2e, tangent bytes max diff %d (mean %.2f)' % (
            n, np.abs(pg - pw).max(), np.abs(ug - uw).max(), np.abs(tg - tw).max(), np.abs(tg - tw).mean()))
        a[lod['pos']:lod['pos'] + n * 12] = b[lod['pos']:lod['pos'] + n * 12]       # judged above
        a[lod['uv']:lod['uv'] + n * 8] = b[lod['uv']:lod['uv'] + n * 8]
        a[lod['tan']:lod['tan'] + n * 8] = b[lod['tan']:lod['tan'] + n * 8]
    for off in info['guids']:
        a[off:off + 16] = b[off:off + 16]
    for lod in info['lods']:
        g7, w7 = struct.unpack_from('<7d', got, lod['bounds']), struct.unpack_from('<7d', want, lod['bounds'])
        print('   bounds LOD n=%5d max err %.4f' % (lod['n'], max(abs(x - y) for x, y in zip(g7, w7))))
        a[lod['bounds']:lod['bounds'] + 56] = b[lod['bounds']:lod['bounds'] + 56]
    for off in info['render_bounds'] + info['ext_bounds']:
        g7, w7 = struct.unpack_from('<7d', got, off), struct.unpack_from('<7d', want, off)
        print('   bounds @%d max err %.4f' % (off, max(abs(x - y) for x, y in zip(g7, w7))))
        a[off:off + 56] = b[off:off + 56]
    for off in info['uv_density']:
        print('   uv density @%d: %.2f vs cooked %.2f' % (off, struct.unpack_from('<f', got, off)[0], struct.unpack_from('<f', want, off)[0]))
        a[off:off + 4] = b[off:off + 4]
    rest = np.nonzero(a != b)[0]
    print('other differing bytes: %d %s' % (len(rest), rest[:20]))


if __name__ == '__main__':
    main()
