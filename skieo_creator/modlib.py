"""Mod library: mods the player can switch on and off from the app. Two kinds:

- 'lua': an in-game UE4SS Lua mod. data/mods/<folder> is copied to Binaries/Win64/ue4ss/Mods/<folder> and enabled
  with a "<folder> : 1" line in ue4ss/Mods/mods.txt. UE4SS itself (MIT, https://github.com/UE4SS-RE/RE-UE4SS) is
  installed from data/ue4ss only when the game doesn't have it yet; a marker file records that the app put it there,
  and it is removed again when the last of the app's Lua mods is switched off. A UE4SS the player installed is never
  touched, except for our own mod folders and their mods.txt lines.
- 'pak': cooked content (the R2 lifts). Its zip ships inside the app (data/lifts) and its .pak/.ucas/.utoc go into
  Content/Paks/~mods. Switching off deletes them.

The lift packs share the build menu: each carries the same composite DT_BuildingEconomy over a copy of the game's own
building table (DT_BuildingStock_0) and one small table for its lift (lift-models/game/dt_modular.py). That copy is from
the game version the pack was built for, so after a game update it would hide the new buildings (and undo changed
ones). refresh() therefore rebuilds it from the player's own game files into STOCK_PAK, which the game mounts above
the packs (a ..._<n>_P pak gets a higher order than a plain ..._P one), and brings installed packs up to the app's.

The game must be closed (it locks dwmapi.dll and the paks)."""
import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from . import install, paths
from .engine import legacy_header as lh

log = logging.getLogger(__name__)

MODS = {
    'weather': dict(kind='lua', folder='SkiWeather', name='Weather Controls', key='F6',
                    about='Live weather panel in game: force sunny / snowing / blizzard, and turn snowfall, storms and '
                          'temperature up or down. Fixes resorts getting buried in snow.'),
    'r2six': dict(kind='pak', name='Doppelmayr D-Line R2 Six', pak='ZZZ_DoppelmayrR2Six_P',
                  zip='DLine_R2_Six_LiftMod.zip',
                  about='New lift in the build menu: D-Line R2 glass station, six-seat bubble chairs, Doppelmayr '
                        'towers. Costs and unlocks like the High-Speed Six (Elite Resort).'),
    'r2gondola': dict(kind='pak', name='Doppelmayr D-Line R2 Gondola', pak='ZZZ_DoppelmayrR2Gondola_P',
                      zip='DLine_R2_Gondola_LiftMod.zip',
                      about='New lift in the build menu: D-Line R2 gondola station with Omega V cabins. '
                            'Costs and unlocks like the Gondola (World-Class).'),
}
PAK_EXTS = ('.pak', '.ucas', '.utoc')
OWN_LIFT_MOD = 'ZZZ_LiftMod_P.pak'          # the mod author's all-in-one lift pack already contains both R2 lifts
MARKER = 'installed-by-skieo-map-creator.txt'
STOCK_PAK = 'ZZZ_LiftStock_9_P'             # the player's own building table for the lift packs (see above)
STOCK_TABLE = 'DT_BuildingStock_0'          # same length as DT_BuildingEconomy: a rename in place
STOCK_STAMP = 'skieo_lift_stock.json'       # in ~mods: the game files STOCK_PAK was made from
STOCK_FORMAT = 1


def _bin(game_folder):
    return Path(game_folder) / 'SkiResortSimulator' / 'Binaries' / 'Win64'


def _mods_dir(game_folder):
    return Path(game_folder) / 'SkiResortSimulator' / 'Content' / 'Paks' / '~mods'


def _mods_txt(b):
    return b / 'ue4ss' / 'Mods' / 'mods.txt'


def _enabled_lines(b):
    f = _mods_txt(b)
    return f.read_text(encoding='utf-8', errors='replace').splitlines() if f.is_file() else []


def _set_line(b, folder, on):
    lines = [l for l in _enabled_lines(b) if l.split(':')[0].strip() != folder]
    if on:
        lines.append('%s : 1' % folder)
    f = _mods_txt(b)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _lua_on(b, folder):
    return ((b / 'dwmapi.dll').is_file() and (b / 'ue4ss' / 'Mods' / folder / 'Scripts' / 'main.lua').is_file()
            and any(l.split(':')[0].strip() == folder and l.split(':')[-1].strip() == '1' for l in _enabled_lines(b)))


