"""Pinned public SkiEO Mod 1.29.0 lift catalog; no game assets or runtime mod."""
import hashlib
import json
from .. import paths

PROVENANCE = json.loads(paths.resource('data', 'osm', 'provenance.json').read_text(encoding='utf-8'))
raw = paths.resource('data', 'osm', 'native-lifts-1.29.0.json').read_bytes()
if hashlib.sha256(raw).hexdigest() != PROVENANCE['catalog_subset_sha256']:
    raise ValueError('Bundled native lift catalog hash differs from provenance')
_document = json.loads(raw)
CATALOG = _document['catalog']
DEFAULTS = _document['defaults']
