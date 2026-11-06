"""Checks for the standalone source/frozen integration of the OSM port."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from skieo_creator import paths
from skieo_creator.osm.native_map import NativeMapFrame, TransverseMercator

ROOT = Path(__file__).resolve().parents[1]


def test_projection_matches_retained_independent_proj_reference():
    e, n = TransverseMercator(9, k0=.9996, false_easting=500000).project(10.8294225, 46.228409)
    assert abs(e-641071.4100571056) < .00001
    assert abs(n-5121052.376263438) < .00001
    frame = NativeMapFrame('/Game/Maps/Test', TransverseMercator(9, 46), (4000, 4000))
    assert frame.local(9, 46) == (0, 0)
    assert frame.local(9.001, 46)[0] > 0
    assert frame.local(9, 45.999)[1] > 0


def test_bundled_official_code_and_data_have_recorded_hashes():
    data = ROOT/'data/osm'
    provenance = json.loads((data/'provenance.json').read_text())
    assert provenance['version'] == '1.29.0'
    assert hashlib.sha256((ROOT/'skieo_creator/osm/scene.py').read_bytes()).hexdigest() == provenance['compiler_adapter_sha256']
    assert hashlib.sha256((data/'native-lifts-1.29.0.json').read_bytes()).hexdigest() == provenance['catalog_subset_sha256']
    document = json.loads((data/'native-lifts-1.29.0.json').read_text())
    assert all(item['kind'] == 'lift' for item in document['catalog'])
    assert {item['class'] for item in document['catalog']} == {item['class'] for item in document['defaults']['objects']}


def test_imports_work_without_previous_project_or_skieomod_installation():
    script = '''
import importlib.abc, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
class BlockPreviousProject(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('skieo_real', 'skieo_game_catalog', 'skieo_scene', 'custom_map', 'custom_maps'):
            raise RuntimeError('Unexpected external dependency: '+fullname)
sys.meta_path.insert(0, BlockPreviousProject())
from skieo_creator import api
from skieo_creator.osm import automatic_osm, native_save
from skieo_creator.osm.creator import prepare_automatic
from skieo_creator.osm.catalog import CATALOG
assert CATALOG
assert prepare_automatic({}, '', Path('.'), None, None, None, None) is None
'''
    result = subprocess.run([sys.executable, '-I', '-c', script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout+result.stderr


def test_frozen_resources_include_catalog_and_provenance_sources(monkeypatch):
    # The spec includes the entire data directory plus the two public source files.
    monkeypatch.setattr(sys, '_MEIPASS', str(ROOT), raising=False)
    assert paths.resource('data', 'osm', 'provenance.json').is_file()
    assert paths.resource('skieo_creator', 'builder.py').is_file()
    assert paths.resource('skieo_creator', 'geo.py').is_file()
    spec = (ROOT/'packaging/SkiEO-Map-Creator.spec').read_text()
    assert "ROOT / 'skieo_creator' / 'builder.py'" in spec
    assert "ROOT / 'skieo_creator' / 'geo.py'" in spec



def test_previous_native_class_encoding_requires_rebuild(tmp_path, monkeypatch):
    from skieo_creator import api
    monkeypatch.setattr(paths, 'MAPS', tmp_path)
    creator = api.Api.__new__(api.Api)
    creator._game = creator._gd = None
    record = {'slug': 'osmbranch_0001', 'name': 'Test', 'recipe': api.builder.RECIPE,
              'spec': {'lat': 46, 'lon': 9, 'size_m': 4000, 'biome': 'Alps', 'osm_lifts': True},
              'native_osm': {'version': 1}}
    assert creator._map_view(record)['stale']
    record['native_osm']['version'] = 5
    assert not creator._map_view(record)['stale']
