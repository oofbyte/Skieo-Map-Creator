"""Native format checks; proprietary schemas are supplied from a local game."""
import copy
import os
import struct
from pathlib import Path

import pytest
from skieo_creator.osm import native_save as ns
from skieo_creator.osm.creator import retire_legacy_overlay



def test_fstring_unicode_lengths_and_invalid_data():
    for value in ('', 'Pradalago', 'Grostè', '山頂', 'Station 🚠'):
        raw = ns.fstr(value)
        assert ns.read_fstr(raw, 0) == (value, len(raw))
    for raw in (b'', struct.pack('<i', -100), struct.pack('<i', 2)+b'ab'):
        with pytest.raises(ValueError):
            ns.read_fstr(raw, 0)
    with pytest.raises(ValueError):
        ns.fstr('a\0b')


def test_indexed_fields_are_preserved_and_malformed_tags_rejected():
    # Generic tagged property serialization, with no invented game schema.
    prefix = ns.fstr('Example')+ns.fstr('IntProperty')+struct.pack('<i', 0)
    values = [ns.Field('Example', ('IntProperty', ()), prefix, 1, i, struct.pack('<i', i)) for i in range(4)]
    raw = ns.encode(values)
    assert ns.fields(raw) == values
    assert ns.encode(ns.fields(raw)) == raw
    with pytest.raises(ValueError):
        ns.fields(raw[:-1])
    with pytest.raises(ValueError):
        ns.fields(raw+b'bad')
    with pytest.raises(ValueError):
        ns.fields(ns.encode([ns.replace(values[0], flags=0x40)]))


def test_native_registry_entry_includes_regular_starting_save():
    from skieo_creator import builder
    names = builder.Names('nativeosm_0001')
    native = builder.entry_spec(names, 'Test', 'Alps', {}, native_resort=object())
    assert ['InitialSave', 'initial_save'] in native['set']
    assert ['TutorialInitialSave', 'initial_save'] in native['set']
    assert ['bIsTutorialMap', 'hex', '00'] in native['set']
    original = builder.entry_spec(names, 'Test', 'Alps', {})
    assert ['InitialSave', 'initial_save'] not in original['set']
    assert ['bIsTutorialMap', 'hex', '01'] in original['set']


def test_legacy_migration_is_reversible_and_checks_identity(tmp_path):
    import json
    mods = tmp_path/'game/SkiResortSimulator/Binaries/Win64/ue4ss/Mods'
    old = mods/'SkiEO-real-resort-campiglio-native-osm'
    old.mkdir(parents=True)
    (old/'enabled.txt').write_bytes(b'')
    (old/'custom-mod.json').write_text(json.dumps({'id': 'other', 'runtime': 'ue4ss'}))
    output = tmp_path/'output'
    with pytest.raises(ValueError, match='identity'):
        retire_legacy_overlay(tmp_path/'game', output)
    assert old.is_dir()
    other = mods/'Other'; other.mkdir(); (other/'enabled.txt').write_bytes(b'')
    (old/'custom-mod.json').write_text(json.dumps({'id': 'real-resort.campiglio-native-osm', 'runtime': 'ue4ss'}))
    retire_legacy_overlay(tmp_path/'game', output)
    assert not old.exists()
    backup = Path(json.loads((output/'legacy-overlay-migration.json').read_text())['backup'])
    assert (backup/'enabled.txt').is_file()
    assert (other/'enabled.txt').is_file()
    retire_legacy_overlay(tmp_path/'game', output)


@pytest.fixture
def native_assets():
    template, base, scene, executable = [os.environ.get(k) for k in ('SKIEO_NATIVE_TEMPLATE', 'SKIEO_NATIVE_BASE', 'SKIEO_NATIVE_SCENE', 'SKIEO_CREATOR_GAME_EXE')]
    if not all((template, base, scene, executable)):
        pytest.skip('Supply local stock native assets and a processed OSM scene; game assets are never shipped')
    import json
    from skieo_creator.osm.native_generation import verify, contract
    proof = verify(contract()['game_build'], Path(executable).resolve().parents[3])
    return Path(template).read_bytes(), Path(base).read_bytes(), json.loads(Path(scene).read_text()), proof


@pytest.mark.parametrize('lifts,pistes', [(True, True), (True, False), (False, True)])
def test_native_bake_preserves_base_and_independent_categories(native_assets, lifts, pistes):
    stock, base, scene, proof = native_assets
    scene = copy.deepcopy(scene)
    scene['lifts'] = scene['lifts'][:1] if lifts else []
    scene['pistes'] = scene['pistes'][:1] if pistes else []
    a, b = (scene['lifts'][0]['terminals_m'] if lifts else ((0, 0), (100, 100)))
    dx, dy = b[0]-a[0], b[1]-a[1]
    sample = lambda x, y: 1000+500*((x-a[0])*dx+(y-a[1])*dy)/(dx*dx+dy*dy)
    resort = ns.make_resort(scene, stock, sample, 123, native_generation=proof)
    gvas = ns.root_gvas(base)
    start, original, tail = ns.root_fields(gvas)
    baked = resort.apply(gvas)
    at, result, ending = ns.root_fields(baked)
    assert baked[:at] == gvas[:start] and ending == tail
    for field in original:
        if field.name not in ('HubData', 'StaffData', 'TrailMapData'):
            assert ns.one(result, field.name).encode() == field.encode()
    original_trails = ns.fields(ns.one(original, 'TrailMapData').data)
    baked_trails = ns.fields(ns.one(result, 'TrailMapData').data)
    assert ns.one(baked_trails, 'WorldBounds') == ns.one(original_trails, 'WorldBounds')
    assert ns.one(baked_trails, 'NavRngSeed') == ns.one(original_trails, 'NavRngSeed')
    assert len(ns.structs_array(ns.one(result, 'Lifts'))) == int(lifts)
    assert struct.unpack_from('<2i', ns.one(baked_trails, 'RegisteredTrails').data) == (0, int(pistes))
    original_staff = ns.fields(ns.one(original, 'StaffData').data)
    baked_staff = ns.fields(ns.one(result, 'StaffData').data)
    for field in original_staff:
        if field.name != 'Roster':
            assert ns.one(baked_staff, field.name) == field
    assert len(ns.structs_array(ns.one(baked_staff, 'Roster'))) == 2*int(lifts)
    assert resort.report['runtime_import_mod'] is False
    assert 'startup' not in resort.scene
    if lifts:
        with pytest.raises(ValueError, match='not overwritten'):
            resort.apply(baked)
        with pytest.raises(ValueError, match='terrain sample'):
            ns.make_resort(scene, stock, lambda x,y: float('nan'), 123, native_generation=proof)


