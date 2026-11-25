"""What differs between Windows and macOS: where the app and the game live, the game's process, starting programs.

The rest of the creator works with one "game folder", the one that holds SkiResortSimulator/Content/Paks. On Windows
that is Steam's install folder of the game (steamapps/common/skieogame); on macOS it is inside the game's app bundle
(steamapps/common/skieogame/Ski-E-O.app/Contents/UE), so every SkiResortSimulator/... path is the same on both.
"""
import os
import platform
import subprocess
import sys
from pathlib import Path

MAC = sys.platform == 'darwin'
WINDOWS = os.name == 'nt'
NAME = 'macOS' if MAC else 'Windows'

MAC_BUNDLE = 'Ski-E-O.app'
MAC_GAME = ('Contents', 'UE')                   # the game folder inside the bundle
MAC_PROCESS = 'skieogame-Mac-Shipping'          # Contents/MacOS/<this>
RETOC = 'retoc.exe' if WINDOWS else 'retoc'


def os_version():
    if MAC:
        return 'macOS %s (%s)' % (platform.mac_ver()[0] or '?', platform.machine())
    return '%s %s' % (NAME, platform.version())


def app_data():
    """where the app keeps its folder: %LOCALAPPDATA% on Windows, ~/Library/Application Support on macOS (LOCALAPPDATA
    set there too wins, for tests)"""
    if os.environ.get('LOCALAPPDATA'):
        return Path(os.environ['LOCALAPPDATA'])
    if MAC:
        return Path.home() / 'Library' / 'Application Support'
    return Path.home() / 'AppData' / 'Local'


def steam_roots():
    if MAC:
        return [Path.home() / 'Library' / 'Application Support' / 'Steam']
    roots = []
    if WINDOWS:
        import winreg
        for hive, key, val in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                               (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')):
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(Path(winreg.QueryValueEx(k, val)[0]))
            except OSError:
                pass
    roots.append(Path(r'C:\Program Files (x86)\Steam'))
    return roots


def game_folder(install_dir):
    """the game folder in Steam's install folder of the game"""
    return Path(install_dir).joinpath(MAC_BUNDLE, *MAC_GAME) if MAC else Path(install_dir)


def install_dir(game):
    """Steam's install folder of the game (steamapps/common/<installdir>) from the game folder"""
    game = Path(game)
    if MAC and game.parts[-2:] == MAC_GAME and game.parent.parent.suffix.lower() == '.app':
        return game.parent.parent.parent
    return game


def game_candidates(picked):
    """the folders a folder the player picked may mean: it, the two above it, and on macOS the game folder inside an
    app bundle (the player picks Ski-E-O.app or the folder holding it)"""
    p = Path(picked)
    out = [p, p.parent, p.parent.parent]
    if MAC:
        out = [p.joinpath(*MAC_GAME), p.joinpath(MAC_BUNDLE, *MAC_GAME)] + out
    return out


def work_root(game):
    """where work folders next to the game go: on the game's drive, so its multi-GB containers can be hard-linked
    instead of copied, but never inside the macOS app bundle (it is signed)"""
    return install_dir(game) if MAC else Path(game) / 'SkiResortSimulator'


def game_running_mac():
    try:
        return subprocess.run(['pgrep', '-x', MAC_PROCESS], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_path(target):
    """open a URL (steam://, https://) or a file with what the system opens it with"""
    if WINDOWS:
        os.startfile(target)
    else:
        subprocess.Popen(['open', str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_detached(argv, cwd, env):
    """start a program that outlives this one"""
    if WINDOWS:
        subprocess.Popen(argv, cwd=cwd, env=env, close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        subprocess.Popen(argv, cwd=cwd, env=env, close_fds=True, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
