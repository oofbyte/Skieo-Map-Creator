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

from . import paths
from .engine import legacy_header as lh
from .engine import levelentry as le

log = logging.getLogger(__name__)

APP_ID = '5012760'
FORMAT = 1
FORMATS = (1, 2, 3)    # 2 = the package adds a new map entry ('append'), which installers before 2 can't do;
                       # 3 = entries are found by map key (registry layout of game build 25996759 and later)
REGISTRY_PAK = 'ZZZ_Registry_P'
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
    roots = []
    if os.name == 'nt':
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


def find_game():
    """Returns (game folder, Steam build id) or (None, None)."""
    libs = []
    for root in steam_roots():
        vdf = root / 'steamapps' / 'libraryfolders.vdf'
        if vdf.is_file():
            libs += _vdf_paths(vdf.read_text(encoding='utf-8', errors='replace'))
        libs.append(root)
    seen = set()
    for lib in libs:
        key = str(lib).lower()
        if key in seen:
            continue
        seen.add(key)
        acf = lib / 'steamapps' / ('appmanifest_%s.acf' % APP_ID)
        if not acf.is_file():
            continue
        info = {}
        for line in acf.read_text(encoding='utf-8', errors='replace').splitlines():
            parts = [p for p in line.strip().split('"') if p.strip()]
            if len(parts) == 2:
                info.setdefault(parts[0], parts[1])
        game = lib / 'steamapps' / 'common' / info.get('installdir', 'skieogame_demo')
        if (game / 'SkiResortSimulator' / 'Content' / 'Paks').is_dir():
            return game, info.get('buildid')
    return None, None


def is_valid_game(game):
    p = Path(game) / 'SkiResortSimulator' / 'Content' / 'Paks'
    return (p / 'global.utoc').is_file() and any(p.glob('SkiResortSimulator-*.utoc'))


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
# of the constant (the tree arrays follow the record count), so its immediate is raised in skieogame.exe.
TREE_LIMIT = 20000000
# cmp esi, imm32 ; jbe ok ; mov eax,1 ; lea rdx,[the "... records, over the bake cap" message]
_TREE_CHECK = re.compile(rb'\x81\xFE(....)\x76.\xB8\x01\x00\x00\x00\x48\x8D\x15', re.S)
_tree_limit_seen = {}


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
            if 0 <= msg - (rva(m.end()) + 4 + struct.unpack_from('<i', data, m.end())[0]) < 128]
    return hits[0].start(1) if len(hits) == 1 else None


