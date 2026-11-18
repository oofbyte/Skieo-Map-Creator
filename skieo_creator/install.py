"""Install / uninstall map packages and rebuild the game's map list -- the Ski-E-O Map Installer's core, shared so
maps made with the creator also show up in (and can be removed with) SkiEO-Map-Installer.exe, and the other way round.

A .skimap file is a zip with manifest.json plus the map's IoStore files (.pak/.ucas/.utoc). The game's map menu
comes from one asset (DA_LevelRegistry), so every installed map needs an entry in it. Instead of shipping a fixed
registry (which would list maps the player doesn't have and crash when they are picked), the installer extracts the
player's own original registry from the game, applies the entry of every installed map, and packs the result as
ZZZ_Registry_P in Content/Paks/~mods.
"""
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from . import paths, system
from .engine import legacy_header as lh
from .engine import levelentry as le

log = logging.getLogger(__name__)

APP_ID = '4493130'          # Ski-E-O! Ski Resort Tycoon
DEMO_APP_ID = '5012760'     # not supported: the demo's game files are encrypted since its November update
FORMAT = 1
FORMATS = (1, 2, 3, 4)  # 2 = the package adds a new map entry ('append'), which installers before 2 can't do;
                        # 3 = entries are found by map key (registry layout of game build 25996759 and later);
                        # 4 = the entry may name no starting save (1.10: maps start empty like the game's own)
REGISTRY_PAK = 'ZZZ_Registry_P'
REGISTRY_VERSION = 2   # what custom_entry() makes of the entries; 2 (1.7.5): never a tutorial map
STATE_NAME = 'skimap_installer.json'
ENGINE = 'UE5_7'
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


class InstallError(Exception):
    pass


# ---------------------------------------------------------------- game discovery

def _vdf_paths(text):
    out = []
    for line in text.splitlines():
        parts = [p for p in line.strip().split('"') if p.strip()]
        if len(parts) == 2 and parts[0] == 'path':
            out.append(Path(parts[1].replace('\\\\', '\\')))
    return out


def steam_roots():
    return system.steam_roots()


def _acf(path):
    """key -> value of a Steam app manifest ({} when there is none)"""
    info = {}
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return info
    for line in text.splitlines():
        parts = [p for p in line.strip().split('"') if p.strip()]
        if len(parts) == 2:
            info.setdefault(parts[0], parts[1])
    return info


def find_game():
    """Returns (game folder, Steam build id) or (None, None)."""
    seen = set()
    for root in steam_roots():
        vdf = root / 'steamapps' / 'libraryfolders.vdf'
        found = _vdf_paths(vdf.read_text(encoding='utf-8', errors='replace')) if vdf.is_file() else []
        for lib in found + [root]:
            if str(lib).lower() in seen:
                continue
            seen.add(str(lib).lower())
            info = _acf(lib / 'steamapps' / ('appmanifest_%s.acf' % APP_ID))
            game = system.game_folder(lib / 'steamapps' / 'common' / info.get('installdir', ''))
            if info.get('installdir') and (game / 'SkiResortSimulator' / 'Content' / 'Paks').is_dir():
                return game, info.get('buildid')
    return None, None


def manifest(game, app=APP_ID):
    """Steam's app manifest of `app` when it is the game in this folder, else {}"""
    folder = system.install_dir(Path(game).resolve())
    info = _acf(folder.parent.parent / ('appmanifest_%s.acf' % app))
    return info if info.get('installdir', '').lower() == folder.name.lower() else {}


def is_demo(game):
    return bool(manifest(game, DEMO_APP_ID)) or system.install_dir(game).name.lower() == 'skieogame_demo'


def is_valid_game(game):
    p = Path(game) / 'SkiResortSimulator' / 'Content' / 'Paks'
    return (p / 'global.utoc').is_file() and any(p.glob('SkiResortSimulator-*.utoc')) and not is_demo(game)


GAME_EXE = 'skieogame.exe'


