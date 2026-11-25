"""Offline check of taking out what the Mod Library of 1.2 to 1.8 put into the game (skieo_creator/oldmods.py), in
stand-in game folders: only what it can tell is the app's goes, never maps, the map list, Z's Mod Loader or anything
else. usage: python tests/test_oldmods.py"""
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skieo_creator import oldmods  # noqa: E402

if sys.platform != 'win32':                   # the Mod Library was Windows only (UE4SS); the checks need Windows' file locks
    print('old mods: skipped (Windows only)')
    sys.exit(0)

DLL, UE4SS, UTOC = b'the app dwmapi', b'the app UE4SS', b'the app R2 Six toc'
oldmods.DWMAPI = {hashlib.sha256(DLL).hexdigest()}          # stand-ins for the files the app shipped
oldmods.UE4SS_DLL = {hashlib.sha256(UE4SS).hexdigest()}
oldmods.PAKS = dict(oldmods.PAKS, ZZZ_DoppelmayrR2Six_P={hashlib.sha256(UTOC).hexdigest()})


def put(p, data=b'x'):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def game(root, ue4ss='app', others=(), lua_manifest=True):
    """a stand-in game folder: UE4SS ('app': the app's, 'own': the player's, 'zs': Z's Mod Loader over the app's),
    the app's mods and packs, a map, the map list, another mod's pak, Z's mods folder"""
    sim = root / 'SkiResortSimulator'
    win, mods = sim / 'Binaries' / 'Win64', sim / 'Content' / 'Paks' / '~mods'
    u = win / 'ue4ss'
    put(win / 'skieogame.exe')
    if ue4ss in ('app', 'zs'):
        put(u / oldmods.MARKER, b'UE4SS was installed by Ski-E-O Map Creator')
    put(win / 'dwmapi.dll', b'Z dwmapi' if ue4ss == 'zs' else DLL if ue4ss == 'app' else b'own dwmapi')
    put(u / 'UE4SS.dll', b'Z UE4SS' if ue4ss == 'zs' else UE4SS if ue4ss == 'app' else b'own UE4SS')
    put(u / 'UE4SS-settings.ini')
    put(u / 'UE4SS.log')
    put(u / 'Mods' / 'shared' / 'UEHelpers' / 'UEHelpers.lua')
    if ue4ss == 'zs':
        put(u / '.skieo-loader-v4')
        put(u / 'Mods' / 'SkiEOModsMenu' / 'Scripts' / 'main.lua')
    for n in ('OofsCheatMenu', 'OofsAnarchy'):
        put(u / 'Mods' / n / 'Scripts' / 'main.lua')
        if lua_manifest:
            put(u / 'Mods' / n / oldmods.MANIFEST, b'{}')
    put(u / 'Mods' / 'OofsCheatMenu' / 'settings.txt')
    for n in others:
        put(u / 'Mods' / n / 'Scripts' / 'main.lua')
    put(u / 'Mods' / 'mods.txt', ('OofsCheatMenu : 1\r\nOofsAnarchy : 1\r\n' + ''.join('%s : 1\r\n' % n for n in others)
                                  + 'Keybinds : 1\r\n').encode())
    put(mods / 'ZZZ_DoppelmayrR2Six_P.pak')
    put(mods / 'ZZZ_DoppelmayrR2Six_P.ucas')
    put(mods / 'ZZZ_DoppelmayrR2Six_P.utoc', UTOC)
    put(mods / 'ZZZ_AlpineBuildings_P.pak')                 # a version the app never shipped: not the app's
    put(mods / 'ZZZ_AlpineBuildings_P.utoc', b'someone else\'s toc')
    for e in oldmods.PAK_EXTS:
        put(mods / ('ZZZ_LiftStock_9_P' + e))
        put(mods / ('ZZZ_MC_vail_1234_P' + e))
        put(mods / ('ZZZ_Registry_P' + e))
        put(mods / ('ZZZ_TheirMod_P' + e))
    put(mods / 'skieo_lift_stock.json', b'[3]')
    put(mods / 'skimap_installer.json', b'{}')
    put(root / 'mods' / 'AssetPackCombiner' / 'Scripts' / 'main.lua')
    return sim


