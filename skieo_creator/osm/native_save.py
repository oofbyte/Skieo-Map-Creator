"""Bake a native starting save using schemas from the installed stock scenario.

This is the Creator's tagged GVAS format, not a SkiEO Mod manifest. Game bytes
and templates are read locally at build time and must remain private. Actors are baked here. A separate geometry initializer prepares explicitly
marked lifts after terrain readiness; no actor import or user save file edits.
"""
from __future__ import annotations

import copy
import hashlib
import math
import struct
from dataclasses import dataclass, replace

MAX_BYTES = 64 * 1024**2


def fstr(value: str) -> bytes:
    if '\0' in value:
        raise ValueError('NUL in native FString')
    if value.isascii():
        raw = value.encode('ascii') + b'\0'
        return struct.pack('<i', len(raw)) + raw
    raw = value.encode('utf-16-le') + b'\0\0'
    return struct.pack('<i', -len(raw)//2) + raw


def read_fstr(blob: bytes, offset: int) -> tuple[str, int]:
    if offset < 0 or offset+4 > len(blob):
        raise ValueError('Truncated native FString')
    count = struct.unpack_from('<i', blob, offset)[0]
    size = count if count >= 0 else -count*2
    end = offset+4+size
    if size > MAX_BYTES or end > len(blob):
        raise ValueError('Invalid native FString length')
    if count == 0:
        return '', end
    raw = blob[offset+4:end]
    suffix = b'\0' if count > 0 else b'\0\0'
    if not raw.endswith(suffix):
        raise ValueError('Unterminated native FString')
    return raw[:-len(suffix)].decode('utf-8' if count > 0 else 'utf-16-le'), end


def read_tree(blob: bytes, offset: int, depth: int = 0):
    if depth > 10:
        raise ValueError('Native property type tree too deep')
    name, offset = read_fstr(blob, offset)
    if offset+4 > len(blob):
        raise ValueError('Truncated native type tree')
    count = struct.unpack_from('<i', blob, offset)[0]
    if not 0 <= count <= 8:
        raise ValueError('Invalid native type tree')
    children = []
    offset += 4
    for _ in range(count):
        child, offset = read_tree(blob, offset, depth+1)
        children.append(child)
    return (name, tuple(children)), offset


@dataclass(frozen=True)
class Field:
    name: str
    tree: tuple
    prefix: bytes  # Name + complete observed type tree; never synthesized.
    flags: int
    index: int
    data: bytes

    def encode(self) -> bytes:
        extra = struct.pack('<i', self.index) if self.flags & 1 else b''
        return self.prefix+struct.pack('<iB', len(self.data), self.flags)+extra+self.data

    def value(self, data: bytes) -> 'Field':
        return replace(self, data=data)

    def boolean(self, value: bool) -> 'Field':
        if self.tree[0] != 'BoolProperty' or self.data:
            raise ValueError('Native BoolProperty schema changed')
        return replace(self, flags=(self.flags & ~0x10) | (0x10 if value else 0))


def parse(blob: bytes, offset: int = 0) -> tuple[list[Field], int]:
    result = []
    for _ in range(10000):
        start = offset
        name, offset = read_fstr(blob, offset)
        if name == 'None':
            return result, offset
        if not name:
            raise ValueError('Empty native property name')
        tree, offset = read_tree(blob, offset)
        prefix = blob[start:offset]
        if offset+5 > len(blob):
            raise ValueError('Truncated native property header')
        size, flags = struct.unpack_from('<iB', blob, offset)
        offset += 5
        # Observed UE5.6+ tag flags: array index, native serializer, bool true.
        if flags & ~0x19 or not 0 <= size <= MAX_BYTES:
            raise ValueError('Unsupported native property tag layout')
        index = 0
        if flags & 1:
            if offset+4 > len(blob):
                raise ValueError('Truncated native array index')
            index = struct.unpack_from('<i', blob, offset)[0]
            offset += 4
            if index < 0:
                raise ValueError('Invalid native array index')
        if offset+size > len(blob):
            raise ValueError('Truncated native property data')
        result.append(Field(name, tree, prefix, flags, index, blob[offset:offset+size]))
        offset += size
    raise ValueError('Too many native properties')


def encode(fields: list[Field]) -> bytes:
    return b''.join(f.encode() for f in fields)+fstr('None')


def fields(blob: bytes) -> list[Field]:
    result, end = parse(blob)
    if end != len(blob):
        raise ValueError('Unexpected bytes after native struct')
    return result


def one(values: list[Field], name: str) -> Field:
    result = [v for v in values if v.name == name and v.index == 0]
    if len(result) != 1:
        raise ValueError('Native schema requires exactly one '+name)
    return result[0]


def put(values: list[Field], name: str, data: bytes) -> list[Field]:
    one(values, name)
    return [v.value(data) if v.name == name and v.index == 0 else v for v in values]


def boolean(values: list[Field], name: str, value: bool) -> list[Field]:
    one(values, name)
    return [v.boolean(value) if v.name == name else v for v in values]


def structs_array(field: Field) -> list[list[Field]]:
    if field.tree[0] != 'ArrayProperty' or field.tree[1][0][0] != 'StructProperty' or len(field.data) < 4:
        raise ValueError('Native struct array schema changed')
    count = struct.unpack_from('<i', field.data)[0]
    if not 0 <= count <= 10000:
        raise ValueError('Invalid native struct array count')
    result, offset = [], 4
    for _ in range(count):
        item, offset = parse(field.data, offset)
        result.append(item)
    if offset != len(field.data):
        raise ValueError('Unexpected bytes after native struct array')
    return result


def array(values: list[list[Field]]) -> bytes:
    return struct.pack('<i', len(values))+b''.join(encode(v) for v in values)


def root_gvas(uexp: bytes) -> bytes:
    if len(uexp) < 10:
        raise ValueError('Truncated native initial-save asset')
    length = struct.unpack_from('<i', uexp, 2)[0]
    if not 0 < length <= MAX_BYTES or 6+length > len(uexp):
        raise ValueError('Invalid native initial-save byte array')
    blob = uexp[6:6+length]
    if blob[:4] != b'GVAS':
        raise ValueError('Initial-save asset does not contain native GVAS')
    return blob


def root_fields(gvas: bytes):
    # The same root anchor used by the installed Creator's gvas_edit.walk.
    at = gvas.find(b'SlotName\0')
    if at < 4:
        raise ValueError('Native GVAS root anchor missing')
    start = at-4
    result, end = parse(gvas, start)
    return start, result, gvas[end:]


def guid(value: dict) -> bytes:
    if set(value) != {'A', 'B', 'C', 'D'} or any(type(v) is not int or not -2**31 <= v < 2**31 for v in value.values()):
        raise ValueError('Invalid native GUID')
    return struct.pack('<4i', *(value[k] for k in ('A', 'B', 'C', 'D')))


def new_guid(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()[:16]


def text(value: str) -> bytes:
    # Culture-invariant FText, also emitted by Creator install.apply_entry and
    # present in native saves written after SetInstanceName/SetTrailDisplayName.
    return b'\x02\0\0\0\xff\x01\0\0\0'+fstr(value)


def vector(point) -> bytes:
    if len(point) != 3 or not all(math.isfinite(v) for v in point):
        raise ValueError('Non-finite native position')
    return struct.pack('<3d', *point)


def soft_class_path(template: Field, path: str) -> bytes:
    """Keep the native empty subobject encoding; it is not an FString terminator.

    The stock/game-written SoftClassPath has two archive FNames followed by
    a zero-length UTF-8 subobject path. FString('') with length 1 writes a NUL
    subobject byte, changes the reference and makes the game fall back to LiftActor.
    """
    if template.tree[0] != 'StructProperty' or template.tree[1][0][0] != 'SoftClassPath':
        raise ValueError('Native class-path property type changed')
    _, offset = read_fstr(template.data, 0)
    _, offset = read_fstr(template.data, offset)
    empty_subpath = template.data[offset:]
    if empty_subpath != bytes(4):
        raise ValueError('Native class-path subobject encoding changed')
    package, separator, object_name = path.rpartition('.')
    if not separator or not package.startswith('/Game/') or not object_name.endswith('_C') or not path.isascii():
        raise ValueError('Invalid catalog Blueprint class path: '+path)
    return fstr(package)+fstr(object_name)+empty_subpath


def enum(field: Field) -> str:
    value, end = read_fstr(field.data, 0)
    if end != len(field.data):
        raise ValueError('Native enum encoding changed')
    return value


@dataclass
class Templates:
    lifts: Field
    lift: list[Field]
    waypoint: list[Field]
    trail_map: Field
    trail: list[Field]
    curve: list[Field]
    hub_data: Field
    hub_name: list[Field]
    roster: Field
    operators: list[list[Field]]

    @classmethod
    def load(cls, uexp: bytes) -> 'Templates':
        _, root, _ = root_fields(root_gvas(uexp))
        lifts = one(root, 'Lifts')
        lift = structs_array(lifts)[0]
        waypoint = structs_array(one(lift, 'Waypoints'))[0]
        tower_type = ('ArrayProperty', (('StructProperty', (('LiftTower', (('/Script/SkiResortSimulator', ()),)),)),))
        if one(lift, 'Towers').tree != tower_type:
            raise ValueError('Native Towers array type changed')
        trail_map = one(root, 'TrailMapData')
        registered = one(fields(trail_map.data), 'RegisteredTrails')
        removed, count = struct.unpack_from('<2i', registered.data)
        if removed or not 1 <= count <= 4096:
            raise ValueError('Stock trail map layout changed')
        trails, offset = [], 8
        for _ in range(count):
            key = struct.unpack_from('<i', registered.data, offset)[0]
            item, offset = parse(registered.data, offset+4)
            if struct.unpack('<i', one(item, 'TrailId').data)[0] != key:
                raise ValueError('Stock native trail key mismatch')
            if enum(one(item, 'Shape')) == 'ETrailShape::Corridor':
                trails.append(item)
        if offset != len(registered.data) or not trails:
            raise ValueError('Stock native trail map is incomplete')
        trail = trails[0]
        curve = fields(one(trail, 'Curve').data)
        hub_data = one(root, 'HubData')
        hub_name = structs_array(one(fields(hub_data.data), 'HubNames'))[0]
        roster = one(fields(one(root, 'StaffData').data), 'Roster')
        operators = [v for v in structs_array(roster) if enum(one(v, 'Role')) == 'EStaffRole::Liftie']
        if not operators:
            raise ValueError('Stock native operator template missing')
        # Required fields are checked before any output or game packaging.
        for name in ('LiftClassPath', 'BaseData', 'Waypoints', 'Towers', 'TopHubId'):
            one(lift, name)
        for name in ('StartPoint', 'ControlPoint', 'EndPoint', 'CorridorWidth'):
            one(curve, name)
        return cls(lifts, lift, waypoint, trail_map, trail, curve, hub_data, hub_name, roster, operators)


@dataclass
class NativeResort:
    replacements: dict[str, Field]
    scene: dict
    report: dict

    def apply(self, gvas: bytes) -> bytes:
        start, root, tail = root_fields(gvas)
        if any(v.name == 'Lifts' for v in root):
            raise ValueError('Creator starting template already contains lifts; not overwritten')
        original_staff = one(root, 'StaffData')
        staff = fields(original_staff.data)
        roster = self.replacements['Roster']
        staff = [v for v in staff if v.name != 'Roster'] + [roster]
        original_trails = one(root, 'TrailMapData')
        trail_fields = fields(original_trails.data)
        replacements = fields(self.replacements['TrailMapData'].data)
        for name in ('RegisteredTrails', 'CurrentTrailId'):
            replacement = one(replacements, name)
            trail_fields = [v for v in trail_fields if v.name != name] + [replacement]
        merged = dict(self.replacements)
        merged['TrailMapData'] = original_trails.value(encode(trail_fields))
        original_hubs = one(root, 'HubData')
        hub_fields = fields(original_hubs.data)
        hub_array = one(hub_fields, 'HubNames')
        names = structs_array(hub_array)
        additions = structs_array(one(fields(self.replacements['HubData'].data), 'HubNames'))
        identifiers = {one(v, 'HubId').data for v in names}
        if any(one(v, 'HubId').data in identifiers for v in additions):
            raise ValueError('OSM hub identifier collides with starting building')
        merged['HubData'] = original_hubs.value(encode(put(hub_fields, 'HubNames', array(names+additions))))
        root = [v.value(encode(staff)) if v.name == 'StaffData' else merged.get(v.name, v) for v in root]
        root.append(self.replacements['Lifts'])
        result = gvas[:start]+encode(root)+tail
        _, check, _ = root_fields(result)
        if len(structs_array(one(check, 'Lifts'))) != self.report['lifts']:
            raise ValueError('Native lift count changed during save assembly')
        trail_fields = fields(one(check, 'TrailMapData').data)
        if struct.unpack_from('<2i', one(trail_fields, 'RegisteredTrails').data) != (0, self.report['pistes']):
            raise ValueError('Native trail count changed during save assembly')
        self.report['base_gvas_sha256'] = hashlib.sha256(gvas).hexdigest()
        self.report['initial_gvas_sha256'] = hashlib.sha256(result).hexdigest()
        return result


def make_resort(scene: dict, template_uexp: bytes, sample_native_m, vertical_offset_m: float, native_generation=None) -> NativeResort:
    """Generate only native records; the surrounding starting save stays Creator-owned."""
    if not native_generation or native_generation.get('local_executable_verified') is not True:
        raise ValueError('Native tower generation requires a verified local game contract')
    from .catalog import DEFAULTS
    template = Templates.load(template_uexp)
    defaults = {v['class']: {k: d['default'] for k, d in v['fields'].items()} for v in DEFAULTS['objects']}
    package = scene['map']['frame']['package']
    lifts, roster, hubs, trails = [], [], {}, []
    preparation = {}

    def position(point):
        x, y = point
        z = float(sample_native_m(x, y))
        if not math.isfinite(z):
            raise ValueError('Invalid native terrain sample; no filling performed')
        return (x*100, y*100, z*100)

    for lift in scene['lifts']:
        d = lift['extensions']['skieo']
        if d['class'] not in defaults:
            raise ValueError('Lift class is absent from installed native defaults')
        bottom, top = [position(p) for p in lift['terminals_m']]
        if bottom[2] >= top[2]:
            raise ValueError('Native lift terminal elevation order is invalid')
        base = fields(one(template.lift, 'BaseData').data)
        transform = fields(one(base, 'Transform').data)
        transform = put(transform, 'Translation', vector(bottom))
        transform = put(transform, 'Rotation', struct.pack('<4d', 0, 0, 0, 1))
        transform = put(transform, 'Scale3D', vector((1, 1, 1)))
        base = put(base, 'Transform', encode(transform))
        base = put(base, 'Id', guid(d['guid']))
        base = put(base, 'HubId', guid(d['hub']))
        pending = 'Preparing lift: '+lift['name']
        if len(pending) > 256:
            pending = 'Preparing lift'
        base = put(base, 'DisplayName', text(pending))
        midpoint = position(tuple((a+b)/2 for a,b in zip(*lift['terminals_m'])))
        preparation[''.join(f"{d['guid'][k] & 0xffffffff:08x}" for k in ('A','B','C','D'))] = {
            'name': lift['name'], 'pending_name': pending, 'class': d['class'],
            'terminals_cm': [bottom, top], 'probes_cm': [bottom, midpoint, top]}
        base = put(base, 'ConstructionCost', struct.pack('<i', 0))
        locks = fields(one(base, 'DesignLocks').data)
        for name, value in (('bNoDemolish', False), ('bNoMove', False), ('bNoRefund', True)):
            locks = boolean(locks, name, value)
        base = put(base, 'DesignLocks', encode(locks))
        item = put(template.lift, 'BaseData', encode(base))
        item = put(item, 'LiftClassPath', soft_class_path(one(template.lift, 'LiftClassPath'), d['class']))
        waypoints = []
        for p in (bottom, top):
            waypoint = put(template.waypoint, 'Position', vector(p))
            waypoints.append(boolean(boolean(waypoint, 'bIsTerminal', True), 'bIsAngleStation', False))
        item = put(item, 'Waypoints', array(waypoints))
        # The verified native LoadFromSaveData branch generates support/cable
        # geometry when this array is empty, but the observed early load produces
        # zero ground heights. The marked actor must be prepared after readiness.
        item = put(item, 'Towers', array([]))
        item = put(item, 'TopHubId', guid(d['top_hub']))
        for name in ('Wear', 'ChairAnimPhaseOffset'):
            item = put(item, name, struct.pack('<f', 0))
        item = put(item, 'Condition', struct.pack('<f', 100))
        for name in ('bManualRepairActive', 'bPlayerAuthoredQueueLine', 'bQueueCapacityLimited',
                     'bTopQueueCapacityLimited', 'bAllowDownloading', 'bPlayerAuthoredTopQueueLine'):
            item = boolean(item, name, False)
        for f in item:
            if f.name in ('BuiltSeasonIndex', 'PlayerLiftPrice', 'TotalGuestRidersAllTime', 'LastRidersHour', 'LastRidersDay',
                          'RidersThisHour', 'RidersToday', 'LastRidersMonth', 'RidersThisMonth'):
                item = put(item, f.name, struct.pack('<i', 0))
        for name in ('QueueLinePoints', 'TopQueueLinePoints', 'QueueRopeLiftCm', 'TopQueueRopeLiftCm'):
            item = put(item, name, struct.pack('<i', 0))
        riders = one(item, 'RidersByLifetimeSkill')
        item = put(item, riders.name, struct.pack('<2i', 0, 0))
        item = put(item, 'OpenHour', struct.pack('<f', 8))
        item = put(item, 'CloseHour', struct.pack('<f', 17))
        lifts.append(item)
        for terminal, (key, p) in enumerate((('hub', bottom), ('top_hub', top))):
            identifier = guid(d[key])
            hubs.setdefault(identifier, {'point': (p[0]/100, p[1]/100), 'name': lift['name']})
            employee = template.operators[len(roster) % len(template.operators)]
            employee = put(employee, 'Id', new_guid(package+':staff:'+lift['id']+':'+str(terminal)))
            employee = put(employee, 'HomeBuildingId', guid(d['guid']))
            for name in ('AssignedEventId', 'BreakVenueId'):
                employee = put(employee, name, bytes(16))
            for name in ('XP', 'PromotionsApplied', 'YearsOfService', 'LifetimeWagesPaid', 'LifetimeRidesDispatched',
                         'LifetimeEventsResolved', 'LifetimeTrailsGroomed', 'LifetimeHappinessEncounters'):
                employee = put(employee, name, struct.pack('<i', 0))
            employee = put(employee, 'Stamina', struct.pack('<f', 100))
            employee = put(employee, 'BreakTimeRemaining', struct.pack('<f', 0))
            employee = put(employee, 'WorldPosition', vector(p))
            employee = put(employee, 'Rotation', vector((0, 0, 0)))
            for name, value in (('bAssignedToTopTerminal', terminal == 1), ('bHasWorldPosition', True),
                                ('bInsideHomeBuilding', False), ('bInsideBreakVenue', False),
                                ('bIsHurrying', False), ('bPromotionAvailable', False)):
                employee = boolean(employee, name, value)
            roster.append(employee)

    for index, piste in enumerate(scene['pistes']):
        d = piste['extensions']['skieo']
        points = [position(p) for p in piste['points_m']]
        curves = []
        for a, b in zip(points, points[1:]):
            curve = put(template.curve, 'StartPoint', vector(a))
            curve = put(curve, 'EndPoint', vector(b))
            curve = put(curve, 'ControlPoint', vector(tuple((x+y)/2 for x, y in zip(a, b))))
            curves.append(put(curve, 'CorridorWidth', struct.pack('<f', d['width'])))
        if not curves:
            raise ValueError('Native trail has no complete curve')
        item = put(template.trail, 'TrailId', struct.pack('<i', index))
        item = put(item, 'DisplayName', text(piste['name']))
        ratings = ('Beginner', 'Intermediate', 'Expert', 'Extreme')
        if type(d['difficulty']) is not int or not 0 <= d['difficulty'] < len(ratings):
            raise ValueError('Invalid native trail difficulty')
        item = put(item, 'PlayerRatedDifficulty', fstr('ETrailDifficulty::'+ratings[d['difficulty']]))
        item = put(item, 'Curve', encode(curves[0]))
        item = put(item, 'AdditionalCurves', array(curves[1:]))
        item = put(item, 'BowlOutline', struct.pack('<i', 0))
        item = put(item, 'BowlCenter', vector((0, 0, 0)))
        item = put(item, 'BowlRadius', struct.pack('<f', 0))
        item = put(item, 'MidpointPosition', vector(points[len(points)//2]))
        item = put(item, 'Length', struct.pack('<f', sum(math.dist(a, b) for a, b in zip(points, points[1:]))))
        slope = max(math.degrees(math.atan2(abs(b[2]-a[2]), math.hypot(b[0]-a[0], b[1]-a[1]))) for a, b in zip(points, points[1:]))
        item = put(item, 'MaxSlopeAngle', struct.pack('<f', slope))
        item = boolean(boolean(boolean(item, 'bManuallyDisabled', False), 'bIsGroomed', True), 'bNightSkiing', False)
        item = [f.value(struct.pack('<i', 0)) if f.name in ('RunsThisMonth', 'LifetimeRunsBySkill', 'LastModifiedMonthIndex')
                else f.value(bytes(8)) if f.name in ('FirstCreatedAt', 'LastModifiedAt') else f for f in item]
        trails.append(item)

    trail_map = fields(template.trail_map.data)
    registered = struct.pack('<2i', 0, len(trails))+b''.join(struct.pack('<i', i)+encode(v) for i, v in enumerate(trails))
    trail_map = put(trail_map, 'RegisteredTrails', registered)
    trail_map = put(trail_map, 'CurrentTrailId', struct.pack('<i', len(trails)))
    # WorldBounds comes from the Creator's already transformed starting save,
    # not the stock scenario; merge that field in NativeResort.apply below.
    hub_names = []
    for identifier, hub in hubs.items():
        candidates = []
        for place in scene.get('places', []):
            distance = math.dist(hub['point'], place['point_m'])
            if distance <= (120 if place['kind'] == 'station' else 150):
                candidates.append((distance, place['id'], place['name']))
        name = min(candidates)[2] if candidates else hub['name']
        entry = put(template.hub_name, 'HubId', identifier)
        hub_names.append(put(entry, 'Name', text(name[:32])))
    hub_data = fields(template.hub_data.data)
    hub_data = put(hub_data, 'HubNames', array(hub_names))
    report = {'format': 'creator-native-starting-resort', 'version': 5, 'lifts': len(lifts),
              'pistes': len(trails), 'staff': len(roster), 'authored_towers': 0, 'runtime_towers': None,
              'tower_generation': 'native-after-terrain-ready', 'native_generation_contract': native_generation,
              'named_places': len(scene.get('places', [])), 'delivery': 'native InitialSaveAsset',
              'class_path_encoding': 'native SoftClassPath with zero-length subobject',
              'runtime_geometry_helper': bool(lifts),
              'support_preparation': {'format': 'creator-native-support-init', 'version': 1,
                                      'target_package': package, 'lifts': preparation},
              'runtime_import_mod': False, 'game_tested': False, 'fps_measured': False,
              'stock_template_sha256': hashlib.sha256(template_uexp).hexdigest(),
              'vertical_offset_m': vertical_offset_m,
              'limitations': ['Support count, positions, heights and cables are generated by the game, not surveyed OSM pylons',
                              'Native two-terminal geometry retains the existing 20 m maximum chord-deviation rule; original OSM route is retained in the report',
                              'Native load, passenger transport and save/reload require game validation']}
    scene = copy.deepcopy(scene)
    scene.pop('startup', None)
    scene['provenance']['vertical'] = 'Sampled from the exact encoded, edge-fitted native Creator grid; native Z in metres = source elevation minus the recorded vertical_offset_m. Source vertical datum unchanged.'
    scene['delivery'] = report
    return NativeResort({'Lifts': template.lifts.value(array(lifts)),
                         'TrailMapData': template.trail_map.value(encode(trail_map)),
                         'HubData': template.hub_data.value(encode(hub_data)),
                         'Roster': template.roster.value(array(roster))}, scene, report)


def clear_forest(recs: bytes, table: bytes, half_m: float, resort: NativeResort):
    """Remove complete generated obstacle records inside projected OSM corridors."""
    import numpy as np
    from skieo_creator import trees
    if len(recs) % trees.STRIDE or trees.STRIDE != 20:
        raise ValueError('Unsupported native obstacle record layout')
    records = np.frombuffer(recs, dtype=np.uint8).reshape(-1, trees.STRIDE)
    x = records[:, :4].copy().view('<i4').ravel()/1000-half_m
    y = records[:, 4:8].copy().view('<i4').ravel()/1000-half_m
    keep = np.ones(len(records), dtype=bool)
    routes = [(v['points_m'], v['width_m']/2+20) for v in resort.scene['pistes']]
    routes += [(v['terminals_m'], 20.0) for v in resort.scene['lifts']]
    for points, radius in routes:
        if not math.isfinite(radius) or radius <= 0:
            raise ValueError('Invalid native corridor width')
        for a, b in zip(points, points[1:]):
            dx, dy = b[0]-a[0], b[1]-a[1]
            length2 = dx*dx+dy*dy
            if length2 <= 0:
                raise ValueError('Zero-length native obstacle corridor')
            indices = np.nonzero(keep & (x >= min(a[0], b[0])-radius) & (x <= max(a[0], b[0])+radius)
                                 & (y >= min(a[1], b[1])-radius) & (y <= max(a[1], b[1])+radius))[0]
            fraction = np.clip(((x[indices]-a[0])*dx+(y[indices]-a[1])*dy)/length2, 0, 1)
            distance2 = (x[indices]-a[0]-fraction*dx)**2+(y[indices]-a[1]-fraction*dy)**2
            keep[indices[distance2 <= radius*radius]] = False
    result = records[keep]
    variants = result[:, 18:20].copy().view('<u2').ravel()
    entries = trees.table_entries(table)
    if len(variants) and int(variants.max()) >= len(entries):
        raise ValueError('Native obstacle variant outside species table')
    counts = {}
    for index, entry in enumerate(entries):
        count = int((variants == index).sum())
        if count:
            species = entry[0].removeprefix('DA_TreeSpecies_')
            counts[species] = counts.get(species, 0)+count
    resort.report['cleared_trees'] = int((~keep).sum())
    resort.report['clearance_m'] = {'trail_margin': 20, 'lift_radius': 20}
    return result.tobytes(), counts
