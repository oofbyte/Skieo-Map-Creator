"""Offline package/installer checks; no engine or personal save integration."""
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from skieo_creator.osm import runtime
from skieo_creator.osm.io import read_json
from skieo_creator import paths, install


def fixture_resort():
    return SimpleNamespace(report={
        'support_preparation': {'target_package': '/Game/Maps/Generated/Map_test_real', 'lifts': {}},
        'native_generation_contract': {'game_build': '26188119'}})


def test_official_support_package_and_isolated_install(tmp_path, monkeypatch):
    game=tmp_path/'steamapps/common/skieogame_demo'
    mods=game/'SkiResortSimulator/Binaries/Win64/ue4ss/Mods'
    mods.mkdir(parents=True)
    (mods.parent/'UE4SS.dll').write_bytes(b'test placeholder')
    other=mods/'OtherMod';other.mkdir();(other/'untouched').write_bytes(b'unchanged')
    output=tmp_path/'output';output.mkdir()
    monkeypatch.setattr(paths,'APP_DIR',tmp_path/'creator')
    monkeypatch.setattr(install,'game_running',lambda:False)
    monkeypatch.setattr(runtime,'verify',lambda build,game:None)
    import custom_map
    monkeypatch.setattr(custom_map.desktop,'setup',lambda *a,**kw:pytest.fail('Must not reconfigure existing UE4SS'))
    report=runtime.build(fixture_resort(),output,'osmbranch_0001',game)
    archive=output/report['archive']
    kind,manifest,_=custom_map.inspect_package(archive)
    assert kind=='mod' and manifest['dependencies']==[]
    assert custom_map.main(['test',str(archive)]) == 0
    project=tmp_path/'official-project'
    with zipfile.ZipFile(archive) as package:
        package.extractall(project)
    assert custom_map.main(['test',str(project)]) == 0
    with zipfile.ZipFile(archive) as z:
        config=json.loads(z.read('Scripts/config.json'))
        assert (mods/report['directory']/config['game_manifest_relative']).resolve()==game.parents[1]/'appmanifest_5012760.acf'
        assert b'RegisterHook(' not in z.read('Scripts/main.lua')
    (mods/'mods.txt').write_text(report['directory']+' : 0\nOtherMod : 0\n')
    runtime.install(output,report,game,'osmbranch_0001','26188119')
    assert (mods/'mods.txt').read_text()==report['directory']+' : 1\nOtherMod : 0\n'
    folder=mods/report['directory']
    assert (folder/'enabled.txt').is_file()
    assert read_json(folder/'custom-mod.json')['id']==report['id']
    runtime.install(output,report,game,'osmbranch_0001','26188119')
    assert list((paths.APP_DIR/'support-helper-backups').iterdir())
    runtime.uninstall(game,'osmbranch_0001')
    assert not folder.exists()
    assert (other/'untouched').read_bytes()==b'unchanged'
    archive.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'):
        runtime.install(output,report,game,'osmbranch_0001','26188119')


def test_refuses_foreign_folder(tmp_path,monkeypatch):
    game=tmp_path/'game';folder=runtime.mods_path(game)/runtime.identity('osmbranch_0001')[1]
    folder.mkdir(parents=True);(folder/'custom-mod.json').write_text('{"id":"another.mod"}')
    with pytest.raises(ValueError,match='identity differs'):
        runtime.uninstall(game,'osmbranch_0001')
    assert folder.exists()
