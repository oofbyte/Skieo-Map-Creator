"""Mod library: in-game Lua mods (UE4SS) the player can switch on and off from the app.

A mod is a folder under data/mods/<folder>, copied to Binaries/Win64/ue4ss/Mods/<folder> and enabled with a
"<folder> : 1" line in ue4ss/Mods/mods.txt. UE4SS itself (MIT, https://github.com/UE4SS-RE/RE-UE4SS) is installed from
data/ue4ss only when the game doesn't have it yet; then a marker file records that the app put it there, and it is
removed again when the last of the app's mods is switched off. A UE4SS the player installed is never touched,
except for our own mod folders and their mods.txt lines. The game must be closed (it locks dwmapi.dll)."""
import shutil
from pathlib import Path

from . import paths

MODS = {
    'weather': dict(folder='SkiWeather', name='Weather Controls', key='F9',
                    about='Live weather panel in game: force sunny / snowing / blizzard, and turn snowfall, storms and '
                          'temperature up or down. Fixes resorts getting buried in snow.'),
}
MARKER = 'installed-by-skieo-map-creator.txt'


def _bin(game_folder):
    return Path(game_folder) / 'SkiResortSimulator' / 'Binaries' / 'Win64'


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


def _is_on(b, folder):
    return ((b / 'ue4ss' / 'Mods' / folder / 'Scripts' / 'main.lua').is_file()
            and any(l.split(':')[0].strip() == folder and l.split(':')[-1].strip() == '1' for l in _enabled_lines(b)))


def status(game_folder):
    b = _bin(game_folder)
    ue4ss = (b / 'dwmapi.dll').is_file()
    return dict(ue4ss=ue4ss, ours=(b / 'ue4ss' / MARKER).is_file(),
                mods=[dict(id=k, name=m['name'], key=m['key'], about=m['about'], on=ue4ss and _is_on(b, m['folder']))
                      for k, m in MODS.items()])


def set_mod(game_folder, mod_id, on):
    m = MODS[mod_id]
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
        if (b / 'ue4ss' / MARKER).is_file() and not any(_is_on(b, x['folder']) for x in MODS.values()):
            (b / 'dwmapi.dll').unlink(missing_ok=True)      # UE4SS no longer loads; then remove what we installed
            shutil.rmtree(b / 'ue4ss', ignore_errors=True)
    return status(game_folder)
