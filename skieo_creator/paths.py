"""Where the creator keeps its files, and where bundled resources live (source tree or PyInstaller folder)."""
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import system

APP_DIR = system.app_data() / 'SkiEO Map Creator'
TILES = APP_DIR / 'cache' / 'tiles'
DEMS = APP_DIR / 'cache' / 'dem'
GAMEDATA = APP_DIR / 'gamedata'
MAPS = APP_DIR / 'maps'
LOGS = APP_DIR / 'logs'
TOOLS = APP_DIR / 'tools'
WEBVIEW = APP_DIR / 'webview'
WEBVIEW_SAFE = APP_DIR / 'webview-safe'      # the browser profile of a safe start (main.watch_load)
SETTINGS = APP_DIR / 'settings.json'


def resource(*parts):
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    return base.joinpath(*parts)


def ensure_dirs():
    for d in (APP_DIR, TILES, DEMS, GAMEDATA, MAPS, LOGS, TOOLS, WEBVIEW):
        d.mkdir(parents=True, exist_ok=True)


def setup_logging():
    ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = RotatingFileHandler(LOGS / 'creator.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(name)s: %(message)s'))
    root.addHandler(h)
    if sys.stderr is not None and not getattr(sys, 'frozen', False):
        s = logging.StreamHandler()
        s.setFormatter(logging.Formatter('%(levelname)-7s %(name)s: %(message)s'))
        root.addHandler(s)


def log_tail(lines=200):
    p = LOGS / 'creator.log'
    if not p.is_file():
        return ''
    return '\n'.join(p.read_text(encoding='utf-8', errors='replace').splitlines()[-lines:])


def retoc():
    """retoc runs from the app folder, so the Oodle library it downloads on first use is kept for next time (never
    inside the macOS app bundle, which is signed)."""
    src, dst = resource('tools', system.RETOC), TOOLS / system.RETOC
    if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
        tmp = dst.with_name(dst.name + '.tmp')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, tmp)
        if not system.WINDOWS:
            os.chmod(tmp, 0o755)
        os.replace(tmp, dst)
    return dst


class Settings:
    DEFAULTS = {'game_folder': None, 'units': 'ft', 'layer': 'topo', 'view': None}

    def __init__(self):
        self.data = dict(self.DEFAULTS)
        try:
            self.data.update(json.loads(SETTINGS.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            pass

    def get(self, k):
        return self.data.get(k)

    def set(self, **kw):
        self.data.update(kw)
        atomic_write(SETTINGS, json.dumps(self.data, indent=1).encode('utf-8'))


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# work folders the app makes inside the game's folder (hard links to its containers need the same drive; .skilift_ and
# .skipkg_: the Mod Library and its packaging script of 1.8 and older, still swept)
WORK_PREFIXES = ('.skilift_', '.skipkg_', '.skimap_', '.skieo_creator_')


def remove_tree(path, wait=10.0):
    """remove a work folder, retrying while something (the virus scanner, looking at new hard links) still holds a file
    in it; True when it is gone (else sweep() removes it on a later start)"""
    path = Path(path)
    end = time.monotonic() + wait
    while True:
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        if time.monotonic() > end:
            logging.getLogger(__name__).warning('could not remove %s yet: it is removed on a later start', path)
            return False
        time.sleep(0.25)


def sweep(*folders, age=600.0):
    """remove the work folders earlier runs had to leave in these folders (older than `age` seconds)"""
    now = time.time()
    for folder in folders:
        try:
            for d in Path(folder).iterdir():
                if d.is_dir() and d.name.startswith(WORK_PREFIXES) and now - d.stat().st_mtime > age:
                    remove_tree(d, wait=0)
        except OSError:
            pass