def test_native_obstacle_clearance_preserves_remaining_records():
    from skieo_creator import trees
    table = trees.encode_table([('DA_TreeSpecies_Pine', 0, 0, 'TestMesh')])
    # Landscape corner coordinates in mm, world coordinates in metres.
    records = [struct.pack('<3i', x, y, 12345)+b'\x12'*6+b'\0\0'
               for x, y in ((100000, 100000), (150000, 100000), (200000, 200000))]
    scene = {'pistes': [{'points_m': [(0, 0), (60, 0)], 'width_m': 30}], 'lifts': []}
    resort = ns.NativeResort({}, scene, {})
    remaining, counts = ns.clear_forest(b''.join(records), table, 100, resort)
    assert remaining == records[2]
    assert counts == {'Pine': 1}
    assert resort.report['cleared_trees'] == 2



def test_unverified_native_generation_cannot_be_exported():
    with pytest.raises(ValueError, match='verified local game contract'):
        ns.make_resort({}, b'', None, 0)


@pytest.mark.parametrize('model', ('aerial-tram', 'gondola', 'fixed-chair1', 'fixed-chair2', 'fixed-chair4', 'high-speed-quad', 'high-speed-six'))

def test_all_exported_models_mark_supports_for_native_preparation(native_assets, model):
    stock, base, scene, proof = native_assets
    scene = copy.deepcopy(scene)
    lift = scene['lifts'][0]
    from skieo_creator.osm.catalog import CATALOG
    lift['extensions']['skieo']['class'] = next(item['class'] for item in CATALOG if item['id'] == model)
    scene['lifts'] = [lift]
    scene['pistes'] = []
    a, b = lift['terminals_m']
    dx, dy = b[0]-a[0], b[1]-a[1]
    sample = lambda x, y: 1000+500*((x-a[0])*dx+(y-a[1])*dy)/(dx*dx+dy*dy)
    resort = ns.make_resort(scene, stock, sample, 123, native_generation=proof)
    baked = resort.apply(ns.root_gvas(base))
    _, fields, _ = ns.root_fields(baked)
    output = ns.structs_array(ns.one(fields, 'Lifts'))[0]
    assert ns.one(output, 'Towers').data == struct.pack('<i', 0)
    assert len(ns.structs_array(ns.one(output, 'Waypoints'))) == 2
    assert resort.report['authored_towers'] == 0
    assert resort.report['runtime_towers'] is None
    assert resort.report['tower_generation'] == 'native-after-terrain-ready'
    config = resort.report['support_preparation']
    assert config['target_package'] == scene['map']['frame']['package']
    identifier = ''.join(f"{lift['extensions']['skieo']['guid'][k] & 0xffffffff:08x}" for k in ('A','B','C','D'))
    definition = config['lifts'][identifier]
    assert definition['class'] == lift['extensions']['skieo']['class']
    assert definition['name'] == lift['name']
    assert definition['pending_name'].startswith('Preparing lift')
    assert [p[2] for p in definition['probes_cm']] == pytest.approx([100000,125000,150000])
    base_data = ns.fields(ns.one(output, 'BaseData').data)
    assert ns.one(base_data, 'DisplayName').data == ns.text(definition['pending_name'])



def test_soft_class_path_keeps_native_empty_subobject_bytes():
    from skieo_creator.osm.catalog import CATALOG
    path = next(item['class'] for item in CATALOG if item['id'] == 'gondola')
    package, name = path.rsplit('.', 1)
    # Native layout observed in stock and game-written lift saves. The final
    # zero length is deliberately distinct from a one-byte NUL subobject.
    wire = ns.fstr(package)+ns.fstr(name)+bytes(4)
    tree = ('StructProperty', (('SoftClassPath', (('/Script/CoreUObject', ()),)),))
    field = ns.Field('LiftClassPath', tree, b'', 8, 0, wire)
    assert ns.soft_class_path(field, path) == wire
    malformed = field.value(wire[:-4]+ns.fstr(''))
    with pytest.raises(ValueError, match='subobject encoding'):
        ns.soft_class_path(malformed, path)


def test_class_paths_match_stock_native_records_byte_for_byte(native_assets):
    stock, _, _, proof = native_assets
    _, root, _ = ns.root_fields(ns.root_gvas(stock))
    for lift in ns.structs_array(ns.one(root, 'Lifts')):
        field = ns.one(lift, 'LiftClassPath')
        package, offset = ns.read_fstr(field.data, 0)
        name, _ = ns.read_fstr(field.data, offset)
        assert ns.soft_class_path(field, package+'.'+name) == field.data