def _process_names():
    """lower-case exe names of all running processes, straight from Windows (a Toolhelp snapshot)"""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)                                  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        names = set()
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            names.add(e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        return names
    finally:
        k32.CloseHandle(snap)


def game_running():
    """Is Ski-E-O running? Asks Windows directly: parsing tasklist's text broke on non-English Windows (its output is
    in the console code page, e.g. a German u-umlaut is byte 0x81 there, which the locale codec can't decode)."""
    if system.MAC:
        return system.game_running_mac()
    if os.name != 'nt':
        return False
    try:
        return GAME_EXE in _process_names()
    except OSError:
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ' + GAME_EXE, '/NH'], capture_output=True,
                           creationflags=NO_WINDOW)
        return GAME_EXE in (r.stdout or b'').decode('ascii', errors='replace').lower()


# ---------------------------------------------------------------- tree limit

# The game drops ALL of a map's trees when it has more than 1.5 million: its baked-obstacle reader does
# ensure(RecordCount <= UObstacleDataManager::MaxBakedTrees) and rejects the whole blob. That compare is the only use
# of the constant (the tree arrays follow the record count). Versions 1.3 to 1.9 raised its immediate in
# skieogame.exe, but a changed exe makes the native helpers of Z's Mod Loader refuse to run (asset packs then miss the
# build menu; Catwalk Trails, Staff Trail Routing switch off). The Oof's Tree Limit mod (Z's Mod Manager) raises it in
# the running game's memory instead, and the app puts the game's own value back where it changed it.
GAME_TREE_LIMIT = 1500000
OLD_TREE_PATCH = 20000000       # what versions 1.3 to 1.9 wrote over it (always this value)
TREE_MOD = 'OofsTreeLimit'      # the mod's folder in <game>/mods
TREES_UNCAPPED = not system.MAC      # Oof's Tree Limit can raise the game's limit (a Windows mod: Experimental mode's
                                    # many trees show in the game); on macOS maps stay within 1.5M
# cmp r32, imm32 ; jbe ok (short in the demo, near in the full game) ... lea rdx, [the "... over the bake cap" message]
_TREE_CHECK = re.compile(rb'\x81[\xF8-\xFF](....)(?:\x76.|\x0F\x86....)', re.S)
_LEA_RDX = re.compile(rb'\x48\x8D\x15(....)', re.S)
_tree_limit_seen = {}
_tree_check_seen = {}           # per exe version: (offset, limit) found, so a running game's exe is read only once


def _tree_check_at(data):
    """file offset of the tree limit (the compare's imm32), or None when the game code changed"""
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    nsec, opt = struct.unpack_from('<H', data, pe + 6)[0], struct.unpack_from('<H', data, pe + 20)[0]
    secs = [struct.unpack_from('<IIII', data, pe + 24 + opt + 40 * i + 8) for i in range(nsec)]   # vsize, va, rawsize, raw

    def rva(o):
        return next(va + o - raw for _, va, size, raw in secs if raw <= o < raw + size)
    msg = data.find('over the bake cap'.encode('utf-16-le'))
    if msg < 0:
        return None
    msg = rva(msg)
    hits = [m for m in _TREE_CHECK.finditer(data)
            if any(0 <= msg - (rva(l.end()) + struct.unpack_from('<i', data, l.start(1))[0]) < 128
                   for l in _LEA_RDX.finditer(data, m.end(), m.end() + 0x60))]
    return hits[0].start(1) if len(hits) == 1 else None


