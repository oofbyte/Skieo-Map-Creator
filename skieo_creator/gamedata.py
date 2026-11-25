"""What the Map Creator reads out of the player's own game with retoc -- no game map is copied or shipped.

Maps are built from the Map Creator's own template (data/map_template.zip, made from scratch in the mod kit:
mod-loader-upload/kit/maptemplate.py). From the game, on first start and after every game update, it converts these from
the game's IoStore containers to legacy assets and checks them:
  * the stock map Map_mad_river_glen_real (umap / uexp): its compiled terrain material (the one thing a map has that
    cannot be made from scratch; builder.terrain_material), and its layout, which must still be the one the template
    was made for (engine/stockcheck.py; after a game update that changed it, a Map Creator update is needed)
  * the game assets the template refers to (terrain, rock and snow materials, weather parameters, game mode, shared
    level): that they are still there
  * DA_TreeSpecies_*, DA_BiomeSettings_*: tree variants per species, species per biome
"""
import hashlib
import json
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import paths, system, trees
from .engine import legacy_header as lh
from .engine import stockcheck

log = logging.getLogger(__name__)
C = 'SkiResortSimulator/Content/'
TEMPLATE_SLUG = 'maptemplate000'
STOCK_MAP = 'Map_mad_river_glen_real'
# the game assets the template refers to: /Game/<path>.<extension>
GAME_ASSETS = ['Materials/Terrain/M_Terrain.uasset', 'Materials/Terrain/M_StriatedRock.uasset',
               'Materials/Snow/M_Snow.uasset', 'Materials/MPC_Weather.uasset', 'Core/BP_SkiResortGameMode.uasset',
               'Maps/Map_Shared.umap', 'Data/DA_BiomeSettings_Northeast.uasset']
FILTERS = ['Maps/Generated/%s.' % STOCK_MAP, 'DA_TreeSpecies_', 'DA_BiomeSettings_'] + [
    a.rsplit('.', 1)[0] + '.' for a in GAME_ASSETS if not a.startswith('Data/DA_BiomeSettings_')]
REQUIRED = [C + 'Maps/Generated/%s.%s' % (STOCK_MAP, e) for e in ('umap', 'uexp')] + [C + a for a in GAME_ASSETS] + [
    'scriptobjects.bin']
FORMAT = 3                                   # bump when the cached layout or the checks change
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
_template = None


def template_file(name):
    """a file of the map template (data/map_template.zip)"""
    with zipfile.ZipFile(paths.resource('data', 'map_template.zip')) as z:
        return z.read(name)


def template():
    """the map template's template.json: its names, tile layout and the stock map's fingerprint it was made for"""
    global _template
    if _template is None:
        _template = json.loads(template_file('template.json'))
    return _template


class GameDataError(Exception):
    pass


def run_retoc(args, cwd=None):
    exe = paths.retoc()
    r = subprocess.run([str(exe)] + [str(a) for a in args], capture_output=True, text=True, encoding='utf-8',
                       errors='replace', creationflags=NO_WINDOW, cwd=str(cwd or exe.parent))   # retoc prints UTF-8
    if r.returncode != 0:
        out = (r.stderr or r.stdout or '').strip()
        log.error('retoc %s failed: %s', args[:1], out[-2000:])
        if 'oodle' in out.lower():
            raise GameDataError('The Oodle compression library could not be downloaded. Check your internet connection '
                                'and try again.')
        raise GameDataError('Reading or writing game files failed (retoc: %s)' % out[-300:])
    return r


def game_fingerprint(game_folder):
    """changes whenever the game's base containers change (game update / verify)"""
    p = Path(game_folder) / 'SkiResortSimulator' / 'Content' / 'Paks'
    h = hashlib.sha1()
    for f in sorted(p.glob('*.utoc')):
        if f.name.startswith('global.') or f.name.startswith('SkiResortSimulator-'):
            st = f.stat()
            h.update(('%s|%d|%d' % (f.name, st.st_size, int(st.st_mtime))).encode())
    return h.hexdigest()[:16]