def _pak_on(game_folder, m):
    return all((_mods_dir(game_folder) / (m['pak'] + e)).is_file() for e in PAK_EXTS)


def _is_on(game_folder, m):
    return _lua_on(_bin(game_folder), m['folder']) if m['kind'] == 'lua' else _pak_on(game_folder, m)


def status(game_folder):
    b = _bin(game_folder)
    own_lifts = (_mods_dir(game_folder) / OWN_LIFT_MOD).is_file()
    mods = []
    for k, m in MODS.items():
        d = dict(id=k, name=m['name'], kind=m['kind'], about=m['about'], on=_is_on(game_folder, m),
                 key=m.get('key'), included=m['kind'] == 'pak' and own_lifts)
        mods.append(d)
    return dict(ue4ss=(b / 'dwmapi.dll').is_file(), ours=(b / 'ue4ss' / MARKER).is_file(), mods=mods)


def _set_lua(game_folder, m, on):
    b = _bin(game_folder)
    if on:
        if not (b / 'dwmapi.dll').is_file():
            src = paths.resource('data', 'ue4ss')
            shutil.copytree(src / 'ue4ss', b / 'ue4ss', dirs_exist_ok=True)
            (b / 'ue4ss' / MARKER).write_text('UE4SS was installed by Ski-E-O Map Creator and is removed when its last '
                                              'mod is switched off.\n', encoding='utf-8')
            shutil.copy2(src / 'dwmapi.dll', b / 'dwmapi.dll')
        dst = b / 'ue4ss' / 'Mods' / m['folder']
        shutil.copytree(paths.resource('data', 'mods', m['folder'], 'Scripts'), dst / 'Scripts', dirs_exist_ok=True)
        _set_line(b, m['folder'], True)
    else:
        _set_line(b, m['folder'], False)
        shutil.rmtree(b / 'ue4ss' / 'Mods' / m['folder'], ignore_errors=True)
        if (b / 'ue4ss' / MARKER).is_file() and not any(_lua_on(b, x['folder']) for x in MODS.values() if x['kind'] == 'lua'):
            (b / 'dwmapi.dll').unlink(missing_ok=True)      # UE4SS no longer loads; then remove what we installed
            shutil.rmtree(b / 'ue4ss', ignore_errors=True)


def _set_pak(game_folder, m, on):
    mods = _mods_dir(game_folder)
    if not on:
        for e in PAK_EXTS:
            (mods / (m['pak'] + e)).unlink(missing_ok=True)
        return
    with zipfile.ZipFile(paths.resource('data', 'lifts', m['zip'])) as z:
        names = {Path(n).name: n for n in z.namelist() if Path(n).suffix in PAK_EXTS}
        if sorted(names) != sorted(m['pak'] + e for e in PAK_EXTS):
            raise ValueError('%s: unexpected files in %s' % (m['name'], m['zip']))
        mods.mkdir(parents=True, exist_ok=True)
        for fn, member in names.items():
            paths.atomic_write(mods / fn, z.read(member))


def set_mod(game_folder, mod_id, on):
    m = MODS[mod_id]
    if m['kind'] == 'lua':
        _set_lua(game_folder, m, on)
    else:
        _set_pak(game_folder, m, on)
        refresh(game_folder)
    return status(game_folder)


# ---------------------------------------------------------------- lift packs vs game updates

_refreshed = set()      # states already handled in this run


def _game_stamp(game_folder):
    paks = Path(game_folder) / 'SkiResortSimulator' / 'Content' / 'Paks'
    return [STOCK_FORMAT] + sorted([f.name, f.stat().st_size, f.stat().st_mtime_ns]
                                   for f in paks.glob('SkiResortSimulator-*.utoc'))