def restore_tree_limit(folder):
    """Puts the game's own tree limit back (game closed) where an older version raised it in skieogame.exe: only that
    value, so the exe is Steam's again. Checked whenever the app looks at the game; cached per exe version. Returns the
    limit in the exe (None: unknown, logged once). Windows only (the macOS game was never patched).
    """
    if system.MAC:
        return None
    exe = Path(folder) / 'SkiResortSimulator' / 'Binaries' / 'Win64' / GAME_EXE
    key = (str(exe),)
    try:
        st = exe.stat()
        key = (str(exe), st.st_size, st.st_mtime_ns)
        if key in _tree_limit_seen:
            return _tree_limit_seen[key]
        if key not in _tree_check_seen:
            data = exe.read_bytes()
            at = _tree_check_at(data)
            _tree_check_seen[key] = (at, at and struct.unpack_from('<I', data, at)[0])
        at, limit = _tree_check_seen[key]
        if not at:
            log.info('tree limit check not found in %s (a game update changed that code)', exe)
        elif limit == OLD_TREE_PATCH:
            if game_running():
                return limit                                    # the exe is locked; tried again next time
            with open(exe, 'r+b') as f:
                f.seek(at)
                f.write(struct.pack('<I', GAME_TREE_LIMIT))
            log.info('game tree limit put back from %d to %d in %s', limit, GAME_TREE_LIMIT, exe)
            limit = GAME_TREE_LIMIT
            st = exe.stat()
            key = (str(exe), st.st_size, st.st_mtime_ns)
    except (OSError, struct.error, StopIteration) as e:                # never let this break the app
        if key in _tree_limit_seen:
            return None
        log.warning('could not check the game tree limit: %s', e)
        limit = None
    _tree_limit_seen[key] = limit
    return limit


def tree_mod_on(folder):
    """Is Oof's Tree Limit installed and turned on in Z's Mod Manager (its folder in <game>/mods, with enabled.txt)?"""
    mod = Path(folder) / 'mods' / TREE_MOD
    return (mod / 'enabled.txt').is_file() and (mod / 'Native' / 'TreeLimit.dll').is_file()


# ---------------------------------------------------------------- state