def _link_or_copy(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _base_inputs(game_folder, work):
    """the base containers in a folder of their own (retoc must not see ~mods); hard links when on the same drive"""
    paks = Path(game_folder) / 'SkiResortSimulator' / 'Content' / 'Paks'
    d = work / 'in'
    d.mkdir()
    n = 0
    for f in paks.iterdir():
        if f.suffix in ('.utoc', '.ucas') and (f.name.startswith('global.') or f.name.startswith('SkiResortSimulator-')):
            _link_or_copy(f, d / f.name)
            n += 1
    if n < 4:
        raise GameDataError('The game\'s data files were not found in %s.' % paks)
    return d


class GameData:
    """The game files the Map Creator reads (see the top), extracted and checked once per game version, cached in
    %LOCALAPPDATA%/SkiEO Map Creator/gamedata."""

    def __init__(self, game_folder):
        self.game_folder = Path(game_folder)
        self.fp = game_fingerprint(game_folder)
        self.dir = paths.GAMEDATA / self.fp
        self.info = None

    def ready(self):
        try:
            info = json.loads((self.dir / 'info.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        if info.get('format') != FORMAT or not all((self.dir / r).is_file() for r in REQUIRED):
            return False
        self.info = info
        return True

    def prepare(self, progress=lambda f, t: None):
        if self.ready():
            return self.info
        progress(0.05, 'Reading the game\'s map files (first start or game update)...')
        # work folder next to the game so hard links work (no copy of the multi-GB containers)
        base = system.work_root(self.game_folder)
        try:
            work = Path(tempfile.mkdtemp(prefix='.skieo_creator_', dir=base))
        except OSError:
            work = Path(tempfile.mkdtemp(prefix='skieo_creator_'))
        try:
            src = _base_inputs(self.game_folder, work)
            out = work / 'out'
            args = ['to-legacy', '--no-shaders']
            for flt in FILTERS:
                args += ['-f', flt]
            run_retoc(args + [src, out])
            progress(0.6, 'Checking the game files...')
            missing = [r for r in REQUIRED if not (out / r).is_file()]
            if missing:
                raise GameDataError('This game version is missing files the creator needs (%s). The game may have '
                                    'changed -- check for a Map Creator update.' % ', '.join(Path(m).name for m in missing))
            info = self._validate(out)
            tmp = self.dir.with_name(self.dir.name + '.tmp')
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(out, tmp)
            (tmp / 'info.json').write_text(json.dumps(info, indent=1), encoding='utf-8')
            shutil.rmtree(self.dir, ignore_errors=True)
            os.replace(tmp, self.dir)
        finally:
            paths.remove_tree(work)
        for old in paths.GAMEDATA.iterdir():                 # keep only this game version
            if old.is_dir() and old.name != self.fp:
                shutil.rmtree(old, ignore_errors=True)
        self.info = info
        progress(1.0, 'Game files ready')
        return info

    # ------------------------------------------------------------ checks

    def _validate(self, root):
        """the game's map layout against the one the template was made for, then a dry run of every writer of the
        builder, so a changed game fails here with a clear message instead of producing a broken map"""
        from . import builder
        g = root / C
        umap = (g / 'Maps/Generated' / (STOCK_MAP + '.umap')).read_bytes()
        uexp = (g / 'Maps/Generated' / (STOCK_MAP + '.uexp')).read_bytes()
        try:
            diff = stockcheck.differences(template()['stock_fingerprint'], stockcheck.fingerprint(umap, uexp))
        except (AssertionError, ValueError, KeyError, IndexError, struct.error) as e:     # a map it can't even read
            diff = ['%s: %s' % (type(e).__name__, e)]
        if diff:
            log.error('the game\'s map layout differs from the template\'s: %s', '; '.join(diff))
            raise GameDataError('This version of Ski-E-O lays its maps out differently from the version the Map '
                                'Creator was made for, so it can\'t build maps for it yet. Check for a Map Creator update. '
                                '(%s)' % '; '.join(diff[:3]))
        try:
            species = {}
            for p in sorted((g / 'Data/Trees').glob('DA_TreeSpecies_*.uasset')):
                species[p.stem.replace('DA_TreeSpecies_', '')] = trees.species_variants(p.read_bytes(), p.with_suffix('.uexp').read_bytes())
            biome_species = {}
            for p in sorted((g / 'Data').glob('DA_BiomeSettings_*.uasset')):
                _, names, _, _ = lh.parse(p.read_bytes())
                biome_species[p.stem.replace('DA_BiomeSettings_', '')] = sorted(
                    n.replace('DA_TreeSpecies_', '') for n in names if n.startswith('DA_TreeSpecies_'))
            builder.check_templates(self.__class__._files(root))
        except (AssertionError, ValueError, KeyError, IndexError, struct.error) as e:
            log.exception('game file check failed')
            raise GameDataError('This version of Ski-E-O changed its map format, so the creator can\'t build maps for '
                                'it yet. Check for a Map Creator update. (%s: %s)' % (type(e).__name__, e))
        if not species or not biome_species:
            raise GameDataError('Tree data was not found in this game version.')
        return dict(format=FORMAT, fingerprint=self.fp, species={k: [list(v) for v in vs] for k, vs in species.items()},
                    biomes=biome_species, snow_piles=self._shells_ok(root))

    @staticmethod
    def _shells_ok(root):
        """snow shells need the game's snow material where the template points (/Game/Materials/Snow/M_Snow) and a
        working template; otherwise maps are still built, with white ground only"""
        from . import shells
        import numpy as np
        if not (Path(root) / C / 'Materials/Snow/M_Snow.uasset').is_file():
            log.warning('M_Snow not found: snow piles off')
            return False
        try:
            tpl = shells.Template.load()
            if tpl is None:
                return False
            tpl.chunk(np.zeros((shells.G, shells.G), np.int64), (-300000.0, -300000.0, 30000.0, 297.6, 297.6, 200.0), 3, 5,
                      'Map_check_shells00_real')
            return True
        except Exception:
            log.exception('snow shell template check failed: snow piles off')
            return False

    def snow_piles(self):
        return bool(self.info and self.info.get('snow_piles'))

    @staticmethod
    def _files(root):
        """the template's files (data/map_template.zip) and, from the game, the stock map (its terrain material) and
        scriptobjects.bin"""
        g = Path(root) / C
        s, m = TEMPLATE_SLUG, 'Map_%s_real' % TEMPLATE_SLUG
        with zipfile.ZipFile(paths.resource('data', 'map_template.zip')) as z:
            rd = z.read
            files = dict(
                umap=rd(m + '.umap'), uexp=rd(m + '.uexp'), ubulk=rd(m + '.ubulk'), layout=template()['layout'],
                guids=template()['guids'],
                obs_ua=rd('DA_BakedObstacles_%s.uasset' % m), obs_ue=rd('DA_BakedObstacles_%s.uexp' % m),
                pv_ua=rd('DA_MapPreview_%s.uasset' % m), pv_ue=rd('DA_MapPreview_%s.uexp' % m),
                htx_ua=rd('H_%s_Out.uasset' % s), htx_ue=rd('H_%s_Out.uexp' % s),
                is_ua=rd('IS_Map_%s_tuto.uasset' % s), is_ue=rd('IS_Map_%s_tuto.uexp' % s))
        return dict(files, stock_umap=(g / 'Maps/Generated' / (STOCK_MAP + '.umap')).read_bytes(),
                    stock_uexp=(g / 'Maps/Generated' / (STOCK_MAP + '.uexp')).read_bytes(),
                    scriptobjects=(Path(root) / 'scriptobjects.bin').read_bytes())

    def files(self):
        return self._files(self.dir)

    def species(self):
        return {k: [tuple(v) for v in vs] for k, vs in self.info['species'].items()}

    def biome_species(self, biome):
        return self.info['biomes'].get(biome, [])
