"""Optional native build/roundtrip with private assets from a local game.

No installation, game launch, runtime importer or personal save is involved.
Private input paths are supplied through the SKIEO_CREATOR_* environment
variables checked below. Nothing proprietary is a fixture.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import zipfile

import numpy as np
import pytest

from skieo_creator import api, dem, gamedata, install, paths
from skieo_creator.osm.io import sha256
from skieo_creator.osm.native_save import root_gvas, root_fields, one, structs_array, read_fstr


def test_native_creator_build_and_cooked_roundtrip(tmp_path, monkeypatch):
    keys = ('SKIEO_CREATOR_GAME_CACHE', 'SKIEO_CREATOR_MAP_RECORD', 'SKIEO_CREATOR_DEM',
            'SKIEO_CREATOR_OSM_SNAPSHOT', 'SKIEO_CREATOR_RETOC', 'SKIEO_CREATOR_GLOBAL_UTOC',
            'SKIEO_CREATOR_GLOBAL_UCAS', 'SKIEO_CREATOR_GAME_EXE')
    values = {key: os.environ.get(key) for key in keys}
    if not all(values.values()):
        pytest.skip('Supply private local game/cache inputs for native build and roundtrip')
    inputs = {key: Path(value).resolve() for key, value in values.items()}
    record = json.loads(inputs['SKIEO_CREATOR_MAP_RECORD'].read_text(encoding='utf-8'))
    spec = dict(record['spec'], slug='osmbranch_0001', name='OSM Branch Validation', treeline_fade=200)
    expected_dem = dem.cache_key(spec['lat'], spec['lon'], spec['size_m'], spec['source']) + '.npy'
    assert inputs['SKIEO_CREATOR_DEM'].name == expected_dem, 'DEM cache does not belong to this map recipe'
    game = gamedata.GameData.__new__(gamedata.GameData)
    game.dir = inputs['SKIEO_CREATOR_GAME_CACHE']
    game.fp = record['game_fp']
    game.info = game._validate(game.dir)
    raw = tmp_path/'osm/raw'/inputs['SKIEO_CREATOR_OSM_SNAPSHOT'].name
    raw.parent.mkdir(parents=True)
    shutil.copyfile(inputs['SKIEO_CREATOR_OSM_SNAPSHOT'], raw)
    assert sha256(raw) == spec['osm_source']['sha256']
    spec['osm_source'] = dict(spec['osm_source'], path=str(raw))
    monkeypatch.setattr(paths, 'APP_DIR', tmp_path)
    monkeypatch.setattr(paths, 'MAPS', tmp_path/'maps')
    monkeypatch.setattr(paths, 'retoc', lambda: inputs['SKIEO_CREATOR_RETOC'])
    def no_network(*args, **kwargs):
        raise AssertionError('The pinned build must not download a different snapshot')
    monkeypatch.setattr(api.net, 'get', no_network)
    creator = api.Api.__new__(api.Api)
    creator._gd = game
    creator._game = SimpleNamespace(build=record['game_build'], folder=inputs['SKIEO_CREATOR_GAME_EXE'].parents[3])
    job = SimpleNamespace(cancel=SimpleNamespace(check=lambda: None), progress=lambda *args: None)
    output = creator._build_and_package(job, spec, np.load(inputs['SKIEO_CREATOR_DEM']), 0, 1, 1041)
    assert 'osm_overlay' not in output
    assert output['native_osm']['runtime_import_mod'] is False
    assert 'osm_initial_save' not in output['spec']
    folder = paths.MAPS/output['slug']
    package = folder/output['package']
    manifest = install.Game.read_package(package)
    assert ['InitialSave', 'initial_save'] in manifest['entry']['set']
    assert ['bIsTutorialMap', 'hex', '00'] in manifest['entry']['set']
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        assert 'Reports/ATTRIBUTION.txt' in archive.namelist()
        assert 'Reports/native-supports.skieomod' in archive.namelist()
        assert not any(name.endswith('.lua') for name in archive.namelist())
    view = tmp_path/'roundtrip-input'
    view.mkdir()
    for key in ('SKIEO_CREATOR_GLOBAL_UTOC', 'SKIEO_CREATOR_GLOBAL_UCAS', 'SKIEO_CREATOR_GAME_EXE'):
        shutil.copyfile(inputs[key], view/inputs[key].name)
    for name in output['files']:
        shutil.copyfile(folder/name, view/name)
    subprocess.run([str(paths.retoc()), 'verify', str(view/next(n for n in output['files'] if n.endswith('.utoc')))], check=True)
    extracted = tmp_path/'roundtrip'
    subprocess.run([str(paths.retoc()), 'to-legacy', '--no-shaders', '-f', 'InitialSaves', str(view), str(extracted)], check=True)
    saves = list(extracted.rglob('IS_Map_osmbranch_0001_tuto.uexp'))
    assert len(saves) == 1  # retoc exit status alone is insufficient.
    saved = root_gvas(saves[0].read_bytes())
    assert hashlib.sha256(saved).hexdigest() == output['native_osm']['initial_gvas_sha256']
    _, root, _ = root_fields(saved)
    scene = json.loads((folder/'Reports/resort.json').read_text(encoding='utf-8'))
    expected = {v['id']: v['extensions']['skieo']['class'] for v in scene['lifts']}
    for record, source in zip(structs_array(one(root, 'Lifts')), scene['lifts']):
        data = one(record, 'LiftClassPath').data
        package_name, offset = read_fstr(data, 0)
        object_name, offset = read_fstr(data, offset)
        assert package_name+'.'+object_name == expected[source['id']]
        assert data[offset:] == bytes(4)  # Native UTF-8 empty subobject, no NUL byte.
        assert one(record, 'Towers').data == bytes(4)
        assert len(structs_array(one(record, 'Waypoints'))) >= 2
    assert output['native_osm']['authored_towers'] == 0
    assert output['native_osm']['runtime_towers'] is None
    assert output['native_osm']['native_generation_contract']['local_executable_verified'] is True