def raise_tree_limit(folder):
    """Sets the game's tree limit to TREE_LIMIT (game closed). Steam updates undo it, so it is checked whenever the
    app looks at the game; cached per exe version. Returns the limit in effect (None: unknown, logged once)."""
    exe = Path(folder) / 'SkiResortSimulator' / 'Binaries' / 'Win64' / GAME_EXE
    key = (str(exe),)
    try:
        st = exe.stat()
        key = (str(exe), st.st_size, st.st_mtime_ns)
        if key in _tree_limit_seen:
            return _tree_limit_seen[key]
        data = exe.read_bytes()
        at = _tree_check_at(data)
        limit = at and struct.unpack_from('<I', data, at)[0]
        if not at:
            log.warning('tree limit check not found in %s: maps over 1.5M trees will show none', exe)
        elif limit < TREE_LIMIT:
            if game_running():
                return limit                                    # the exe is locked; tried again next time
            with open(exe, 'r+b') as f:
                f.seek(at)
                f.write(struct.pack('<I', TREE_LIMIT))
            log.info('game tree limit raised from %d to %d', limit, TREE_LIMIT)
            limit = TREE_LIMIT
            st = exe.stat()
            key = (str(exe), st.st_size, st.st_mtime_ns)
    except (OSError, struct.error, StopIteration) as e:                # never let this break the app
        if key in _tree_limit_seen:
            return None
        log.warning('could not raise the game tree limit: %s', e)
        limit = None
    _tree_limit_seen[key] = limit
    return limit


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
                if f not in names or '/' in f or '\\' in f or not f.startswith('ZZZ_'):
                    raise InstallError('%s is damaged (bad file list).' % Path(path).name)
        return m

    def check_ready(self):
        if game_running():
            raise InstallError('Close Ski-E-O first, then try again.')
        if not is_valid_game(self.folder):
            raise InstallError('The game files were not found in %s.' % self.folder)

    def install(self, package, progress=lambda msg: None):
        self.check_ready()
        raise_tree_limit(self.folder)
        m = self.read_package(package)
        st = self.state()
        for mid, other in st['maps'].items():
            if mid != m['id'] and other['entry'].get('slot') and other['entry'].get('slot') == m['entry'].get('slot'):
                raise InstallError('"%s" uses the same menu slot as "%s". Uninstall "%s" first.'
                                   % (m['name'], other['name'], other['name']))
            if mid != m['id'] and set(other['files']) & set(m['files']):
                raise InstallError('"%s" has files that belong to "%s".' % (m['name'], other['name']))
        self.mods.mkdir(parents=True, exist_ok=True)
        progress('Copying %s...' % m['name'])
        old = st['maps'].get(m['id'])
        with zipfile.ZipFile(package) as z:
            for f in m['files']:
                tmp = self.mods / (f + '.part')
                with z.open(f) as src, open(tmp, 'wb') as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                os.replace(tmp, self.mods / f)
        if old:
            for f in set(old['files']) - set(m['files']):
                (self.mods / f).unlink(missing_ok=True)
        st['maps'][m['id']] = {k: m[k] for k in ('id', 'name', 'version', 'order', 'files', 'entry', 'game_build')}
        self.save_state(st)
        progress('Updating the map list...')
        self.rebuild_registry(st)
        return m

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
        work = Path(tempfile.mkdtemp(prefix='.skimap_', dir=self.folder / 'SkiResortSimulator'))
        try:
            ua, ue, so = extract_registry(self.paks, work)
            for m in maps:
                ua, ue = apply_entry(ua, ue, m['entry'])
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
            shutil.rmtree(work, ignore_errors=True)


def run_retoc(args):
    exe = paths.retoc()
    r = subprocess.run([str(exe)] + args, capture_output=True, text=True, encoding='utf-8', errors='replace',
                       creationflags=NO_WINDOW, cwd=str(exe.parent))     # retoc prints UTF-8 (paths can have accents)
    if r.returncode != 0:
        raise InstallError('retoc failed: ' + (r.stderr or r.stdout)[-400:])
    return r


def extract_registry(paks, work):
    """The game's original DA_LevelRegistry (uasset, uexp) and script objects, read from its own containers.
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
    run_retoc(['to-legacy', '--no-shaders', '--filter', 'DA_LevelRegistry', str(src), str(out)])
    d = out / 'SkiResortSimulator' / 'Content' / 'Data'
    try:
        return ((d / 'DA_LevelRegistry.uasset').read_bytes(), (d / 'DA_LevelRegistry.uexp').read_bytes(),
                (out / 'scriptobjects.bin').read_bytes())
    except OSError:
        raise InstallError('The game\'s map list could not be read -- the game may have changed.')


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


def apply_entry(ua, ue, spec):
    """Point one menu slot of the registry at a custom map, or add a new entry for it.
    spec: slot (existing entry to take over) | append (existing entry to copy as a new one, placed after the last
          entry of the same biome), add_names, initial_save ({package, object} -> new imports | {copy: [slot, field]}),
          set: [[field, kind, ...]]"""
    ua2, _, names = lh.edit(ua, add=spec['add_names'])
    ix = names.index
    n_imp = lh.parse(ua2)[0]['imports_count'][1]
    isv = spec['initial_save']
    if 'package' in isv:
        obj_imp = (ix('/Script/SkiResortSimulator'), 0, ix('InitialSaveAsset'), 0, -(n_imp + 2), ix(isv['object']), 0, 0)
        pkg_imp = (ix('/Script/CoreUObject'), 0, ix('Package'), 0, 0, ix(isv['package']), 0, 0)
        ua3, first = lh.add_imports(ua2, [obj_imp, pkg_imp])
        assert first == n_imp
        is_ref = struct.pack('<i', -(n_imp + 1))
    else:
        ua3 = ua2
        _, other, _ = le.slices(ue, entry_at(ue, isv['copy'][0]))
        is_ref = other[isv['copy'][1]]
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
