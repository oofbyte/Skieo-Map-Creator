"""The stock assets a generated map is built from, read out of the player's own game install with retoc.

Nothing from the game is shipped with the creator: on first start (and after every game update) it converts these
from the game's IoStore containers to legacy assets and checks that their formats are what the builder expects.
  * Map_mad_river_glen_real (umap / uexp / ubulk)  the donor map every generated map is a re-terrained copy of
  * DA_BakedObstacles of Mad River Glen and Sugar Bowl  forest templates (density, tree sizes, species tables)
  * DA_MapPreview, H_mad_river_glen_Out          map preview and terrain height texture templates
  * IS_Map_sugar_bowl(_real)_tutorial            starting save template
  * DA_TreeSpecies_*, DA_BiomeSettings_*         tree variants per species, species per biome
"""
import hashlib
import json
import logging
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from . import paths, trees
from .engine import legacy_header as lh

log = logging.getLogger(__name__)
C = 'SkiResortSimulator/Content/'
DONOR_MAP = 'Map_mad_river_glen_real'
FILTERS = ['Maps/Generated/Map_mad_river_glen_real', 'DA_BakedObstacles_Map_mad_river_glen_real',
           'DA_BakedObstacles_Map_sugar_bowl_real.', 'DA_MapPreview_Map_mad_river_glen_real', 'H_mad_river_glen_Out',
           'IS_Map_sugar_bowl_real_tutorial', 'IS_Map_sugar_bowl_tutorial', 'DA_TreeSpecies_', 'DA_BiomeSettings_',
           'Materials/Snow/M_Snow.']
REQUIRED = [C + 'Maps/Generated/%s.%s' % (DONOR_MAP, e) for e in ('umap', 'uexp', 'ubulk')] + [
    C + 'Generated/DA_BakedObstacles_%s.%s' % (DONOR_MAP, e) for e in ('uasset', 'uexp')] + [
    C + 'Generated/DA_BakedObstacles_Map_sugar_bowl_real.%s' % e for e in ('uasset', 'uexp')] + [
    C + 'Generated/DA_MapPreview_%s.%s' % (DONOR_MAP, e) for e in ('uasset', 'uexp')] + [
    C + 'Materials/Terrain/Generated/mad_river_glen/H_mad_river_glen_Out.%s' % e for e in ('uasset', 'uexp')] + [
    C + 'InitialSaves/IS_Map_sugar_bowl_real_tutorial.uexp', C + 'InitialSaves/IS_Map_sugar_bowl_tutorial.uasset',
    'scriptobjects.bin']
FORMAT = 2                                   # bump when the cached layout or the checks change
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


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
    """Extracted + validated donor assets for one game version, cached in %LOCALAPPDATA%/SkiEO Map Creator/gamedata."""

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
        base = self.game_folder / 'SkiResortSimulator'
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
            shutil.rmtree(work, ignore_errors=True)
        for old in paths.GAMEDATA.iterdir():                 # keep only this game version
            if old.is_dir() and old.name != self.fp:
                shutil.rmtree(old, ignore_errors=True)
        self.info = info
        progress(1.0, 'Game files ready')
        return info

    # ------------------------------------------------------------ checks

    def _validate(self, root):
        """dry-run every format assumption of the builder on the donor files, so a changed game fails here with a
        clear message instead of producing a broken map"""
        from . import builder
        try:
            g = root / C
            umap = (g / 'Maps/Generated' / (DONOR_MAP + '.umap')).read_bytes()
            uexp = (g / 'Maps/Generated' / (DONOR_MAP + '.uexp')).read_bytes()
            builder.check_donor(umap, uexp)
            mrg = trees.read_skob((g / 'Generated' / ('DA_BakedObstacles_%s.uexp' % DONOR_MAP)).read_bytes())
            sbw = trees.read_skob((g / 'Generated/DA_BakedObstacles_Map_sugar_bowl_real.uexp').read_bytes())
            trees.table_entries(mrg['table'])
            trees.table_entries(sbw['table'])
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
        g = Path(root) / C

        def rd(rel):
            return (g / rel).read_bytes()
        return dict(
            umap=rd('Maps/Generated/%s.umap' % DONOR_MAP), uexp=rd('Maps/Generated/%s.uexp' % DONOR_MAP),
            ubulk=rd('Maps/Generated/%s.ubulk' % DONOR_MAP),
            obs_ua=rd('Generated/DA_BakedObstacles_%s.uasset' % DONOR_MAP),
            obs_ue=rd('Generated/DA_BakedObstacles_%s.uexp' % DONOR_MAP),
            sb_obs_ue=rd('Generated/DA_BakedObstacles_Map_sugar_bowl_real.uexp'),
            pv_ua=rd('Generated/DA_MapPreview_%s.uasset' % DONOR_MAP), pv_ue=rd('Generated/DA_MapPreview_%s.uexp' % DONOR_MAP),
            htx_ua=rd('Materials/Terrain/Generated/mad_river_glen/H_mad_river_glen_Out.uasset'),
            htx_ue=rd('Materials/Terrain/Generated/mad_river_glen/H_mad_river_glen_Out.uexp'),
            is_ue=rd('InitialSaves/IS_Map_sugar_bowl_real_tutorial.uexp'),
            is_ua=rd('InitialSaves/IS_Map_sugar_bowl_tutorial.uasset'),
            scriptobjects=(Path(root) / 'scriptobjects.bin').read_bytes())

    def files(self):
        return self._files(self.dir)

    def species(self):
        return {k: [tuple(v) for v in vs] for k, vs in self.info['species'].items()}

    def biome_species(self, biome):
        return self.info['biomes'].get(biome, [])
