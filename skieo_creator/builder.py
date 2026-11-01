"""Build a playable map package from an elevation grid (same recipe as the hand-built custom maps).

The map is a copy of the game's Mad River Glen map with:
  * its landscape heights (16 tiles in the .ubulk, the 16 texture storage providers, component bounds, collision)
    replaced, the landscape frame (extent / vertical scale) and the edge skirt fitted to the new terrain
  * its own names: every renamed string keeps its length (slug = 14 chars like 'mad_river_glen', starting save = 26
    chars like 'IS_Map_sugar_bowl_tutorial')
  * its own 64 snow shell meshes (snow piles / drifts, see shells.py), or none (white ground only) without the template
  * its own forest, height texture, map preview and starting save (ticket office on the base area)
and is packed with retoc into ZZZ_MC_<slug>_P.pak/.ucas/.utoc plus a .skimap package for the map installer.
"""
import io
import logging
import os
import re
import secrets
import shutil
import struct
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import gamedata, shells, trees
from .engine import collision_write as cw
from .engine import component_bounds as cb
from .engine import frame as FR
from .engine import gvas_edit
from .engine import heightmap as hm
from .engine import legacy_header as lh
from .engine import map_frame as mf
from .engine import material_params as matp
from .engine import provider_write as pw
from .engine import terrain_write as tw

log = logging.getLogger(__name__)
G = 2017
C = 'SkiResortSimulator/Content/'
DONOR = 'mad_river_glen'
DONOR_MAP = 'Map_mad_river_glen_real'
IS_OLD = 'IS_Map_sugar_bowl_tutorial'
IS_KEY_OLD = b'Map_sugar_bowl_real'
TEMPLATE_KEY = 'Map_soda_springs_real'          # locked entry of the game's map list new entries are copied from
ENTRY_FORMAT = 3
# what a map is built like; maps of an older recipe show "Rebuild recommended" in My Maps
#   2 (1.1.0): terrain mips made like Unreal's (no cracks at tile borders), edges fitted to the edge wall, tree limit
#   3 (1.1.1): no trees on cliffs / bare rock (slope falloff; the steepest tree slope is spec['tree_slope'] since 1.1.4)
#   4 (1.1.2): the tile-border normals are the map's own (lines of wrong lighting along tile borders); trees stop at 33 deg
#   5 (1.1.3): rock texture 3x larger (MATERIAL_OVERRIDES): no repeating stripes on big rock faces
#   6 (1.1.5): full forest right up to the tree slope (no thinning before it)
#   7 (1.1.6): the terrain textures' grid scale is the map's own (was the donor's: wrong lighting, lines at tile borders)
RECIPE = 7
BUILD_LOCK = threading.Lock()                     # frame.py holds the map frame globally: one build at a time


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- names

def new_slug(name, taken=()):
    """14-char [a-z0-9_] id, unique among `taken`"""
    base = re.sub(r'[^a-z0-9]+', '', name.lower())[:9] or 'map'
    while True:
        s = (base + '_' + secrets.token_hex(4))[:14]
        s = s + '0' * (14 - len(s))
        if s not in taken:
            return s


class Names:
    def __init__(self, slug):
        assert len(slug) == 14 and re.fullmatch(r'[a-z0-9_]+', slug), slug
        self.slug = slug
        self.is_new = 'IS_Map_%s_tuto' % slug
        assert len(self.is_new) == len(IS_OLD)
        self.mapn = 'Map_%s_real' % slug
        self.pak = 'ZZZ_MC_%s_P' % slug
        self.p_map = '/Game/Maps/Generated/' + self.mapn
        self.p_obs = '/Game/Generated/DA_BakedObstacles_' + self.mapn
        self.p_htx = '/Game/Materials/Terrain/Generated/%s/H_%s_Out' % (slug, slug)
        self.p_pre = '/Game/Generated/DA_MapPreview_' + self.mapn
        self.p_is = '/Game/InitialSaves/' + self.is_new

    def renames(self, kind):
        m = {
            'obs': {'/Game/Generated/DA_BakedObstacles_' + DONOR_MAP: self.p_obs,
                    'DA_BakedObstacles_' + DONOR_MAP: 'DA_BakedObstacles_' + self.mapn},
            'htx': {'/Game/Materials/Terrain/Generated/%s/H_%s_Out' % (DONOR, DONOR): self.p_htx,
                    'H_%s_Out' % DONOR: 'H_%s_Out' % self.slug},
            'pre': {'/Game/Generated/DA_MapPreview_' + DONOR_MAP: self.p_pre,
                    'DA_MapPreview_' + DONOR_MAP: 'DA_MapPreview_' + self.mapn},
            'is': {'/Game/InitialSaves/' + IS_OLD: self.p_is, IS_OLD: self.is_new},
        }
        if kind == 'map':
            return dict({'/Game/Maps/Generated/' + DONOR_MAP: self.p_map, DONOR_MAP: self.mapn}, **m['obs'], **m['htx'])
        return m[kind]

    def rename(self, ua, kind, old_pkg, new_pkg):
        new, _, _ = lh.edit(ua, rename=self.renames(kind))
        return lh.rename_package_string(new, old_pkg, new_pkg)


