"""Acquire OSM for a Creator window and compile native objects without a reviewed project.

Raw HTTP bytes are content addressed and immutable. All geometry uses the
native map's projected metre frame. Missing geometry is always an error.
"""
from __future__ import annotations

import hashlib
import json
import math
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

from .io import write_json, sha256
from .native_map import NativeMapFrame, TransverseMercator

log = logging.getLogger(__name__)
CACHE_MAX_AGE = timedelta(hours=24)

ENDPOINTS = ("https://overpass-api.de/api/interpreter",
             "https://overpass.private.coffee/api/interpreter",
             "https://maps.mail.ru/osm/tools/overpass/api/interpreter")
MAX_BYTES = 128 * 1024**2
# Same verified mapping as enrichment.prepare_pistes; do not infer a new
# rating scale from native enum names.
RATINGS = {"novice": 0, "easy": 0, "intermediate": 1, "advanced": 2, "expert": 2}


def osm_name(tags: dict, fallback: str = "") -> tuple[str, str]:
    """Keep actual OSM names, including Unicode; do not translate geography."""
    preferred = ("name", "official_name", "name:en", "short_name", "piste:name")
    localized = tuple(sorted(k for k in tags if k.startswith("name:") and k not in preferred))
    for key in (*preferred, *localized, "ref"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key
    return fallback, "generated-id" if fallback else "missing"


def query_for(spec: dict) -> str:
    lat, lon, size = (float(spec[k]) for k in ("lat", "lon", "size_m"))
    if not all(math.isfinite(v) for v in (lat, lon, size)) or abs(lat) >= 80 or not 0 < size <= 20000:
        raise ValueError("OSM area is outside the supported map domain")
    # Conservative geographic envelope of the projected square; actual
    # inclusion is subsequently checked in metres, never in angular units.
    radius = size / math.sqrt(2)
    delta_lat = radius / 100000
    delta_lon = radius / (100000 * math.cos(math.radians(abs(lat)+delta_lat)))
    west, east = lon-delta_lon, lon+delta_lon
    if not -180 <= west < east <= 180:
        raise ValueError("OSM windows crossing the antimeridian are unsupported")
    box = f"({lat-delta_lat:.9f},{west:.9f},{lat+delta_lat:.9f},{east:.9f})"
    selectors = []
    if spec.get("osm_lifts"):
        selectors.append(f'way["aerialway"]{box};')
    if spec.get("osm_pistes"):
        selectors.extend((f'way["piste:type"="downhill"]{box};',
                          f'relation["piste:type"="downhill"]{box};',
                          f'relation["route"="piste"]{box};'))
    if not selectors:
        raise ValueError("No OSM category selected")
    selectors.extend((f'node["aerialway"="station"]{box};', f'node["place"]{box};',
                      f'node["natural"="peak"]{box};', f'node["mountain_pass"="yes"]{box};'))
    return '[out:json][timeout:45];(' + ''.join(selectors) + ');(._;>>;);out body;'


def validate_snapshot(raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise ValueError("OSM response exceeds 128 MiB")
    data = json.loads(raw)
    if not isinstance(data, dict) or "remark" in data or not isinstance(data.get("elements"), list):
        raise ValueError("OSM download is incomplete; please retry")
    seen = set()
    for item in data["elements"]:
        if item.get("type") not in ("node", "way", "relation") or type(item.get("id")) is not int:
            raise ValueError("Invalid OSM element")
        key = (item["type"], item["id"])
        if key in seen:
            raise ValueError("Duplicate OSM element")
        seen.add(key)
    return data


def cached_snapshot(query: str, raw_dir: Path, cancel) -> Path | None:
    """Reuse a complete, hash-verified response for the exact request, up to 24h.

    Existing map recipes still pin their original snapshot without an expiry.
    Receipts and raw responses are never edited during cache reuse.
    """
    now = datetime.now(timezone.utc)
    for receipt in sorted(raw_dir.glob('*.provenance.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        cancel.check()
        try:
            info = json.loads(receipt.read_text(encoding='utf-8'))
            if not isinstance(info, dict) or info.get('query') != query:
                continue
            stamp = datetime.fromisoformat(info['retrieved_utc'])
            if stamp.tzinfo is None or not timedelta(0) <= now-stamp <= CACHE_MAX_AGE:
                continue
            digest = info['sha256']
            if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue  # A broken receipt is not authority to reuse any content.
        snapshot = raw_dir/(digest+'.json')
        if not snapshot.is_file() or sha256(snapshot) != digest:
            raise ValueError('Stored original OSM data changed or is missing')
        validate_snapshot(snapshot.read_bytes())
        log.info('Reusing verified OSM snapshot %s from %s', digest, stamp.isoformat())
        return snapshot
    return None


def acquire(spec: dict, raw_dir: Path, get, cancel, progress) -> Path:
    """get(url) uses Creator's cancellable HTTP client; no separate credentials."""
    query = query_for(spec)
    cached = cached_snapshot(query, raw_dir, cancel)
    if cached is not None:
        progress(1, 'Using verified cached OSM data...')
        return cached
    errors = []
    certificate_failures = []
    for index, endpoint in enumerate(ENDPOINTS):
        cancel.check()
        progress(index/len(ENDPOINTS), f"Downloading real OSM lifts and trails (server {index+1}/{len(ENDPOINTS)})...")
        try:
            url = endpoint + "?" + urlencode({"data": query})
            raw = get(url)
            document = validate_snapshot(raw)
        except Exception as exc:
            cancel.check()  # Never swallow the Creator's cancellation exception.
            log.warning("OSM server %s failed: %s", endpoint, exc)
            errors.append(f"{endpoint}: {exc}")
            if getattr(exc, 'category', None) == 'tls':
                certificate_failures.append(endpoint)
            continue
        digest = hashlib.sha256(raw).hexdigest()
        raw_dir.mkdir(parents=True, exist_ok=True)
        snapshot = raw_dir/(digest+".json")
        if snapshot.exists():
            if sha256(snapshot) != digest:
                raise ValueError("Stored original OSM data changed")
        else:
            with snapshot.open("xb") as stream:
                stream.write(raw)
        # Each acquisition has a separate receipt; raw content is never edited.
        import uuid
        write_json(raw_dir/(digest+"-"+uuid.uuid4().hex+".provenance.json"), {
            "endpoint": endpoint, "query": query, "url": url, "sha256": digest,
            "retrieved_utc": datetime.now(timezone.utc).isoformat(),
            "osm_base": document.get("osm3s"),
            "attribution": "© OpenStreetMap contributors", "license": "ODbL-1.0",
            "license_url": "https://www.openstreetmap.org/copyright"})
        return snapshot
    if certificate_failures:
        raise ValueError('OSM download failed: HTTPS certificate verification failed for '+
                         ', '.join(certificate_failures)+'. See the log for certificate details; '
                         'the remaining servers could not complete the download.') from RuntimeError('; '.join(errors))
    raise ValueError("OpenStreetMap servers could not complete the download (unavailable, busy, or timed out). Try again later; downloaded terrain is cached.") from RuntimeError("; ".join(errors))


def _deviation(points):
    a, b = points[0], points[-1]
    dx, dy = b[0]-a[0], b[1]-a[1]
    norm = dx*dx+dy*dy
    if norm < 4:
        return math.inf
    result = 0.0
    for x, y in points:
        t = max(0, min(1, ((x-a[0])*dx+(y-a[1])*dy)/norm))
        result = max(result, math.hypot(x-a[0]-t*dx, y-a[1]-t*dy))
    return result


def compile_snapshot(spec: dict, package: str, snapshot: Path, sample_height) -> dict:
    """One trail per original way; relation tags inherited without bridging gaps."""
    from .catalog import CATALOG, PROVENANCE
    from .scene import compile_scene
    data = validate_snapshot(snapshot.read_bytes())
    frame = NativeMapFrame(package, TransverseMercator(spec["lon"], spec["lat"]),
                           (spec["size_m"], spec["size_m"]))
    nodes = {v["id"]: v for v in data["elements"] if v["type"] == "node"}
    ways = {v["id"]: v for v in data["elements"] if v["type"] == "way"}
    relations = [v for v in data["elements"] if v["type"] == "relation"]
    catalog = {v["id"]: v["class"] for v in CATALOG if v["kind"] == "lift"}
    inherited = {}
    omitted = []
    for relation in relations:
        tags = relation.get("tags", {})
        if not spec.get("osm_pistes") or not (tags.get("piste:type") == "downhill" or tags.get("route") == "piste"):
            continue
        for member in relation.get("members", []):
            if member["type"] == "way":
                if member["ref"] not in ways:
                    raise ValueError(f"Incomplete OSM relation {relation['id']}: missing way {member['ref']}")
                inherited.setdefault(member["ref"], []).append((relation["id"], tags))
            elif member["type"] == "relation":
                omitted.append({"kind": "relation", "osm_ids": [relation["id"]],
                                "reason": "Nested piste relation not flattened; tagged ways still processed"})
    lifts, pistes, sources, places = [], [], [], []
    for identifier, node in sorted(nodes.items()):
        tags = node.get("tags", {})
        name, basis = osm_name(tags)
        if not name or not (tags.get("aerialway") == "station" or tags.get("place")
                            or tags.get("natural") == "peak" or tags.get("mountain_pass") == "yes"):
            continue
        point = frame.local(node["lon"], node["lat"])
        if frame.contains(point):
            places.append({"id": f"osm-node-{identifier}", "name": name, "name_basis": basis,
                           "kind": "station" if tags.get("aerialway") == "station" else "place",
                           "point_m": point, "position_cm": frame.world(point), "source_tags": tags})

    def route(way):
        ids = way.get("nodes", [])
        if len(ids) < 2 or any(i not in nodes for i in ids):
            raise ValueError(f"Incomplete OSM way {way['id']}; missing original nodes")
        coordinates = []
        for identifier in ids:
            node = nodes[identifier]
            lon, lat = node.get("lon"), node.get("lat")
            if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
                raise ValueError(f"Invalid OSM node {identifier}")
            coordinates.append(frame.local(lon, lat))
        if any(math.dist(a, b) < 1e-6 for a, b in zip(coordinates, coordinates[1:])):
            raise ValueError(f"OSM way {way['id']} contains consecutive duplicate coordinates")
        return coordinates

    def elevation(point):
        value = float(sample_height(*point))
        if not math.isfinite(value) or value == -9999:
            raise ValueError("Invalid terrain height beneath OSM geometry; no NoData filling")
        return value

    for identifier, way in sorted(ways.items()):
        tags = dict(way.get("tags", {}))
        parents = inherited.get(identifier, [])
        for key in ("piste:type", "piste:difficulty", "piste:width", "name", "official_name", "name:en", "ref"):
            values = {p[key] for _, p in parents if key in p}
            if key not in tags and len(values) == 1:
                tags[key] = values.pop()
        kind = ("lift" if spec.get("osm_lifts") and "aerialway" in tags
                else "piste" if spec.get("osm_pistes") and ("piste:type" in tags or parents) else None)
        if not kind:
            continue
        points = route(way)
        # Ignore envelope-only objects; report routes intersecting the window
        # but extending beyond it instead of clipping or moving them.
        if not all(frame.contains(p) for p in points):
            omitted.append({"kind": kind, "osm_ids": [identifier], "name": tags.get("name", ""),
                            "reason": "Route not wholly inside map; no clipping"})
            continue
        full_name, name_basis = osm_name(tags, f"OSM {identifier}")
        name = full_name[:256]  # Official scene contract; full source name is retained.
        record = {"id": f"osm-way-{identifier}", "name": name, "full_osm_name": full_name,
                  "name_basis": name_basis, "osm_way_ids": [identifier],
                  "osm_relation_ids": [i for i, _ in parents], "source_tags": tags}

        def omit(reason):
            omitted.append({"kind": kind, "osm_ids": [identifier], "name": name, "reason": reason})

        if kind == "lift":
            lift_type = tags["aerialway"]
            assumptions = []
            model = {"gondola": "gondola", "cable_car": "aerial-tram"}.get(lift_type)
            if lift_type == "chair_lift":
                seats = tags.get("aerialway:occupancy")
                detachable = tags.get("aerialway:detachable")
                model = ({"4": "high-speed-quad", "6": "high-speed-six"}.get(seats)
                         if detachable == "yes" else {"1": "fixed-chair1", "2": "fixed-chair2", "4": "fixed-chair4"}.get(seats))
                if not model:
                    model = "fixed-chair4"
                    assumptions.append("Generic four-seat chair model; source occupancy/detachability not reproduced")
                if detachable not in ("yes", "no"):
                    assumptions.append("Unknown detachability; fixed chair modelling default")
            if not model:
                omit("Unsupported native lift type; Surface lifts remain separate and unverified on native levels")
                continue
            error = _deviation(points)
            if error > 20:
                omit("Native two-terminal lift cannot preserve original route within 20 metres")
                continue
            a, b = points[0], points[-1]
            z0, z1 = elevation(a), elevation(b)
            if abs(z0-z1) < 1:
                omit("Lift terminal order ambiguous: less than one metre elevation difference")
                continue
            if z0 > z1:
                a, b = b, a
            if model not in catalog:
                raise ValueError(f"Official lift catalog lacks {model}")
            sources.append({"id": record["id"], "name": name, "kind": "lift", "class": catalog[model],
                            "position": list(a), "end_position": list(b)})
            lifts.append(dict(record, type=lift_type, route_m=points, terminals_m=[a, b],
                              maximum_chord_deviation_m=error, native_catalog_id=model,
                              assumptions=assumptions+["Native generic station/carrier/support models and default operating parameters"]))
        else:
            if tags.get("piste:type") != "downhill" or tags.get("area") == "yes" or way["nodes"][0] == way["nodes"][-1]:
                omit("Not an open downhill centreline; area geometry is not converted to an invented axis")
                continue
            difficulty = RATINGS.get(tags.get("piste:difficulty"))
            if difficulty is None:
                omit("Missing, conflicting or unsupported OSM piste difficulty")
                continue
            width = tags.get("piste:width", tags.get("width"))
            basis = "OSM width tag"
            if width is None:
                width, basis = 30.0, "Provisional modelling default: 30 metres; not measured"
            else:
                match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:m)?\s*", str(width))
                if not match or not 2 <= float(match[1]) <= 100:
                    omit("Invalid or unsupported OSM trail width")
                    continue
                width = float(match[1])
            z = [elevation(p) for p in points]
            if abs(z[0]-z[-1]) < 1:
                omit("Downhill direction ambiguous: less than one metre elevation difference")
                continue
            if z[0] < z[-1]:
                points.reverse()
            native = {"name": name, "points": [frame.world(p) for p in points],
                      "width": width*100, "difficulty": difficulty}
            pistes.append(dict(record, points_m=points, width_m=width, width_basis=basis,
                               difficulty=difficulty, extensions={"skieo": native}))
    # This calls the installed official GUID/hub compiler, not a replica.
    manifest = {"id": "real-resort.creator-"+hashlib.sha256(package.encode()).hexdigest()[:16],
                "terrain": {"source": {"vertices": [2017, 2017], "spacing_meters": spec["size_m"]/2016},
                            "origin_centimeters": [-spec["size_m"]*50, -spec["size_m"]*50, 0]}}
    compiled = compile_scene({"version": 1, "objects": sources}, manifest)
    for lift, definition in zip(lifts, compiled):
        lift["extensions"] = {"skieo": definition}
    if not lifts and not pistes:
        raise ValueError("No supported OSM lifts or downhill trails found inside this area. Try a larger area or disable OSM.")
    return {"format": "real-resort-scene", "version": 1, "map": frame.descriptor(),
            "lifts": lifts, "pistes": pistes, "places": places, "omitted": omitted,
            "provenance": {"osm_sha256": sha256(snapshot), "osm_snapshot": str(snapshot),
                           "attribution": "© OpenStreetMap contributors, ODbL-1.0",
                           "geometry": "Original OSM way nodes; no gap filling; one trail per way",
                           "vertical": "DEM used only to order endpoints; runtime native ground query; no datum conversion",
                           "defaults": "Missing widths use 30 m; unsupported chair capacities use a generic four-seat model",
                           "compiler_sha256": PROVENANCE['compiler_upstream_sha256'],
                           "compiler_adapter_sha256": PROVENANCE['compiler_adapter_sha256']},
            "game_integration": "unverified", "performance": "unmeasured"}