def left_alone(sim, root):
    mods = sim / 'Content' / 'Paks' / '~mods'
    for e in oldmods.PAK_EXTS:
        assert (mods / ('ZZZ_MC_vail_1234_P' + e)).is_file() and (mods / ('ZZZ_Registry_P' + e)).is_file()
        assert (mods / ('ZZZ_TheirMod_P' + e)).is_file()
    assert (mods / 'skimap_installer.json').is_file(), 'the maps stay'
    assert (mods / 'ZZZ_AlpineBuildings_P.utoc').is_file(), 'a pack the app never shipped stays'
    assert (root / 'mods' / 'AssetPackCombiner' / 'Scripts' / 'main.lua').is_file(), "Z's mods stay"
    assert (sim / 'Binaries' / 'Win64' / 'skieogame.exe').is_file()
    assert not (mods / 'skieo_lift_stock.json').exists() and not any((mods / ('ZZZ_LiftStock_9_P' + e)).exists()
                                                                     for e in oldmods.PAK_EXTS), 'the merged pak goes'
    assert not any((mods / ('ZZZ_DoppelmayrR2Six_P' + e)).exists() for e in oldmods.PAK_EXTS), 'its own pack goes'


with tempfile.TemporaryDirectory() as tmp:
    # the app's UE4SS, nothing else in it: all of it goes, with its mods; the rest stays
    root = Path(tmp) / 'a'
    sim = game(root)
    assert oldmods.remove(root)
    win = sim / 'Binaries' / 'Win64'
    assert not (win / 'dwmapi.dll').exists() and not (win / 'ue4ss').exists(), 'the app\'s UE4SS goes'
    left_alone(sim, root)
    assert oldmods.remove(root), 'nothing left: done again at once'

    # other mods use the app's UE4SS: it stays (the player's now), only the app's mods and their lines go
    root = Path(tmp) / 'b'
    sim = game(root, others=['SomeoneElsesMod'])
    assert oldmods.remove(root)
    u = sim / 'Binaries' / 'Win64' / 'ue4ss'
    assert (sim / 'Binaries' / 'Win64' / 'dwmapi.dll').is_file() and (u / 'UE4SS.dll').is_file()
    assert (u / 'Mods' / 'SomeoneElsesMod' / 'Scripts' / 'main.lua').is_file() and (u / 'Mods' / 'shared').is_dir()
    assert not (u / 'Mods' / 'OofsCheatMenu').exists() and not (u / 'Mods' / 'OofsAnarchy').exists()
    assert not (u / oldmods.MARKER).exists(), 'no longer the app\'s'
    assert (u / 'Mods' / 'mods.txt').read_bytes() == b'SomeoneElsesMod : 1\r\nKeybinds : 1\r\n', 'its line ends kept'
    left_alone(sim, root)

    # Z's Mod Loader installed over the app's UE4SS: it stays as it is, the app's mods go
    root = Path(tmp) / 'c'
    sim = game(root, ue4ss='zs')
    assert oldmods.remove(root)
    u = sim / 'Binaries' / 'Win64' / 'ue4ss'
    assert (sim / 'Binaries' / 'Win64' / 'dwmapi.dll').read_bytes() == b'Z dwmapi' and (u / '.skieo-loader-v4').is_file()
    assert (u / 'Mods' / 'SkiEOModsMenu' / 'Scripts' / 'main.lua').is_file() and (u / 'UE4SS.log').is_file()
    assert not (u / 'Mods' / 'OofsCheatMenu').exists()
    left_alone(sim, root)

    # the player's own UE4SS: only the app's mods that carry its record go
    root = Path(tmp) / 'd'
    sim = game(root, ue4ss='own')
    u = sim / 'Binaries' / 'Win64' / 'ue4ss'
    (u / 'Mods' / 'OofsAnarchy' / oldmods.MANIFEST).unlink()             # a copy the player put there by hand
    assert oldmods.remove(root)
    assert not (u / 'Mods' / 'OofsCheatMenu').exists() and (u / 'Mods' / 'OofsAnarchy' / 'Scripts' / 'main.lua').is_file()
    assert (u / 'Mods' / 'mods.txt').read_bytes() == b'OofsAnarchy : 1\r\nKeybinds : 1\r\n'
    assert (sim / 'Binaries' / 'Win64' / 'dwmapi.dll').is_file() and (u / 'UE4SS.dll').is_file()
    left_alone(sim, root)

    # a dwmapi.dll that isn't the one the app shipped: its UE4SS stays
    root = Path(tmp) / 'e'
    sim = game(root)
    put(sim / 'Binaries' / 'Win64' / 'dwmapi.dll', b'an updated dwmapi')
    assert oldmods.remove(root)
    assert (sim / 'Binaries' / 'Win64' / 'ue4ss' / 'UE4SS.dll').is_file()
    assert not (sim / 'Binaries' / 'Win64' / 'ue4ss' / 'Mods' / 'OofsCheatMenu').exists()

    # a pack and the merged pak the Asset Pack Combiner turned off (.pak.off) are the combiner's to delete, all of them;
    # a merged pak without the app's stamp isn't provably the app's
    root = Path(tmp) / 'f'
    sim = game(root)
    mods = sim / 'Content' / 'Paks' / '~mods'
    for stem in ('ZZZ_DoppelmayrR2Six_P', 'ZZZ_LiftStock_9_P'):
        (mods / (stem + '.pak')).rename(mods / (stem + '.pak.off'))
    assert oldmods.remove(root)
    for stem in ('ZZZ_DoppelmayrR2Six_P', 'ZZZ_LiftStock_9_P'):
        assert all((mods / (stem + e)).is_file() for e in ('.pak.off', '.ucas', '.utoc')), stem
    assert (mods / 'skieo_lift_stock.json').is_file()
    root = Path(tmp) / 'f2'
    sim = game(root)
    mods = sim / 'Content' / 'Paks' / '~mods'
    (mods / 'skieo_lift_stock.json').unlink()
    assert oldmods.remove(root)
    assert all((mods / ('ZZZ_LiftStock_9_P' + e)).is_file() for e in oldmods.PAK_EXTS), 'no stamp: left alone'

    # Z's Mod Loader took the app's UE4SS over: a copy of an Oof mod without the app's record in it stays
    root = Path(tmp) / 'z2'
    sim = game(root, ue4ss='zs')
    u = sim / 'Binaries' / 'Win64' / 'ue4ss'
    (u / 'Mods' / 'OofsAnarchy' / oldmods.MANIFEST).unlink()
    assert oldmods.remove(root)
    assert not (u / 'Mods' / 'OofsCheatMenu').exists() and (u / 'Mods' / 'OofsAnarchy' / 'Scripts' / 'main.lua').is_file()

    # the app's UE4SS set up to load mods from other folders too: it stays, the app's mods go
    root = Path(tmp) / 'h'
    sim = game(root)
    u = sim / 'Binaries' / 'Win64' / 'ue4ss'
    put(u / 'UE4SS-settings.ini', b'[Overrides]\r\n+ModsFolderPaths = ../../../../mods\r\n')
    assert oldmods.remove(root)
    assert (u / 'UE4SS.dll').is_file() and (sim / 'Binaries' / 'Win64' / 'dwmapi.dll').is_file()
    assert not (u / 'Mods' / 'OofsCheatMenu').exists() and not (u / oldmods.MARKER).exists()

    # a mod folder that is a junction to a folder outside the game: only the link goes, never what it points at
    import _winapi
    root = Path(tmp) / 'j'
    sim = game(root)
    u = sim / 'Binaries' / 'Win64' / 'ue4ss'
    outside = put(Path(tmp) / 'dev' / 'OofsCheatMenu' / 'Scripts' / 'main.lua', b'the modder\'s own copy')
    import shutil as _sh
    _sh.rmtree(u / 'Mods' / 'OofsCheatMenu')
    _winapi.CreateJunction(str(outside.parent.parent), str(u / 'Mods' / 'OofsCheatMenu'))
    assert oldmods.remove(root)
    assert outside.read_bytes() == b'the modder\'s own copy', 'the junction\'s target untouched'
    assert not u.exists()

    # a read-only file still goes
    root = Path(tmp) / 'r'
    sim = game(root)
    import os as _os, stat as _st
    _os.chmod(sim / 'Binaries' / 'Win64' / 'ue4ss' / 'UE4SS.log', _st.S_IREAD)
    _os.chmod(sim / 'Content' / 'Paks' / '~mods' / 'ZZZ_LiftStock_9_P.pak', _st.S_IREAD)
    assert oldmods.remove(root)
    assert not (sim / 'Binaries' / 'Win64' / 'ue4ss').exists()
    assert not (sim / 'Content' / 'Paks' / '~mods' / 'ZZZ_LiftStock_9_P.pak').exists()

    # a file that won't go (in use): tried again later, the marker stays till the rest of UE4SS is gone
    root = Path(tmp) / 'g'
    sim = game(root)
    u = sim / 'Binaries' / 'Win64' / 'ue4ss'
    with open(u / 'UE4SS.log', 'rb'):
        assert not oldmods.remove(root), 'something left'
        assert (u / oldmods.MARKER).is_file() and (u / 'UE4SS.log').is_file()
    assert oldmods.remove(root) and not u.exists(), 'gone the next time'
print('old mods: all checks passed')