class Game:
    def __init__(self, folder, build=None):
        self.folder = Path(folder)
        self.build = build
        self.paks = self.folder / 'SkiResortSimulator' / 'Content' / 'Paks'
        self.mods = self.paks / '~mods'
        self.state_file = self.mods / STATE_NAME

    def state(self):
        if self.state_file.is_file():
            return json.loads(self.state_file.read_text(encoding='utf-8'))
        return {'format': FORMAT, 'maps': {}}

    def save_state(self, st):
        self.mods.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(st, indent=1), encoding='utf-8')
        os.replace(tmp, self.state_file)

    def installed(self):
        return self.state()['maps']

    # ------------------------------------------------------------ packages

    @staticmethod
    def read_package(path):
        with zipfile.ZipFile(path) as z:
            m = json.loads(z.read('manifest.json').decode('utf-8'))
            if m.get('format') not in FORMATS:
                raise InstallError('%s was made for a different installer version (format %s).' % (Path(path).name, m.get('format')))
            names = set(z.namelist())
            for f in m['files']:
                if f not in names or '/' in f or '\\' in f or not f.startswith('ZZZ_') \
                        or Path(f).suffix.lower() not in ('.pak', '.ucas', '.utoc'):
                    raise InstallError('%s is damaged (bad file list).' % Path(path).name)
        return m

    def check_ready(self):
        if game_running():
            raise InstallError('Close Ski-E-O first, then try again.')
        if not is_valid_game(self.folder):
            raise InstallError('The game files were not found in %s.' % self.folder)

    def install(self, package, progress=lambda msg: None):
        self.check_ready()
        restore_tree_limit(self.folder)
        m = self.read_package(package)
        st = self.state()
        for mid, other in st['maps'].items():
            if mid != m['id'] and other['entry'].get('slot') and other['entry'].get('slot') == m['entry'].get('slot'):
                raise InstallError('"%s" uses the same menu slot as "%s". Uninstall "%s" first.'
                                   % (m['name'], other['name'], other['name']))
            if mid != m['id'] and set(other['files']) & set(m['files']):
                raise InstallError('"%s" has files that belong to "%s".' % (m['name'], other['name']))
        old = st['maps'].get(m['id'])
        mine = set(old['files']) if old else set()      # never replace another mod's file (the app's own map paks
        taken = [f for f in m['files'] if (self.mods / f).exists() and f not in mine and not f.startswith('ZZZ_MC_')]
        if taken:                                       # are ZZZ_MC_*: a rebuilt map may replace them)
            raise InstallError('"%s" would replace %s in the game\'s mods folder, which another mod put there.'
                               % (m['name'], taken[0]))
        self.mods.mkdir(parents=True, exist_ok=True)
        progress('Copying %s...' % m['name'])
        stage = self.mods / '.skimap_incoming'          # checked here before it goes into ~mods
        paths.remove_tree(stage)
        stage.mkdir()
        try:
            with zipfile.ZipFile(package) as z:
                for f in m['files']:
                    with z.open(f) as src, open(stage / f, 'wb') as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
            self.only_a_map(m, stage)
            for f in m['files']:
                os.replace(stage / f, self.mods / f)
        finally:
            paths.remove_tree(stage)
        if old:
            for f in set(old['files']) - set(m['files']):
                (self.mods / f).unlink(missing_ok=True)
        st['maps'][m['id']] = {k: m[k] for k in ('id', 'name', 'version', 'order', 'files', 'entry', 'game_build')}
        self.save_state(st)
        progress('Updating the map list...')
        self.rebuild_registry(st)
        return m

    def only_a_map(self, m, folder):
        """a map package holds a map: not the assets all mods share (build menu, snow caps, map list), nor the game's
        own outside the map folders, so installing a map never changes the game beyond the map"""
        stems = {}
        for f in m['files']:
            stems.setdefault(Path(f).stem, set()).add(Path(f).suffix.lower())
        for stem, have in stems.items():               # IoStore containers only: the .pak beside one holds no files
            if have != {'.pak', '.ucas', '.utoc'} or pak_files(Path(folder) / (stem + '.pak')) != 0:
                raise InstallError('"%s" is not only a map: %s is not a map container. It was not installed.'
                                   % (m['name'], stem))
            bad = not_a_map(assets(Path(folder) / (stem + '.utoc')), base_assets(self.paks))
            if bad:
                raise InstallError('"%s" is not only a map: it replaces %s. It was not installed.' % (m['name'], bad[0]))

    def uninstall(self, map_id, progress=lambda msg: None):
        self.check_ready()
        st = self.state()
        m = st['maps'].pop(map_id, None)
        if m is None:
            return
        progress('Removing %s...' % m['name'])
        for f in m['files']:
            (self.mods / f).unlink(missing_ok=True)
        self.save_state(st)
        progress('Updating the map list...')
        self.rebuild_registry(st)

    # ------------------------------------------------------------ map list

    def rebuild_registry(self, st=None):
        st = st or self.state()
        maps = sorted(st['maps'].values(), key=lambda m: (m['order'], m['id']))
        reg = [self.mods / (REGISTRY_PAK + ext) for ext in ('.pak', '.ucas', '.utoc')]
        if not maps:
            for f in reg:
                f.unlink(missing_ok=True)
            return
        work = Path(tempfile.mkdtemp(prefix='.skimap_', dir=system.work_root(self.folder)))
        try:
            ua, ue, so = extract_registry(self.paks, work)
            for m in maps:
                ua, ue = apply_entry(ua, ue, custom_entry(m['entry']))
            legacy = work / 'legacy'
            d = legacy / 'SkiResortSimulator' / 'Content' / 'Data'
            d.mkdir(parents=True)
            (d / 'DA_LevelRegistry.uasset').write_bytes(ua)
            (d / 'DA_LevelRegistry.uexp').write_bytes(ue)
            (legacy / 'scriptobjects.bin').write_bytes(so)
            out = work / 'out'
            out.mkdir()
            run_retoc(['to-zen', '--version', ENGINE, str(legacy), str(out / (REGISTRY_PAK + '.utoc'))])
            for f in reg:
                src = out / f.name
                if not src.is_file():
                    raise InstallError('Building the map list failed (%s missing).' % f.name)
            for f in reg:
                shutil.copyfile(out / f.name, str(f) + '.part')
            for f in reg:
                os.replace(str(f) + '.part', f)
        finally:
            paths.remove_tree(work)
        st['registry'] = REGISTRY_VERSION
        self.save_state(st)

    def registry_stale(self):
        """the installed map list was built by an older version (custom_entry changed since): rebuild it once"""
        st = self.state()
        return bool(st['maps']) and st.get('registry', 1) < REGISTRY_VERSION