# ---------------------------------------------------------------- terrain

class Frame:
    """landscape frame: sp = metres per vertex, sc = metres per height unit, hmin = metres at unit -32768"""

    def __init__(self, size_m, hmin, hmax):
        self.sp = size_m / (G - 1)
        self.sc = max(hmax - hmin, 1.0) / 65534.0
        self.hmin = hmin
        self.half = (G - 1) * self.sp / 2.0

    def root(self):
        e = self.half * 100.0
        return (-e, -e, 32768 * self.sc * 100.0, self.sp * 100.0, self.sp * 100.0, self.sc * 128 * 100.0, 0.0)

    def top_z_cm(self):
        return 65535 * self.sc * 100.0

    def install(self, name):
        FR.set_frame(self.sp, self.sc, name)


def heights(dem, size_m):
    """metres grid -> (signed 16-bit landscape heights (int64 grid), Frame)"""
    dem = np.asarray(dem, np.float64)
    hmin, hmax = float(dem.min()), float(dem.max())
    fr = Frame(size_m, hmin, hmax)
    H = np.clip(np.rint((dem - hmin) / fr.sc) - 32768, -32768, 32767).astype(np.int64)
    return H, fr


EDGE_BAND = 40                                    # vertices: how far in from the edge fit_edges bends the terrain


def fit_edges(H, stations, band=EDGE_BAND):
    """The map's edge wall is straight between its stations (128 per side, ~16 vertices apart: 29 m on the stock
    maps, up to 94 m on a 12 km map). Bend the outermost band of the terrain so its edge runs exactly along the wall
    top: no gaps under the edge to see through, no wall sticking up above it."""
    Hf = H.astype(np.float64)
    n = Hf.shape[0]
    pos = np.rint(np.asarray(stations, np.float64) * (n - 1))      # map_frame.snap puts the wall vertices on these
    idx = np.arange(n, dtype=np.float64)
    w = 0.5 + 0.5 * np.cos(np.pi * np.arange(band + 1) / band)      # 1 at the edge -> 0 at the band's inner side
    for side in range(4):
        A = np.rot90(Hf, side)                                     # a view: writing A writes Hf
        edge = A[0].copy()
        line = np.interp(idx, pos, np.interp(pos, idx, edge))      # the wall top, sampled where the terrain has vertices
        A[:band + 1] += w[:, None] * (line - edge)[None, :]        # zero at the corners, so the sides don't fight
    return np.clip(np.rint(Hf), -32768, 32767).astype(np.int64)


def slope_deg(H, fr):
    gy, gx = np.gradient(H.astype(np.float64) * fr.sc, fr.sp)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def base_site(H, fr, r0, c0, radius=60):
    """flattest ground (28-cell window, ~ticket office + parking) near (r0, c0); radius in cells"""
    slope = slope_deg(H, fr)
    W = 14
    best = None
    r0, c0 = int(round(r0)), int(round(c0))
    for r in range(max(W, r0 - radius), min(G - W - 1, r0 + radius) + 1, 2):
        for c in range(max(W, c0 - radius), min(G - W - 1, c0 + radius) + 1, 2):
            w = slope[r - W:r + W + 1, c - W:c + W + 1]
            m = w.mean() + 0.15 * w.max() + 0.02 * np.hypot(r - r0, c - c0)
            if best is None or m < best[0]:
                best = (m, r, c)
    return int(best[1]), int(best[2])


