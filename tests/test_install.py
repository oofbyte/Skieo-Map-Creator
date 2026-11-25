"""Offline checks of the map list entries (skieo_creator/install.py, builder.entry_spec): usage: python tests/test_install.py"""
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skieo_creator import builder, install  # noqa: E402

# a custom map is never a tutorial map: New Game takes the first tutorial map of the list as THE tutorial map, whose
# "Skip Tutorial" starts it without its starting save (no real lifts and trails)
spec = builder.entry_spec(builder.Names('killingto_ddf8'), 'Killington', 'Northeast', {'PeakElevationFeet': 4241},
                          start_save=True)
assert ['bIsTutorialMap', 'remove'] in spec['set'] and not any(i[0] == 'bIsTutorialMap' and i[1] == 'hex' for i in spec['set'])
assert ['InitialSave', 'initial_save'] in spec['set'] and ['TutorialInitialSave', 'initial_save'] in spec['set']

# ... also maps packaged by 1.7.4 and older (their entries said bIsTutorialMap 01)
old = dict(spec, set=[i for i in spec['set'] if i[0] != 'bIsTutorialMap'] + [['bIsTutorialMap', 'hex', '01']])
fixed = install.custom_entry(old)
assert [i for i in fixed['set'] if i[0] == 'bIsTutorialMap'] == [['bIsTutorialMap', 'remove']], fixed['set']
assert ['DioramaVariantMap', 'ref', 'None', 'None'] in fixed['set'] and 'None' in fixed['add_names']
assert ['InitialSave', 'initial_save'] in fixed['set'], 'the starting save stays'
assert install.custom_entry(fixed)['set'].count(['bIsTutorialMap', 'remove']) == 1

# the map list of maps installed by an older version is rebuilt once
with tempfile.TemporaryDirectory() as tmp:
    g = install.Game(tmp)
    assert not g.registry_stale(), 'nothing installed'
    g.save_state({'format': 1, 'maps': {'mc_x': {'entry': old}}})
    assert g.registry_stale(), 'built by 1.7.4 or older'
    st = g.state()
    st['registry'] = install.REGISTRY_VERSION
    g.save_state(st)
    assert not g.registry_stale()
    assert json.loads(g.state_file.read_text(encoding='utf-8'))['registry'] == install.REGISTRY_VERSION

# a map package never replaces a file another mod put in ~mods (its own older files and the app's ZZZ_MC_ maps it may)
real = install.is_valid_game, install.game_running
install.is_valid_game, install.game_running = (lambda folder: True), (lambda: False)
try:
    with tempfile.TemporaryDirectory() as tmp:
        g = install.Game(tmp)
        g.mods.mkdir(parents=True)
        (g.mods / 'ZZZ_Theirs_P.pak').write_bytes(b'theirs')
        pkg = Path(tmp) / 'x.skimap'
        with zipfile.ZipFile(pkg, 'w') as z:
            z.writestr('manifest.json', json.dumps(dict(format=3, id='x', name='X', version=1, order=0, entry={},
                                                        files=['ZZZ_Theirs_P.pak'], game_build=0)))
            z.writestr('ZZZ_Theirs_P.pak', b'mine')
        try:
            g.install(pkg)
            raise AssertionError("replaced another mod's file")
        except install.InstallError as e:
            assert 'would replace ZZZ_Theirs_P.pak' in str(e), e
        assert (g.mods / 'ZZZ_Theirs_P.pak').read_bytes() == b'theirs' and not g.installed()
finally:
    install.is_valid_game, install.game_running = real

# macOS: the running game is found by its command line (the kernel's process name is cut to 16 characters there)
calls = []
real_run, real_mac = install.system.subprocess.run, install.system.MAC
install.system.subprocess.run = lambda argv, **kw: calls.append(argv) or type('R', (), {'returncode': 0})()
install.system.MAC = True
try:
    assert install.game_running() and calls == [['pgrep', '-f', 'MacOS/skieogame-Mac-Shipping']], calls
finally:
    install.system.subprocess.run, install.system.MAC = real_run, real_mac
print('install: all checks passed')
