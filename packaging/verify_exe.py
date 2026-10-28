"""Check that a SkiEO-Map-Creator.exe was built from this source.

    py -3.12 -m pip install -r requirements.txt
    py -3.12 packaging\\verify_exe.py SkiEO-Map-Creator.exe

Opens the PyInstaller archive inside the exe and compares it with this folder:
- every bundled file of the app (web/, data/, tools/, THIRD-PARTY.txt) byte for byte,
- every app module (skieo_creator/*, run.py) as compiled code, against the .py here compiled by this Python.
Everything else in the exe is Python and the libraries in requirements.txt; they are listed at the end.
Needs Python 3.12 (the exe's Python), because compiled code differs between Python versions.
"""
import hashlib
import marshal
import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parent.parent
DATA_DIRS = ('web', 'data', 'tools')


def source_path(name):
    if name == 'THIRD-PARTY.txt':
        return ROOT / 'packaging' / 'THIRD-PARTY.txt'
    return ROOT / name


def compiled(path):
    rel = path.relative_to(ROOT).as_posix()
    return compile(path.read_bytes(), rel, 'exec', dont_inherit=True, optimize=0)


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    exe = Path(sys.argv[1])
    print('%s\nSHA-256 %s\n' % (exe.name, hashlib.sha256(exe.read_bytes()).hexdigest()))
    car = CArchiveReader(str(exe))
    toc = {name.replace('\\', '/'): (name, entry[-1]) for name, entry in car.toc.items()}
    if 'python312.dll' not in toc:
        raise SystemExit('this exe does not bundle Python 3.12; not a Ski-E-O Map Creator build?')
    if sys.version_info[:2] != (3, 12):
        raise SystemExit('run this with Python 3.12 (you have %d.%d)' % sys.version_info[:2])
    bad = []

    # bundled files
    bundled = {n for n in toc if n.split('/')[0] in DATA_DIRS or n == 'THIRD-PARTY.txt'}
    for n in sorted(bundled):
        p = source_path(n)
        if not p.is_file():
            bad.append('in the exe but not in the source: ' + n)
        elif car.extract(toc[n][0]) != p.read_bytes():
            bad.append('different from the source: ' + n)
    here = {p.relative_to(ROOT).as_posix() for d in DATA_DIRS for p in (ROOT / d).rglob('*') if p.is_file()}
    for n in sorted(here - bundled):
        bad.append('in the source but not in the exe: ' + n)
    print('bundled files checked: %d' % len(bundled))

    # entry script and app modules
    scripts = [n for n, (_, t) in toc.items() if t == 's']
    if 'run' not in scripts or marshal.loads(car.extract(toc['run'][0])) != compiled(ROOT / 'run.py'):
        bad.append('different from the source: run.py')
    pyz = car.open_embedded_archive([n for n, (_, t) in toc.items() if t == 'z'][0])
    mods = sorted(m for m in pyz.toc if m.split('.')[0] == 'skieo_creator')
    for m in mods:
        is_pkg = pyz.toc[m][0] == 1
        p = ROOT.joinpath(*m.split('.'), '__init__.py') if is_pkg else ROOT.joinpath(*m.split('.')).with_suffix('.py')
        if not p.is_file():
            bad.append('module in the exe but not in the source: ' + m)
        elif pyz.extract(m) != compiled(p):
            bad.append('module different from the source: ' + m)
    src_mods = sorted((ROOT / 'skieo_creator').rglob('*.py'))
    if len(src_mods) != len(mods):
        bad.append('the source has %d app modules, the exe %d' % (len(src_mods), len(mods)))
    print('app modules checked: %d, plus run.py' % len(mods))

    libs = sorted({m.split('.')[0] for m in pyz.toc} - {'skieo_creator'})
    print('\nother Python modules in the exe (standard library + requirements.txt and their dependencies):')
    print('  ' + ', '.join(libs))
    print('native files in the exe:')
    print('  ' + ', '.join(sorted({n.split('/')[0] for n, (_, t) in toc.items() if t in ('b',)} - set(DATA_DIRS))))

    print()
    if bad:
        print('\n'.join('  ' + b for b in bad))
        raise SystemExit('FAILED: %d differences' % len(bad))
    print('OK: the exe matches this source.')


if __name__ == '__main__':
    main()
