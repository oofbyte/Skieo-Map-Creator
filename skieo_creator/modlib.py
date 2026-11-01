"""Mod library: mods the player can switch on and off from the app. Two kinds:

- 'lua': an in-game UE4SS Lua mod. data/mods/<folder> is copied to Binaries/Win64/ue4ss/Mods/<folder> and enabled
  with a "<folder> : 1" line in ue4ss/Mods/mods.txt. UE4SS itself (MIT, https://github.com/UE4SS-RE/RE-UE4SS) is
  installed from data/ue4ss only when the game doesn't have it yet; a marker file records that the app put it there,
  and it is removed again when the last of the app's Lua mods is switched off. A UE4SS the player installed is never
  touched, except for our own mod folders and their mods.txt lines.
- 'pak': cooked content (the R2 lifts). Its zip ships inside the app (data/lifts) and its .pak/.ucas/.utoc go into
  Content/Paks/~mods. Switching off deletes them.

The game must be closed (it locks dwmapi.dll and the paks)."""
import shutil
import zipfile
from pathlib import Path

from . import paths

MODS = {
    'weather': dict(kind='lua', folder='SkiWeather', name='Weather Controls', key='F9',
                    about='Live weather panel in game: force sunny / snowing / blizzard, and turn snowfall, storms and '
                          'temperature up or down. Fixes resorts getting buried in snow.'),
    'r2six': dict(kind='pak', name='Doppelmayr D-Line R2 Six', pak='ZZZ_DoppelmayrR2Six_P',
                  zip='DLine_R2_Six_LiftMod.zip',
                  about='New lift in the build menu: D-Line R2 glass station, six-seat bubble chairs, Doppelmayr '
                        'towers. Unlocked from level 1.'),
    'r2gondola': dict(kind='pak', name='Doppelmayr D-Line R2 Gondola', pak='ZZZ_DoppelmayrR2Gondola_P',
                      zip='DLine_R2_Gondola_LiftMod.zip',
                      about='New lift in the build menu: D-Line R2 gondola station with Omega V cabins. '
                            'Unlocked from level 1.'),
}
PAK_EXTS = ('.pak', '.ucas', '.utoc')
OWN_LIFT_MOD = 'ZZZ_LiftMod_P.pak'          # the mod author's all-in-one lift pack already contains both R2 lifts
MARKER = 'installed-by-skieo-map-creator.txt'


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
    return status(game_folder)