def _state(game_folder):
    mods = _mods_dir(game_folder)
    files = [mods / (m['pak'] + '.utoc') for m in MODS.values() if m['kind'] == 'pak'] + [mods / (STOCK_PAK + '.utoc')]
    return (str(game_folder), json.dumps(_game_stamp(game_folder)),
            tuple((f.stat().st_size, f.stat().st_mtime_ns) if f.is_file() else None for f in files))


def _outdated(game_folder, m):
    with zipfile.ZipFile(paths.resource('data', 'lifts', m['zip'])) as z:
        shipped = z.read(next(n for n in z.namelist() if Path(n).name == m['pak'] + '.utoc'))
    return shipped != (_mods_dir(game_folder) / (m['pak'] + '.utoc')).read_bytes()     # the toc hashes every chunk


def _build_stock(game_folder):
    sim = Path(game_folder) / 'SkiResortSimulator'
    work = Path(tempfile.mkdtemp(prefix='.skilift_', dir=sim))
    try:
        ua, ue, so = install.extract_data_asset(sim / 'Content' / 'Paks', work, 'DT_BuildingEconomy', 'building list')
        old, new = '/Game/Data/DT_BuildingEconomy', '/Game/Data/' + STOCK_TABLE
        ua, _, _ = lh.edit(ua, rename={old: new, 'DT_BuildingEconomy': STOCK_TABLE})
        ua = lh.rename_package_string(ua, old, new)
        legacy = work / 'legacy'
        d = legacy / 'SkiResortSimulator' / 'Content' / 'Data'
        d.mkdir(parents=True)
        (d / (STOCK_TABLE + '.uasset')).write_bytes(ua)
        (d / (STOCK_TABLE + '.uexp')).write_bytes(ue)
        (legacy / 'scriptobjects.bin').write_bytes(so)
        out = work / 'out'
        out.mkdir()
        install.run_retoc(['to-zen', '--version', install.ENGINE, str(legacy), str(out / (STOCK_PAK + '.utoc'))])
        for e in PAK_EXTS:
            paths.atomic_write(_mods_dir(game_folder) / (STOCK_PAK + e), (out / (STOCK_PAK + e)).read_bytes())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def refresh(game_folder):
    """Game closed: updates switched-on mods to the app's versions and keeps STOCK_PAK in step with the game
    (made when missing or when the game files changed; removed with the last pack). Never raises: on failure the packs
    keep using their own copy, and it is tried again when the game or the packs change, or in the next run."""
    key = None
    try:
        key = _state(game_folder)
        if key in _refreshed:
            return
        b = _bin(game_folder)
        for m in MODS.values():                         # Lua mods from an older app version (the weather key was F9,
            if m['kind'] == 'lua' and _lua_on(b, m['folder']):      # the full game's Report a Bug key)
                inst = b / 'ue4ss' / 'Mods' / m['folder'] / 'Scripts' / 'main.lua'
                if inst.read_bytes() != paths.resource('data', 'mods', m['folder'], 'Scripts', 'main.lua').read_bytes():
                    log.info('%s: updating to the version in the app', m['folder'])
                    _set_lua(game_folder, m, True)
        mods = _mods_dir(game_folder)
        on = [m for m in MODS.values() if m['kind'] == 'pak' and _pak_on(game_folder, m)]
        if not on:
            for e in PAK_EXTS:
                (mods / (STOCK_PAK + e)).unlink(missing_ok=True)
            (mods / STOCK_STAMP).unlink(missing_ok=True)
        else:
            for m in on:
                if _outdated(game_folder, m):
                    log.info('%s: updating to the version in the app', m['pak'])
                    _set_pak(game_folder, m, True)
            stamp = _game_stamp(game_folder)
            try:
                made_from = json.loads((mods / STOCK_STAMP).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                made_from = None
            if made_from != stamp or not all((mods / (STOCK_PAK + e)).is_file() for e in PAK_EXTS):
                _build_stock(game_folder)
                (mods / STOCK_STAMP).write_text(json.dumps(stamp), encoding='utf-8')
                log.info('lift packs: building table rebuilt from the game files %s', stamp)
        _refreshed.add(_state(game_folder))
    except Exception as e:                              # never let this break the app
        log.warning('lift packs: could not refresh: %s', e)
        _refreshed.add(key)
