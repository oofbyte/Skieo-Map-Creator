import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skieo_creator.osm.automatic_osm import acquire, compile_snapshot, query_for, validate_snapshot, osm_name
from skieo_creator.osm.creator import prepare_automatic, options
from skieo_creator.osm.io import sha256


class Cancel:
    def check(self):
        pass


def snapshot(path):
    values = [dict(type='node', id=1, lon=9, lat=46),
              dict(type='node', id=2, lon=9.001, lat=46.001),
              dict(type='node', id=3, lon=9.0005, lat=46.0005),
              dict(type='way', id=10, nodes=[1, 3, 2], tags={'aerialway': 'gondola', 'name': 'Lift'}),
              dict(type='way', id=11, nodes=[1, 2], tags={'aerialway': 'platter', 'name': 'Surface'}),
              dict(type='way', id=20, nodes=[2, 3, 1], tags={}),
              dict(type='relation', id=30, members=[{'type': 'way', 'ref': 20}],
                   tags={'route': 'piste', 'piste:type': 'downhill', 'piste:difficulty': 'easy', 'name': 'Trail'})]
    path.write_text(json.dumps({'elements': values}))
    return path


class AutomaticOSMTests(unittest.TestCase):
    spec = dict(lat=46, lon=9, size_m=4000, osm_lifts=True, osm_pistes=True)

    def test_osm_names_unicode_localized_fallback_and_geographic_anchors(self):
        self.assertEqual(osm_name({'name': 'Grostè', 'name:en': 'Grostè EN'}), ('Grostè', 'name'))
        self.assertEqual(osm_name({'name:it': 'Cima Grostè'}), ('Cima Grostè', 'name:it'))
        self.assertEqual(osm_name({'ref': '42'}), ('42', 'ref'))
        self.assertEqual(osm_name({}, 'OSM 10'), ('OSM 10', 'generated-id'))
        with tempfile.TemporaryDirectory() as tmp:
            path = snapshot(Path(tmp)/'osm.json')
            data = json.loads(path.read_text())
            data['elements'][0]['tags'] = {'aerialway': 'station', 'name': 'Campo Carlo Magno'}
            data['elements'][1]['tags'] = {'natural': 'peak', 'name': 'Cima Grostè'}
            data['elements'][3]['tags']['name'] = 'Grostè Express'
            path.write_text(json.dumps(data))
            scene = compile_snapshot(self.spec, '/Game/Maps/Test', path, lambda x, y: -y)
            self.assertEqual(scene['lifts'][0]['name'], 'Grostè Express')
            self.assertEqual(scene['lifts'][0]['extensions']['skieo']['name'], 'Grostè Express')
            self.assertEqual(scene['places'][0]['name'], 'Campo Carlo Magno')
            self.assertEqual(scene['places'][0]['position_cm'], [0, 0])
            self.assertEqual(scene['places'][1]['kind'], 'place')

    def test_flags_control_download_and_no_paths_are_needed(self):
        both = query_for(self.spec)
        self.assertIn('way["aerialway"]', both)
        self.assertIn('relation["route"="piste"]', both)
        self.assertIn('>>', both)  # Full original member ways and nodes.
        self.assertNotIn('way["aerialway"]', query_for(dict(self.spec, osm_lifts=False)))
        self.assertNotIn('piste', query_for(dict(self.spec, osm_pistes=False)))
        with self.assertRaises(ValueError):
            query_for(dict(self.spec, lat=85))
        self.assertIsNone(prepare_automatic({}, '', Path('.'), None, None, None, None))
        for invalid in ('true', 1, None, []):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                options({'osm_lifts': invalid})

    def test_original_vertices_relations_units_and_independent_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = snapshot(Path(tmp)/'osm.json')
            digest = sha256(path)
            for lifts, pistes in ((True, True), (True, False), (False, True)):
                scene = compile_snapshot(dict(self.spec, osm_lifts=lifts, osm_pistes=pistes),
                                         '/Game/Maps/Test', path, lambda x, y: 1000-y)
                self.assertEqual(len(scene['lifts']), int(lifts))
                self.assertEqual(len(scene['pistes']), int(pistes))
                if lifts:
                    lift = scene['lifts'][0]
                    self.assertEqual(len(lift['route_m']), 3)
                    self.assertEqual(lift['terminals_m'][0], (0, 0))
                    self.assertEqual(lift['extensions']['skieo']['position_cm'], [0, 0])
                    self.assertTrue(any('Surface lifts' in v['reason'] for v in scene['omitted']))
                if pistes:
                    piste = scene['pistes'][0]
                    self.assertEqual(piste['osm_relation_ids'], [30])
                    self.assertEqual(piste['points_m'][-1], (0, 0))
                    self.assertEqual(piste['difficulty'], 0)
                    self.assertEqual(piste['extensions']['skieo']['width'], 3000)
                    self.assertIn('not measured', piste['width_basis'])
                self.assertEqual(sha256(path), digest)

    def test_missing_geometry_invalid_heights_and_partial_download_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = snapshot(Path(tmp)/'osm.json')
            with self.assertRaisesRegex(ValueError, 'Invalid terrain'):
                compile_snapshot(self.spec, '/Game/Maps/Test', path, lambda x, y: float('nan'))
            data = json.loads(path.read_text())
            data['elements'] = [v for v in data['elements'] if v['id'] != 3]
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'missing original nodes'):
                compile_snapshot(self.spec, '/Game/Maps/Test', path, lambda x, y: -y)
        for raw in (b'{"remark":"timeout","elements":[]}', b'{"foo":1}'):
            with self.assertRaises(ValueError):
                validate_snapshot(raw)

    def test_network_fallback_cancellation_and_immutable_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = snapshot(root/'fixture.json').read_bytes()
            calls = []
            def get(url):
                calls.append(url)
                if len(calls) == 1:
                    return b'{"remark":"server timeout","elements":[]}'
                return raw
            path = acquire(self.spec, root/'raw', get, Cancel(), lambda *args: None)
            self.assertEqual(len(calls), 2)
            digest = sha256(path)
            acquire(self.spec, root/'raw', lambda url: raw, Cancel(), lambda *args: None)
            self.assertEqual(sha256(path), digest)
            self.assertEqual(len(list((root/'raw').glob('*.provenance.json'))), 1)
            class Cancelled(Exception):
                pass
            class Token:
                def check(self):
                    raise Cancelled()
            with self.assertRaises(Cancelled):
                acquire(self.spec, root/'raw', get, Token(), lambda *args: None)

    def test_rebuild_pins_snapshot_and_preserves_raw_processed_separation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = snapshot(root/'fixture.json').read_bytes()
            spec = dict(self.spec)
            get = lambda url: raw
            scene = prepare_automatic(spec, '/Game/Maps/Test', root/'cache', lambda x, y: -y,
                                         get, Cancel(), lambda *args: None)
            self.assertEqual(len(scene['lifts']), 1)
            self.assertEqual(len(list((root/'cache/processed').glob('*.json'))), 1)
            with patch('skieo_creator.osm.automatic_osm.acquire') as fetch:
                prepare_automatic(spec, '/Game/Maps/Test', root/'cache', lambda x, y: -y,
                                  get, Cancel(), lambda *args: None)
                fetch.assert_not_called()
            Path(spec['osm_source']['path']).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                prepare_automatic(spec, '/Game/Maps/Test', root/'cache', lambda x, y: -y,
                                  get, Cancel(), lambda *args: None)