def run_retoc(args):
    exe = paths.retoc()
    r = subprocess.run([str(exe)] + args, capture_output=True, text=True, encoding='utf-8', errors='replace',
                       creationflags=NO_WINDOW, cwd=str(exe.parent))     # retoc prints UTF-8 (paths can have accents)
    if r.returncode != 0:
        raise InstallError('retoc failed: ' + (r.stderr or r.stdout)[-400:])
    return r


def extract_registry(paks, work):
    """The game's original DA_LevelRegistry (uasset, uexp) and script objects, read from its own containers."""
    return extract_data_asset(paks, work, 'DA_LevelRegistry', 'map list')


def extract_data_asset(paks, work, name, what, sub='Data'):
    """(uasset, uexp, script objects) of Content/<sub>/<name> as the game itself has it (not a mod's copy).
    retoc wants the base container and global.utoc in one folder without ~mods, so hard links are made next to them."""
    src = work / 'in'
    src.mkdir()
    for f in Path(paks).iterdir():
        if f.suffix in ('.utoc', '.ucas') and (f.name.startswith('global.') or f.name.startswith('SkiResortSimulator-')):
            try:
                os.link(f, src / f.name)
            except OSError as e:
                raise InstallError('Could not read the game files (%s).' % e)
    out = work / 'base'
    run_retoc(['to-legacy', '--no-shaders', '--filter', name, str(src), str(out)])
    d = out / 'SkiResortSimulator' / 'Content' / sub
    try:
        return ((d / (name + '.uasset')).read_bytes(), (d / (name + '.uexp')).read_bytes(),
                (out / 'scriptobjects.bin').read_bytes())
    except OSError:
        raise InstallError('The game\'s %s could not be read -- the game may have changed.' % what)


# ---------------------------------------------------------------- what a map package may hold

# the assets every mod shares, one owner each (the game's, or the one mod meant to own it): the build menu's table (and
# the old Mod Library's renamed copy of it), the snow cap list and the map list
SHARED = {'/Game/Data/DT_BuildingEconomy', '/Game/Data/DT_BuildingStock_0', '/Game/Data/SnowCaps/DA_SnowCapRegistry',
          '/Game/Data/DA_LevelRegistry'}
# where a map package's own packages live (builder.Names; the Map Installer's packages take over levels the same way)
MAP_DIRS = ('/Game/Maps/', '/Game/Generated/', '/Game/InitialSaves/', '/Game/Materials/Terrain/', '/Game/Meshes/SnowShell/')


def package_path(p):
    """'../../../SkiResortSimulator/Content/Data/X.uasset' -> '/Game/Data/X' (plugins: '/<Plugin>/...')"""
    p = p.replace(chr(92), '/')
    m = re.search(r'SkiResortSimulator/Content/(.+)\.(?:uasset|umap)$', p)
    if m:
        return '/Game/' + m.group(1)
    m = re.search(r'/Plugins/(?:.*/)?([^/]+)/Content/(.+)\.(?:uasset|umap)$', p)
    return '/%s/%s' % (m.group(1), m.group(2)) if m else None


def assets(utoc):
    """the packages in an IoStore container (its .ucas must be beside it)"""
    out = set()
    for line in run_retoc(['list', '--path', str(utoc)]).stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[2] == 'ExportBundleData':
            p = package_path(parts[3].strip())
            if p:
                out.add(p)
    return out


_base = {}


def base_assets(paks):
    """the game's own packages (cached per game build)"""
    files = sorted(Path(paks).glob('SkiResortSimulator-*.utoc'))
    key = tuple((f.name, f.stat().st_size, f.stat().st_mtime_ns) for f in files)
    if key not in _base:
        _base.clear()
        _base[key] = set().union(*(assets(f) for f in files)) if files else set()
    return _base[key]


