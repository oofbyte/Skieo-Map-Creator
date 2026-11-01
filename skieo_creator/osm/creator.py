"""Two-checkbox OSM workflow: native map build and provenance, no runtime import."""
from pathlib import Path
import html
from .io import read_json, write_json, sha256


def coverage_html(scene: dict) -> str:
    rows = []
    for kind in ("lifts", "pistes"):
        for item in scene[kind]:
            notes = item.get("assumptions", [])
            if item.get("width_basis"):
                notes = [*notes, item["width_basis"]]
            if item.get("full_osm_name") and item["full_osm_name"] != item["name"]:
                notes = [*notes, "Source name shortened to the native 256-character object-name limit: "+item["full_osm_name"]]
            rows.append((kind, item["name"], "Included", "; ".join(notes)))
    for item in scene.get("omitted", []):
        rows.append((item["kind"], item.get("name") or str(item.get("osm_ids", "")), "Excluded", item["reason"]))
    for item in scene.get("places", []):
        rows.append((item["kind"], item["name"], "Geographic name anchor",
                     "Names nearby native hubs only (station: 120 m; place/peak: 150 m). Native hub labels are limited to 32 characters; this is not a separately placed landmark."))
    table = ''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>OSM coverage report</title>'
            '<style>body{font:16px system-ui;margin:2rem;max-width:1100px}table{border-collapse:collapse}'
            'td,th{padding:.6rem;border:1px solid #ccc;text-align:left}</style><h1>OSM coverage report</h1>'
            f'<p>Included: {len(scene["lifts"])} lifts and {len(scene["pistes"])} trail segments. '
            'OSM coverage can be incomplete. Generic native models and provisional trail widths are listed below.</p>'
            '<p>© OpenStreetMap contributors · ODbL 1.0. No missing geometry has been invented. '
            'Game integration and performance of this generated map have not been verified.</p>'
            '<table><tr><th>Type</th><th>Name</th><th>Status</th><th>Notes</th></tr>'+table+'</table></html>')


def options(params: dict) -> dict:
    """The user supplies only two booleans; data/tool paths are internal."""
    flags = {key: params.get(key, False) for key in ("osm_lifts", "osm_pistes")}
    if any(type(value) is not bool for value in flags.values()):
        raise ValueError("OSM switches must be booleans")
    return flags


def prepare_automatic(spec, package, cache, sample_height, get, cancel, progress):
    from .automatic_osm import acquire, compile_snapshot, query_for
    flags = options(spec)
    if not any(flags.values()):
        return None
    query = query_for(spec)
    source = spec.get("osm_source")
    if source and source.get("query") == query:
        snapshot = Path(source["path"])
        if not snapshot.resolve().is_relative_to((cache/"raw").resolve()) or sha256(snapshot) != source["sha256"]:
            raise ValueError("Original OSM snapshot changed or is missing; rebuild refused")
    else:
        snapshot = acquire(spec, cache/"raw", get, cancel, progress)
        spec["osm_source"] = {"path": str(snapshot), "sha256": sha256(snapshot), "query": query}
    cancel.check()
    progress(0.9, "Preparing real OSM lifts and trails...")
    scene = compile_snapshot(spec, package, snapshot, sample_height)
    processed = cache/"processed"
    processed.mkdir(parents=True, exist_ok=True)
    import uuid
    write_json(processed/(uuid.uuid4().hex+".json"), scene)
    return scene



def native_report(resort, output: Path) -> dict:
    """The native .skimap contains the objects; these files are provenance only."""
    reports = output/'Reports'
    reports.mkdir(exist_ok=True)
    resort.report['packed_files_sha256'] = {p.name: sha256(p) for p in output.iterdir() if p.suffix in ('.utoc', '.ucas', '.pak')}
    write_json(reports/'native-osm.json', resort.report)
    write_json(reports/'resort.json', resort.scene)
    (reports/'coverage.html').write_text(coverage_html(resort.scene), encoding='utf-8')
    (reports/'ATTRIBUTION.txt').write_text('Geographic data © OpenStreetMap contributors, ODbL 1.0. https://www.openstreetmap.org/copyright\nSee resort.json for immutable source hashes, omissions and modelling assumptions. Native terrain preserves the original Creator processing.\n', encoding='utf-8')
    return {**resort.report, 'omitted': len(resort.scene.get('omitted', [])),
            'coverage_report': 'Reports/coverage.html', 'directory': ''}


def retire_legacy_overlay(game: Path, output: Path) -> None:
    """Back up only this project's previous importer, outside UE4SS Mods."""
    import shutil
    import uuid
    mods = Path(game)/'SkiResortSimulator/Binaries/Win64/ue4ss/Mods'
    old = mods/'SkiEO-real-resort-campiglio-native-osm'
    if not old.exists():
        return
    if old.is_symlink() or not old.is_dir():
        raise ValueError('Legacy OSM mod has an unexpected path; migration refused')
    manifest = read_json(old/'custom-mod.json')
    if manifest.get('id') != 'real-resort.campiglio-native-osm' or manifest.get('runtime') != 'ue4ss':
        raise ValueError('Legacy OSM mod identity differs; migration refused')
    backup = output/'legacy-overlay-backups'/uuid.uuid4().hex
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(backup))
    write_json(output/'legacy-overlay-migration.json', {'original': str(old), 'backup': str(backup),
               'reason': 'OSM objects are now baked into the native starting save; runtime importer retired'})