def suggest_base(H, fr):
    """a good default base area: flat ground low on the map, away from the edges"""
    slope = slope_deg(H, fr)
    elev = H.astype(np.float64)
    step, W = 24, 14
    best = None
    lo, hi = np.percentile(elev, [2, 98])
    for r in range(120, G - 120, step):
        for c in range(120, G - 120, step):
            w = slope[r - W:r + W + 1, c - W:c + W + 1]
            rel = (elev[r, c] - lo) / max(hi - lo, 1.0)
            m = w.mean() + 0.15 * w.max() + 14.0 * abs(rel - 0.08)
            if best is None or m < best[0]:
                best = (m, r, c)
    return base_site(H, fr, best[1], best[2], radius=30)


def noshell(umap, uexp):
    """zero the StaticMesh reference of each SM_SnowShell_* component (int32 at +23 of the export)"""
    f, names, _, _ = lh.parse(umap)
    io_, ni = f['imports_offset'][1], f['imports_count'][1]
    imps = []
    for i in range(ni):
        _, _, cnm, _, _, obj, _ = struct.unpack_from('<iiiiiii', umap, io_ + i * 32)
        imps.append((names[obj], names[cnm]))
    eo, ec = f['exports_offset'][1], f['exports_count'][1]
    nxt = f['cell_imports_offset'][1] if f['cell_imports_offset'][1] > 0 else f['depends_offset'][1]
    st = (nxt - eo) // ec
    out = bytearray(uexp)
    zeroed = 0
    for i in range(ec):
        p = eo + i * st
        n, num = struct.unpack_from('<ii', umap, p + 16)
        if not names[n].startswith('SM_SnowShell_'):
            continue
        sz, so = struct.unpack_from('<qq', umap, p + 28)
        a = so - len(umap) + 23
        v = struct.unpack_from('<i', out, a)[0]
        if not (v < 0 and imps[-v - 1][1] == 'StaticMesh' and imps[-v - 1][0].startswith('SM_SnowShell_')):
            raise ValueError('snow shell component layout changed')
        out[a:a + 4] = bytes(4)
        zeroed += 1
    if zeroed != 64:
        raise ValueError('expected 64 snow shell components, found %d' % zeroed)
    return bytes(out)


def patch_environment(umap, uexp, peak_m, base_m):
    """real peak / base elevation into the LevelEnvironmentSettings export (the game's elevation readout, lapse rate)"""
    f, names, _, _ = lh.parse(umap)
    eo, ec = f['exports_offset'][1], f['exports_count'][1]
    nxt = f['cell_imports_offset'][1] if f['cell_imports_offset'][1] > 0 else f['depends_offset'][1]
    st = (nxt - eo) // ec
    out = bytearray(uexp)
    hits = 0
    for i in range(ec):
        p = eo + i * st
        ni, nn = struct.unpack_from('<ii', umap, p + 16)
        if names[ni] == 'LevelEnvironmentSettings' and nn:
            sz, so = struct.unpack_from('<qq', umap, p + 28)
            o = so - len(umap)
            old = struct.unpack_from('<2f', out, o + 20)
            if not (50 < old[0] < 9000 and -500 < old[1] < old[0]):
                raise ValueError('environment settings layout changed (%r)' % (old,))
            struct.pack_into('<2f', out, o + 20, peak_m, base_m)
            hits += 1
    if hits != 1:
        raise ValueError('expected one environment settings export, found %d' % hits)
    return bytes(out)