def pak_files(path):
    """how many files a .pak holds outside the IoStore container beside it (retoc's and the editor's .pak beside a
    container hold none: a 347-byte index); None if it can't be read (not a pak, or its index encrypted)"""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 1024))
        tail = f.read()
        i = tail.rfind(struct.pack('<I', 0x5A6F12E1))       # the footer's magic
        if i < 1 or tail[i - 1] or i + 24 > len(tail):      # (byte before: the index is encrypted)
            return None
        off, length = struct.unpack_from('<qq', tail, i + 8)
        if not (0 <= off and off + length <= size and length >= 8):
            return None
        f.seek(off)
        idx = f.read(min(length, 4096))
    n = struct.unpack_from('<i', idx, 0)[0]                 # the mount point (FString), then the file count
    at = 4 + (n if n >= 0 else -2 * n)
    return struct.unpack_from('<i', idx, at)[0] if at + 4 <= len(idx) else None


def not_a_map(have, base):
    """packages a map package must not have: the shared ones, and the game's own outside the map folders"""
    return sorted(p for p in have if p in SHARED or (p in base and not p.startswith(MAP_DIRS)))


# ---------------------------------------------------------------- registry entries

def fstr(s):
    """an Unreal FString: ASCII as length + bytes; anything else as UTF-16 with a negative length (writing UTF-8 bytes
    under a character count corrupted the map list for names like 'Zürs')"""
    if s.isascii():
        return struct.pack('<i', len(s) + 1) + s.encode('ascii') + b'\0'
    u = s.encode('utf-16-le') + b'\0\0'
    return struct.pack('<i', -(len(u) // 2)) + u


def entry_at(ue, display):
    """start of the entry whose DisplayName is the string-table key `display` (since build 25996759 every entry's name
    is MountainNames:<map key>, e.g. Map_whistler) or, in older registries / our own entries, the literal string"""
    b = bytes(ue)
    needle = fstr(display)
    at = b.find(needle)
    while at > 0:
        if at >= 19 and b[at - 9] == 11:                     # string-table text: flags(4) history 11 table(8) key
            return at - 8 - 1 - 4 - 6
        if b[at - 5] == 0xFF:                                # culture-invariant text: flags(4) history -1 has(4) string
            return at - (4 + 1 + 4) - 6
        at = b.find(needle, at + 1)
    raise InstallError('Menu slot "%s" not found in the game\'s map list -- the game may have changed.' % display)


def all_entries(ue):
    """[(start, fields, end)] of every map entry; the array count sits right after the 2-byte property header."""
    count = struct.unpack_from('<i', ue, 2)[0]
    o, out = 6, []
    for _ in range(count):
        _, ent, e = le.slices(ue, o)
        out.append((o, ent, e))
        o = e
    return out


def import_index(ua, object_name):
    f, names, _, _ = lh.parse(ua)
    n, off = f['imports_count'][1], f['imports_offset'][1]
    for i in range(n):
        if names[struct.unpack_from('<8i', ua, off + 32 * i)[5]] == object_name:
            return -(i + 1)
    raise InstallError('"%s" not found in the game\'s map list -- the game may have changed.' % object_name)


def custom_entry(spec):
    """What every custom map's entry is, also ones packaged by older versions:
    - real size only: no Diorama variant (Map size in Advanced options), which would be the copied entry's -- another
      resort's tabletop map;
    - never a tutorial map (bIsTutorialMap, set up to 1.7.4, from the demo days): New Game takes the first tutorial
      map of the list as THE tutorial map, so in a region without one of the game's own (Canadian Rockies, Scandes ...)
      a custom map became it, offered the tutorial on every start, and its "Skip Tutorial" starts that map without its
      starting save: no real lifts and trails."""
    keep = [i for i in spec['set'] if i[0] not in ('DioramaVariantMap', 'DioramaTutorialInitialSave', 'bIsTutorialMap')]
    return dict(spec, add_names=spec['add_names'] + ['None'],
                set=keep + [['DioramaVariantMap', 'ref', 'None', 'None'], ['DioramaTutorialInitialSave', 'remove'],
                            ['bIsTutorialMap', 'remove']])


def apply_entry(ua, ue, spec):
    """Point one menu slot of the registry at a custom map, or add a new entry for it.
    spec: slot (existing entry to take over) | append (existing entry to copy as a new one, placed after the last
          entry of the same biome), add_names, initial_save ({package, object} -> new imports | {copy: [slot, field]}),
          set: [[field, kind, ...]]"""
    have = set(lh.parse(ua)[1])
    ua2, _, names = lh.edit(ua, add=[n for n in dict.fromkeys(spec['add_names']) if n not in have])
    ix = names.index
    n_imp = lh.parse(ua2)[0]['imports_count'][1]
    isv = spec.get('initial_save') or {}               # (none: a map without a starting save, as the game's own)
    is_ref = None
    if 'package' in isv:
        obj_imp = (ix('/Script/SkiResortSimulator'), 0, ix('InitialSaveAsset'), 0, -(n_imp + 2), ix(isv['object']), 0, 0)
        pkg_imp = (ix('/Script/CoreUObject'), 0, ix('Package'), 0, 0, ix(isv['package']), 0, 0)
        ua3, first = lh.add_imports(ua2, [obj_imp, pkg_imp])
        assert first == n_imp
        is_ref = struct.pack('<i', -(n_imp + 1))
    elif 'copy' in isv:
        ua3 = ua2
        _, other, _ = le.slices(ue, entry_at(ue, isv['copy'][0]))
        is_ref = other[isv['copy'][1]]
    else:
        ua3 = ua2
    if 'append' in spec:
        if bytes(ue).find(fstr(dict((i[0], i[2]) for i in spec['set'] if i[1] == 'text')['DisplayName'])) > 0:
            raise InstallError('The map list already has an entry with this name.')
        _, ent, _ = le.slices(ue, entry_at(ue, spec['append']))
    else:
        s = entry_at(ue, spec['slot'])
        _, ent, e = le.slices(ue, s)
    for item in spec['set']:
        field, kind = item[0], item[1]
        if kind == 'ref':
            ent[field] = struct.pack('<5i', ix(item[2]), 0, ix(item[3]), 0, 0)
        elif kind == 'initial_save':
            if is_ref is None:
                raise InstallError('This map package names no starting save.')
            ent[field] = is_ref
        elif kind == 'hex':
            ent[field] = bytes.fromhex(item[2])
        elif kind == 'remove':
            ent.pop(field, None)
        elif kind == 'text':
            ent[field] = b'\x02\x00\x00\x00\xff\x01\x00\x00\x00' + fstr(item[2])
        elif kind in ('i', 'f'):
            ent[field] = struct.pack('<' + kind, item[2])
        elif kind == 'import':
            ent[field] = struct.pack('<i', import_index(ua3, item[2]))
        else:
            raise InstallError('Unknown entry field kind %r' % kind)
    if 'append' in spec:
        same = [end for _, other, end in all_entries(ue) if other.get('BiomeSettings') == ent.get('BiomeSettings')]
        at = same[-1] if same else len(ue) - 4
        count = struct.unpack_from('<i', ue, 2)[0]
        new_ue = ue[:2] + struct.pack('<i', count + 1) + ue[6:at] + le.encode(ent) + ue[at:]
        return lh.set_export_size(ua3, len(new_ue)), new_ue
    new_ue = ue[:s] + le.encode(ent) + ue[e:]
    return lh.set_export_size(ua3, len(new_ue)), new_ue


# ---------------------------------------------------------------- packaging (map author side)

def make_package(path, manifest, files_dir):
    manifest = dict(manifest, format=manifest.get('format') or (2 if 'append' in manifest['entry'] else 1))
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:     # maps with snow
                                                                                                  # shells are ~280 MB
        z.writestr('manifest.json', json.dumps(manifest, indent=1))
        for f in manifest['files']:
            z.write(Path(files_dir) / f, f)
