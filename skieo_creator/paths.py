"""Where the creator keeps its files, and where bundled resources live (source tree or PyInstaller folder)."""
import json
import logging
import os
import shutil
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_DIR = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'SkiEO Map Creator'
TILES = APP_DIR / 'cache' / 'tiles'
DEMS = APP_DIR / 'cache' / 'dem'
GAMEDATA = APP_DIR / 'gamedata'
MAPS = APP_DIR / 'maps'
LOGS = APP_DIR / 'logs'
TOOLS = APP_DIR / 'tools'
WEBVIEW = APP_DIR / 'webview'
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
    """retoc.exe runs from the app folder, so the Oodle library it downloads on first use is kept for next time."""
    src, dst = resource('tools', 'retoc.exe'), TOOLS / 'retoc.exe'
    if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
        tmp = dst.with_suffix('.tmp')
        shutil.copyfile(src, tmp)
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