def terrain_files(H, fr, gd, names, biome, peak_m, base_m, cancel, progress, with_shells=False):
    """(umap, uexp, ubulk) of the re-terrained donor map; with_shells keeps the donor's 64 snow shell components and
    points them at the map's own meshes (FNames keep a trailing _<n> as their number: '.../SM_SnowShell_<i>')"""
    ub = gd['ubulk']
    out = bytearray(ub)
    hts, nts, orig = {}, {}, {}
    N = tw.normals(H, fr.sp * 100.0, fr.sc * 100.0)       # whole map at once: tiles agree on their border normals
    k = 0
    for tx in range(4):
        for ty in range(4):
            cancel.check()
            t = tw.LAYOUT[tx][ty]
            o = hm.load_tile_stream(ub, t)
            orig[t] = tw.decode_tile(o)[0]
            Ht = H[ty * 504:ty * 504 + 505, tx * 504:tx * 504 + 505].tolist()
            hts[t] = Ht
            nts[t] = N[ty * 504:ty * 504 + 505, tx * 504:tx * 504 + 505]
            blob = tw.to_bytes(tw.tile_blob(Ht, o, nts[t]))
            off = t * tw.TILE_VALUES * 2
            out[off:off + len(blob)] = blob
            k += 1
            progress(k / 16 * 0.6, 'Writing terrain (%d of 16)...' % k)
    umap = gd['umap']
    um = names.rename(umap, 'map', '/Game/Maps/Generated/' + DONOR_MAP, names.p_map)
    if with_shells:
        um = lh.edit(um, rename={'/Game/Meshes/SnowShell/%s/SM_SnowShell_%d' % (DONOR_MAP, i):
                                 '/Game/Meshes/SnowShell/%s/SM_SnowShell_%d' % (names.mapn, i) for i in range(8)})[0]
    if biome != 'Northeast':
        um = lh.edit(um, rename={'DA_BiomeSettings_Northeast': 'DA_BiomeSettings_' + biome,
                                 '/Game/Data/DA_BiomeSettings_Northeast': '/Game/Data/DA_BiomeSettings_' + biome})[0]
    cancel.check()
    progress(0.65, 'Fitting the landscape...')
    ue = pw.patch_providers(umap, gd['uexp'] if with_shells else noshell(umap, gd['uexp']), hts, nts,
                            scale=fr.root()[3:6])
    g_old, g_new = cb.global_grid(orig, tw.LAYOUT), cb.global_grid(hts, tw.LAYOUT)
    ue = cb.patch_boxes(umap, ue, g_old, g_new)
    cancel.check()
    progress(0.8, 'Building collision...')
    ue = cw.patch_collision(umap, ue, g_old, g_new)
    fr.install(names.slug)
    ue = mf.patch_frame(umap, ue, H)
    ue = patch_environment(umap, ue, peak_m, base_m)
    um, ue = matp.set_scalars(um, ue, MATERIAL_OVERRIDES)     # last: moves the exports after it
    progress(1.0, 'Terrain done')
    return um, ue, bytes(out)


# Terrain material settings for real-world mountains. M_Terrain's scalar defaults (cooked material, game build
# 26187996): LapseRate 6.5, MW_LandscapeWorldUVSize 384, GrassTiling 16, Paintstrokes_Intensity 0.15, DirtSpecular 1,
# DirtRoughness 1, DirtTiling 16, RockMetallic 0, RockTiling 8192, ScreeTiling 16, ForestDuffTiling 4,
# DwarfShrubTiling 32, TussockTiling 32.
# RockTiling is the rock texture's size in world cm (~82 m). Stock maps only have small rock patches; on the big faces
# of real mountains that repeat lined up into diagonal stripes. 3x (tested in game against 1x, and against the paint
# strokes off) reads as natural rock.
MATERIAL_OVERRIDES = {'RockTiling': 8192.0 * 3}


# ---------------------------------------------------------------- other assets

def obstacles_assets(gd, recs, table, hdr_tail, fr):
    ua = bytearray(gd['obs_ua'])
    ue0 = gd['obs_ue']
    src = trees.read_skob(ue0)
    n = len(recs) // src['stride']
    root = fr.root()
    raw = (b'SKOB' + struct.pack('<iii', src['ver'], n, src['stride']) + struct.pack('<2d', root[0], root[1])
           + hdr_tail + table + recs)
    ue = src['head2'] + struct.pack('<i', len(raw)) + raw + struct.pack('<ii', src['ver'], n) + src['tail']
    old = len(ue0)
    pos = [i for i in range(len(ua) - 7) if struct.unpack_from('<q', ua, i)[0] == old]
    tot = [i for i in range(len(ua) - 7) if struct.unpack_from('<q', ua, i)[0] == len(ua) + old]
    if len(pos) != 1 or len(tot) != 1:
        raise ValueError('baked obstacle header layout changed')
    struct.pack_into('<q', ua, pos[0], len(ue))
    struct.pack_into('<q', ua, tot[0], len(ua) + len(ue))
    return bytes(ua), ue


