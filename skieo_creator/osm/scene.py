"""Map scene sources, compiled to grounded native placeables at build time."""
from __future__ import annotations
import hashlib
import math
import re
import struct
# Adapted from SkiEO Mod 1.29.0 (BSD-3-Clause; see data/osm/LICENSE.SkiEO-Mod.txt).
# Only imports/error adaptation changed; compile_scene remains upstream code.
from .catalog import CATALOG, DEFAULTS
ManifestError = ValueError

DEFAULT_NAMES = {item['class']: item['name'] for item in CATALOG}
for item in DEFAULTS['objects']:
    field=item.get('fields',{}).get('DisplayName') if isinstance(item.get('fields'),dict) else None
    if field and field.get('default'): DEFAULT_NAMES[item['class']]=field['default']


def compile_scene(document, manifest):
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('objects'), list):
        raise ManifestError('Scene requires version: 1 and an objects array')
    if len(document['objects']) > 1024: raise ManifestError('A scene supports at most 1024 objects')
    terrain = manifest['terrain']; source = terrain['source']
    extent = [(v-1)*source['spacing_meters'] for v in source['vertices']]
    origin = terrain['origin_centimeters']
    result, seen, terminals = [], set(), []
    def guid(key):
        values=struct.unpack('<4i',hashlib.sha256((manifest['id']+'\0'+key).encode()).digest()[:16])
        return dict(zip(('A','B','C','D'),values))
    def number(value): return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
    def position(value):
        if not isinstance(value,list) or len(value)!=2 or not all(number(v) for v in value): raise ManifestError('Scene positions require two finite metre coordinates')
        if any(abs(value[i])>extent[i]/2 for i in (0,1)): raise ManifestError('Scene object is outside the terrain')
        return [origin[i]+extent[i]*50+value[i]*100 for i in (0,1)]
    for item in document['objects']:
        if not isinstance(item,dict) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}',str(item.get('id',''))): raise ManifestError('Invalid scene object ID')
        if item['id'] in seen: raise ManifestError('Duplicate scene object ID')
        seen.add(item['id'])
        if not re.fullmatch(r'/Game/[A-Za-z0-9_/]+\.[A-Za-z0-9_]+_C',str(item.get('class',''))): raise ManifestError('Scene requires a full game Blueprint class path')
        if item.get('kind') not in ('building','lift','decoration','park','snowblower','system'): raise ManifestError('Invalid scene object kind')
        if not isinstance(item.get('name',''),str) or len(item.get('name',''))>256: raise ManifestError('Invalid scene object name')
        for key in ('rotation_degrees','height_offset_meters'):
            if not number(item.get(key,0)) or abs(item.get(key,0))>36000: raise ManifestError('Invalid scene rotation or height offset')
        compiled={'id':item['id'],'class':item['class'],'kind':item['kind'],'name':item.get('name',item['id']),
                  'position_cm':position(item.get('position')),'yaw':item.get('rotation_degrees',0),'offset_cm':item.get('height_offset_meters',0)*100,'guid':guid('object:'+item['id'])}
        if 'variant' in item:
            if item['kind']=='lift' or not isinstance(item['variant'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',item['variant']):
                raise ManifestError('Scene variants require a building variant identifier')
            compiled['variant']=item['variant']
        if not item.get('name') or item.get('name') == DEFAULT_NAMES.get(item['class']):
            compiled['name']=''
        terminals.append((compiled,'hub',item['position']))
        if item['kind']=='lift':
            compiled['end_position_cm']=position(item.get('end_position'))
            if math.dist(item['position'],item['end_position'])<2: raise ManifestError('Lift terminals must be at least two metres apart')
            terminals.append((compiled,'top_hub',item['end_position']))
        result.append(compiled)
    # Nearby facilities and lift terminals share a native hub. IDs are stable
    # across builds and independent of the order of source objects.
    parents=list(range(len(terminals)))
    def root(i):
        while parents[i]!=i: parents[i]=parents[parents[i]];i=parents[i]
        return i
    for i,(_,_,p) in enumerate(terminals):
        for j in range(i):
            if math.dist(p,terminals[j][2])<=80: parents[root(i)]=root(j)
    groups={}
    for i,(item,key,_) in enumerate(terminals): groups.setdefault(root(i),[]).append(item['id']+':'+key)
    for i,(item,key,_) in enumerate(terminals): item[key]=guid('hub:'+min(groups[root(i)]))
    return result
