"""Build/validate a small geometry initializer; only this mod folder is installed.

Actors/trails are already in the native starting save. The helper never imports
OSM, detects New Game/load requests, or reads/writes personal save files.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import zipfile

from .. import paths
from .io import read_json, write_json, sha256
from .native_generation import verify


def identity(slug):
    if not re.fullmatch('[a-z0-9_]{14}', slug):
        raise ValueError('Invalid Creator map ID')
    return 'real-resort.creator-supports-'+slug, 'SkiEO-CreatorSupports-'+slug


def mods_path(game):
    win = Path(game)/'SkiResortSimulator/Binaries/Win64'
    for base in (win/'ue4ss', win):
        if (base/'UE4SS.dll').is_file():
            return base/'Mods'
    return win/'ue4ss/Mods'


def sdk_root():
    import custom_map
    if importlib.metadata.version('skieo-custom-map') != '1.29.0':
        raise ValueError('Support initializer requires the pinned official SkiEO Mod 1.29.0 tools')
    candidates = [paths.resource('share', 'skieomod')]
    candidates += [p/'share/skieomod' for p in Path(custom_map.__file__).resolve().parents]
    for p in candidates:
        if (p/'SkiEO-CustomMaps/Scripts/compatibility.lua').is_file():
            return p
    raise ValueError('Official support initializer resources are missing')


def build(resort, output, slug, game):
    import custom_map as official
    mod_id, directory = identity(slug)
    scripts = sdk_root()/'SkiEO-CustomMaps/Scripts'
    config = dict(resort.report['support_preparation'])
    config['game_manifest_relative'] = os.path.relpath(Path(game).parents[1]/'appmanifest_5012760.acf', mods_path(game)/directory).replace('\\', '/')
    config['game_build'] = resort.report['native_generation_contract']['game_build']
    output = Path(output); reports = output/'Reports';reports.mkdir(exist_ok=True)
    archive = reports/'native-supports.skieomod'
    with tempfile.TemporaryDirectory(prefix='support-build-', dir=output) as tmp:
        stage = Path(tmp);(stage/'Scripts').mkdir()
        for name in ('main.lua', 'support_init.lua'):
            shutil.copyfile(paths.resource('data', 'osm', 'runtime', name), stage/'Scripts'/name)
        for name in ('compatibility.lua', 'json.lua'):
            shutil.copyfile(scripts/name, stage/'Scripts'/name)
        write_json(stage/'Scripts/config.json', config)
        shutil.copyfile(paths.resource('data','osm','LICENSE.SkiEO-Mod.txt'), stage/'LICENSE.SkiEO-Mod.txt')
        shutil.copyfile(paths.resource('data','osm','runtime','LICENSE.txt'), stage/'LICENSE.txt')
        (stage/'ATTRIBUTION.txt').write_text('OSM data © OpenStreetMap contributors, ODbL 1.0. Native support initialization uses public native functions; compatibility/json SDK: SkiEO Mod 1.29.0, BSD-3-Clause.\n',encoding='utf-8')
        files=sorted(p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file())
        manifest={'format':{'name':'skieo-mod','version':'1.0.0'},'id':mod_id,
                  'metadata':{'name':'Creator native support preparation','version':'0.1.0','authors':['SkiEO Map Creator contributors'],
                              'description':'Prepare already baked, explicitly marked lift geometry after native terrain becomes ready. No actor import or save-file edits.',
                              'language':'en','license':'MIT code; BSD-3-Clause SDK; ODbL data'},
                  'runtime':'ue4ss','dependencies':[],'install_directory':directory,'files':files}
        write_json(stage/'custom-mod.json',manifest)
        official.projects.build(stage, archive)
        for label, target in (('project',stage),('archive',archive)):
            # Same installed official validators as the CLI, without launching or installing.
            kind, checked, _ = official.inspect_package(official.prepare(target, tmp))
            if kind!='mod' or checked['id']!=mod_id:
                raise ValueError('Unexpected support package validation result')
    return {'archive':'Reports/native-supports.skieomod','sha256':sha256(archive),'id':mod_id,'directory':directory}


def install(output, report, game, slug, game_build):
    import custom_map as official
    from .. import install as native_install
    if native_install.game_running():
        raise ValueError('Close the game before installing support preparation')
    verify(game_build, game)
    root=Path(output).resolve();archive=(root/report['archive']).resolve()
    if not archive.is_relative_to(root) or sha256(archive)!=report['sha256']:
        raise ValueError('Support initializer package changed; rebuild required')
    kind, manifest, _=official.inspect_package(archive)
    expected_id, directory=identity(slug)
    if kind!='mod' or manifest['id']!=expected_id or manifest['runtime']!='ue4ss' or manifest['dependencies'] or manifest['install_directory']!=directory:
        raise ValueError('Unexpected support initializer identity')
    mods=mods_path(game)
    if not (mods.parent/'UE4SS.dll').is_file():
        parser,_=official.create_parser();args=parser.parse_args(['run',str(archive),'--game-dir',str(game)])
        config=official.desktop.setup(args,protocol=False)
        mods=Path(config['ue4ss_mods'])
    mods.mkdir(parents=True,exist_ok=True)
    destination=mods/directory
    stage=mods/('.creator-supports-'+uuid.uuid4().hex);stage.mkdir()
    backup=None
    try:
        with zipfile.ZipFile(archive) as z:
            for name in ['custom-mod.json',*manifest['files']]:
                target=(stage/name).resolve()
                if not target.is_relative_to(stage.resolve()):raise ValueError('Unsafe support package path')
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(name))
        (stage/'enabled.txt').touch()
        if destination.exists():
            if destination.is_symlink() or read_json(destination/'custom-mod.json').get('id')!=expected_id:
                raise ValueError('Existing support folder belongs to another mod')
            backup=paths.APP_DIR/'support-helper-backups'/uuid.uuid4().hex;backup.parent.mkdir(parents=True,exist_ok=True)
            shutil.move(str(destination),str(backup))
        # Match the official installer's handling of an explicitly disabled own mod.
        # Back up mods.txt and change only this generated helper's enable value.
        mods_file=mods/'mods.txt'
        if mods_file.is_file():
            original=mods_file.read_text(encoding='utf-8-sig')
            updated=re.sub(r'(?m)^([ \t]*'+re.escape(directory)+r'[ \t]*:[ \t]*)0([ \t]*\r?)$',r'\g<1>1\2',original)
            if updated!=original:
                saved=paths.APP_DIR/'support-helper-backups'/('mods-'+uuid.uuid4().hex+'.txt')
                saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(mods_file,saved)
                mods_file.write_text(updated,encoding='utf-8')
        try:os.replace(stage,destination)
        except OSError:
            if backup and not destination.exists():shutil.move(str(backup),str(destination))
            raise
    finally:shutil.rmtree(stage,ignore_errors=True)


def uninstall(game,slug):
    mod_id,directory=identity(slug);folder=mods_path(game)/directory
    if not folder.exists():return
    if folder.is_symlink() or read_json(folder/'custom-mod.json').get('id')!=mod_id:
        raise ValueError('Support folder identity differs; not removed')
    backup=paths.APP_DIR/'support-helper-backups'/uuid.uuid4().hex;backup.parent.mkdir(parents=True,exist_ok=True)
    shutil.move(str(folder),str(backup))