def height_texture(gd, H):
    """terrain height texture: PF_R32_FLOAT (H + 32768) / 65535, or 8-bit grey on older game builds"""
    ue = bytearray(gd['htx_ue'])
    N = G * G * 4
    st = len(ue) - 24 - N
    head = bytes(ue[:st])
    if b'PF_R32_FLOAT' in head:
        f = (H.astype(np.float32) + np.float32(32768)) * np.float32(1 / 65535)
        ue[st:st + N] = np.clip(f, 0.0, 1.0).astype('<f4').tobytes()
    elif b'PF_B8G8R8A8' in head:
        t = np.clip(np.rint((H.astype(np.float64) + 32768) / 65535.0 * 255.0), 0, 255).astype(np.uint8)
        px = np.empty((G, G, 4), np.uint8)
        px[..., 0] = px[..., 1] = px[..., 2] = t
        px[..., 3] = 255
        ue[st:st + N] = px.tobytes()
    else:
        raise ValueError('terrain height texture has an unknown pixel format')
    return bytes(ue)


def render_preview(H, fr, W=1920, Hh=1080, step=6, yaw=38.0, pitch=33.0, zex=1.3):
    """diorama-style preview (terrain slab on a gradient), like the game's own map previews"""
    n = (H.shape[0] - 1) // step + 1
    Z = (H[::step, ::step][:n, :n].astype(float) + 32768) * fr.sc * zex
    ext = (H.shape[0] - 1) * fr.sp
    xs = np.linspace(-ext / 2, ext / 2, n)
    X, Y = np.meshgrid(xs, xs)
    gy, gx = np.gradient(Z, step * fr.sp)
    nz = 1 / np.sqrt(gx ** 2 + gy ** 2 + 1)
    nx, ny = -gx * nz, -gy * nz
    L = np.array([-0.55, -0.45, 0.70])
    L = L / np.linalg.norm(L)
    sh = np.clip(nx * L[0] + ny * L[1] + nz * L[2], 0, 1)
    zn = (Z - Z.min()) / max(Z.max() - Z.min(), 1e-6)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    forest, rock = np.array([46, 62, 52]), np.array([118, 112, 106])
    snow, dirt = np.array([226, 232, 240]), np.array([96, 82, 70])
    col = forest[None, None, :] * (1 - zn[..., None]) + dirt[None, None, :] * zn[..., None]
    rock_w = np.clip((slope - 28) / 14, 0, 1)[..., None]
    col = col * (1 - rock_w) + rock * rock_w
    snow_w = 0.75 * np.clip((zn - 0.9) / 0.08, 0, 1)[..., None]
    col = col * (1 - snow_w) + snow * snow_w
    col = np.clip(col * (0.35 + 0.85 * sh[..., None]), 0, 255)
    yw, pt = np.radians(yaw), np.radians(pitch)
    rx = X * np.cos(yw) - Y * np.sin(yw)
    depth = X * np.sin(yw) + Y * np.cos(yw)
    sy = depth * np.sin(pt) - Z * np.cos(pt)
    scale = 0.58 * W / (rx.max() - rx.min())
    cx = W / 2 - (rx.max() + rx.min()) / 2 * scale
    cy = Hh * 0.52 - (sy.min() + sy.max()) / 2 * scale
    px, py = rx * scale + cx, sy * scale + cy
    yy, xx = np.mgrid[0:Hh, 0:W]
    t = (0.55 * yy / Hh + 0.25 * xx / W)[..., None]
    g = np.array([112, 108, 120]) * (1 - t) + np.array([56, 70, 98]) * t
    img = Image.fromarray(g.astype(np.uint8))
    d = ImageDraw.Draw(img)
    drop = 70 * scale
    sidecol = [(40, 44, 54), (26, 30, 40), (34, 38, 48), (20, 24, 34)]
    for k, idx in enumerate(([(0, j) for j in range(n)], [(n - 1, j) for j in range(n)],
                             [(i, 0) for i in range(n)], [(i, n - 1) for i in range(n)])):
        for a in range(len(idx) - 1):
            (i0, j0), (i1, j1) = idx[a], idx[a + 1]
            d.polygon([(px[i0, j0], py[i0, j0]), (px[i1, j1], py[i1, j1]), (px[i1, j1], py[i1, j1] + drop),
                       (px[i0, j0], py[i0, j0] + drop)], fill=sidecol[k])
    order = np.dstack(np.unravel_index(np.argsort(-depth[:-1, :-1].ravel()), (n - 1, n - 1)))[0]
    for i, j in order:
        c = tuple(int(v) for v in col[i, j] * 0.9)
        d.polygon([(px[i, j], py[i, j]), (px[i, j + 1], py[i, j + 1]), (px[i + 1, j + 1], py[i + 1, j + 1]),
                   (px[i + 1, j], py[i + 1, j])], fill=c)
    return img


def preview_assets(gd, img):
    ua, ue0 = gd['pv_ua'], gd['pv_ue']
    l1 = struct.unpack_from('<i', ue0, 2)[0]
    l2 = struct.unpack_from('<i', ue0, 6 + l1)[0]
    tail = ue0[6 + l1 + 4 + l2:]
    if len(tail) != 8:
        raise ValueError('map preview layout changed')

    def jpg(im, q):
        b = io.BytesIO()
        im.convert('RGB').save(b, 'JPEG', quality=q)
        return b.getvalue()
    j2 = jpg(img.resize((1920, 1080), Image.LANCZOS), 88)
    j1 = jpg(img.resize((640, 360), Image.LANCZOS), 85)
    ue = ue0[:2] + struct.pack('<i', len(j1)) + j1 + struct.pack('<i', len(j2)) + j2 + tail
    return lh.set_export_size(ua, len(ue)), ue


def initial_save(gd, fr, terrain_gen, base_row, base_col, base_h, mappath, new_key):
    """starting save: Sugar Bowl tutorial save with this map's path, terrain seed and world bounds, the ticket office /
    camera / cleared zones moved to the base area"""
    src = gd['is_ue']
    n = struct.unpack_from('<i', src, 2)[0]
    gvas, trailer = src[6:6 + n], src[6 + n:]
    gv = bytearray(gvas_edit.patch(gvas, mappath=mappath, terrain_generation=terrain_gen))
    props = {nm: (d, size, so) for nm, typ, size, d, so, st in gvas_edit.walk(bytes(gv))}
    nx = (base_col * fr.sp - fr.half) * 100.0
    ny = (base_row * fr.sp - fr.half) * 100.0
    nz = (base_h + 32768) * fr.sc * 100.0
    d, size, so = props['Buildings']
    ox, oy, oz = struct.unpack_from('<3d', gv, d + 487)
    if (ox, oy) != (-17300.0, -700.0):
        raise ValueError('starting save layout changed (ticket office at %r)' % ((ox, oy),))
    struct.pack_into('<3d', gv, d + 487, nx, ny, nz)
    dx, dy, dz = nx - ox, ny - oy, nz - oz
    d, size, so = props['CameraLocation']
    cx, cy, cz = struct.unpack_from('<3d', gv, d)
    struct.pack_into('<3d', gv, d, cx + dx, cy + dy, cz + dz)
    d2, size2, so2 = props['TrailMapData']
    i = bytes(gv[d2:d2 + size2]).find(bytes([0x31, 0, 0, 0, 8]))
    if i <= 0:
        raise ValueError('starting save trail map layout changed')
    e = fr.half * 100.0
    struct.pack_into('<6d', gv, d2 + i + 5, -e, -e, 0.0, e, e, fr.top_z_cm())
    d, size, so = props['ClearedObstaclesByLevel']
    raw = bytearray(gv[d:d + size])
    pos = 0
    while True:
        i = raw.find(b'Center\x00', pos)
        if i < 0:
            break
        j = raw.find(b'\x10\x00\x00\x00\x08', i)
        zx, zy = struct.unpack_from('<2d', raw, j + 5)
        struct.pack_into('<2d', raw, j + 5, zx + dx, zy + dy)
        pos = i + 10
    k = raw.find(struct.pack('<i', len(IS_KEY_OLD) + 1) + IS_KEY_OLD + b'\0')
    if k <= 0:
        raise ValueError('starting save level key not found')
    nk = struct.pack('<i', len(new_key) + 1) + new_key + b'\0'
    raw[k:k + 4 + len(IS_KEY_OLD) + 1] = nk
    gv[d:d + size] = raw
    struct.pack_into('<i', gv, so, size + len(new_key) - len(IS_KEY_OLD))
    return src[:2] + struct.pack('<i', len(gv)) + bytes(gv) + trailer


# ---------------------------------------------------------------- checks run once per game version

def check_donor(umap, uexp):
    noshell(umap, uexp)
    roots = [e for e in mf.find_exports(umap, 'SceneComponent') if e[0] == 'RootComponent0']
    if len(roots) != 1 or len(mf.find_exports(umap, 'ProceduralMeshComponent')) != 1:
        raise ValueError('landscape root / skirt layout changed')
    if len(cb.component_exports(umap)) < 16 or not cw.collision_exports(umap):
        raise ValueError('landscape component layout changed')
    patch_environment(umap, uexp, 1200.0, 400.0)


def check_templates(gd):
    """dry-run the asset writers on a tiny synthetic map"""
    H = np.zeros((G, G), np.int64)
    fr = Frame(6000.0, 500.0, 1500.0)
    obstacles_assets(gd, b'', trees.encode_table([]), trees.read_skob(gd['obs_ue'])['hdr'][16:], fr)
    height_texture(gd, H)
    preview_assets(gd, Image.new('RGB', (64, 36)))
    initial_save(gd, fr, 1, 1000, 1000, 0, '/Game/Maps/Generated/Map_test_check_000_real', b'Map_test_check_000_real')
    lh.parse(gd['is_ua'])


# ---------------------------------------------------------------- the whole map

def typed(stats):
    return [[k, 'f' if isinstance(v, float) else 'i', v] for k, v in stats.items()]


def entry_spec(names, display, biome, stats):
    prev = 'DA_MapPreview_' + names.mapn
    return {'append': TEMPLATE_KEY,
            'add_names': [names.p_map, names.mapn, names.p_pre, prev, names.p_is, names.is_new],
            'initial_save': {'package': names.p_is, 'object': names.is_new},
            'set': [['Map', 'ref', names.p_map, names.mapn], ['MapPreview', 'ref', names.p_pre, prev],
                    ['TutorialInitialSave', 'initial_save'], ['bIsTutorialMap', 'hex', '01'], ['bLocked', 'remove'],
                    ['DisplayName', 'text', display], ['BiomeSettings', 'import', 'DA_BiomeSettings_' + biome]]
            + typed(stats)}


def build(spec, dem, game, out_dir, cancel, progress):
    """spec: dict(name, slug, lat, lon, size_m, biome, treeline, valley, snow, acres, base_rc, seed, tg)
    game: GameData. Returns dict(files=[...], manifest=..., preview=PIL image, stats=...)"""
    with BUILD_LOCK:
        return _build(spec, dem, game, Path(out_dir), cancel, progress)


def _build(spec, dem, game, out_dir, cancel, progress):
    t0 = time.time()
    names = Names(spec['slug'])
    gd = game.files()

    def stage(a, b):
        return lambda f, t: progress(a + (b - a) * f, t)

    H, fr = heights(dem, spec['size_m'])
    H = fit_edges(H, mf.wall_stations(gd['umap'], gd['uexp']))
    peak_m, base_m = fr.hmin + 65534 * fr.sc, fr.hmin
    row, col = base_site(H, fr, *spec['base_rc'], radius=40)
    base_h = int(H[row, col])
    cancel.check()

    progress(0.02, 'Planting trees...')
    donors = {'mrg': trees.read_skob(gd['obs_ue']), 'sb': trees.read_skob(gd['sb_obs_ue'])}
    recs, table, hdr_tail, counts = trees.forest(
        H, fr.hmin, fr.sp, fr.sc, spec['biome'], spec['treeline'], spec['valley'], donors, game.species(),
        game.biome_species(spec['biome']), spec['seed'], max_trees=int(spec.get('max_trees', trees.DEFAULT_TREES)),
        tree_slope=float(spec.get('tree_slope', trees.TREE_SLOPE)), treeline_fade=float(spec.get('treeline_fade', 0.0)))
    obs_ua, obs_ue = obstacles_assets(gd, recs, table, hdr_tail, fr)
    obs_ua = names.rename(obs_ua, 'obs', '/Game/Generated/DA_BakedObstacles_' + DONOR_MAP, names.p_obs)
    cancel.check()

    progress(0.12, 'Terrain height texture...')
    htx_ua = names.rename(gd['htx_ua'], 'htx', '/Game/Materials/Terrain/Generated/%s/H_%s_Out' % (DONOR, DONOR), names.p_htx)
    htx_ue = height_texture(gd, H)

    progress(0.15, 'Rendering the map preview...')
    img = render_preview(H, fr)
    pv_ua, pv_ue = preview_assets(gd, img)
    pv_ua = names.rename(pv_ua, 'pre', '/Game/Generated/DA_MapPreview_' + DONOR_MAP, names.p_pre)
    cancel.check()

    progress(0.25, 'Starting save...')
    is_ue = initial_save(gd, fr, spec['tg'], row, col, base_h, names.p_map, names.mapn.encode())
    is_ua = names.rename(lh.set_export_size(gd['is_ua'], len(is_ue)), 'is', '/Game/InitialSaves/' + IS_OLD, names.p_is)

    shell = {}
    if spec.get('snow_piles', True) and game.snow_piles():
        shell = shells.shell_files(H, fr.root()[:6], names.mapn, cancel, stage(0.27, 0.37))
    um, ue, ub = terrain_files(H, fr, gd, names, spec['biome'], peak_m, base_m, cancel, stage(0.37, 0.85),
                               with_shells=bool(shell))
    files = {
        C + 'Maps/Generated/%s.umap' % names.mapn: um, C + 'Maps/Generated/%s.uexp' % names.mapn: ue,
        C + 'Maps/Generated/%s.ubulk' % names.mapn: ub,
        C + 'Generated/DA_BakedObstacles_%s.uasset' % names.mapn: obs_ua,
        C + 'Generated/DA_BakedObstacles_%s.uexp' % names.mapn: obs_ue,
        C + 'Materials/Terrain/Generated/%s/H_%s_Out.uasset' % (names.slug, names.slug): htx_ua,
        C + 'Materials/Terrain/Generated/%s/H_%s_Out.uexp' % (names.slug, names.slug): htx_ue,
        C + 'Generated/DA_MapPreview_%s.uasset' % names.mapn: pv_ua,
        C + 'Generated/DA_MapPreview_%s.uexp' % names.mapn: pv_ue,
        C + 'InitialSaves/%s.uasset' % names.is_new: is_ua, C + 'InitialSaves/%s.uexp' % names.is_new: is_ue,
        'scriptobjects.bin': gd['scriptobjects'],
    }
    files.update(shell)
    cancel.check()
    progress(0.87, 'Packing the map...')
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='build_', dir=out_dir))
    try:
        legacy = work / 'legacy'
        for rel, data in files.items():
            p = legacy / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        pak_dir = work / 'pak'
        pak_dir.mkdir()
        gamedata.run_retoc(['to-zen', '--version', 'UE5_7', legacy, pak_dir / (names.pak + '.utoc')])
        produced = [names.pak + e for e in ('.pak', '.ucas', '.utoc')]
        for f in produced:
            if not (pak_dir / f).is_file():
                raise BuildError('Packing the map failed (%s missing).' % f)
            os.replace(pak_dir / f, out_dir / f)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    peak_ft = int(round(peak_m * 3.28084))
    vert_ft = int(round((peak_m - base_m) * 3.28084))
    base_lat, base_lon = spec['base_latlon']
    stats = dict(Latitude=float(base_lat), Longitude=float(base_lon), SkiableAcres=int(spec['acres']),
                 SnowfallMultiplier=float(spec['snow']), TerrainGeneration=int(spec['tg']),
                 PeakElevationFeet=peak_ft, VerticalDropFeet=vert_ft)
    log.info('built %s (%s) in %.1f s: peak %d ft, vertical %d ft, %d trees', spec['name'], names.slug, time.time() - t0,
             peak_ft, vert_ft, len(recs) // trees.STRIDE)
    progress(1.0, 'Map built')
    return dict(files=produced, entry=entry_spec(names, spec['name'], spec['biome'], stats), preview=img,
                stats=dict(peak_ft=peak_ft, vert_ft=vert_ft, base_ft=int(round(base_m * 3.28084)),
                           trees=len(recs) // trees.STRIDE, species=counts, base_rc=[row, col], snow_piles=bool(shell)))
