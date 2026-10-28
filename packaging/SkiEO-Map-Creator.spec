# -*- mode: python ; coding: utf-8 -*-
# build: packaging\build.bat   (one-file exe in dist\)
from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / 'run.py')],
    pathex=[str(ROOT)],
    binaries=[(str(ROOT / 'tools' / 'retoc.exe'), 'tools')],
    datas=[(str(ROOT / 'web'), 'web'), (str(ROOT / 'data'), 'data'), (str(ROOT / 'tools' / 'retoc-LICENSE'), 'tools'),
           (str(ROOT / 'packaging' / 'THIRD-PARTY.txt'), '.')],
    hiddenimports=['webview.platforms.edgechromium', 'webview.platforms.winforms', 'clr_loader', 'pythonnet'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'test', 'scipy', 'pyproj', 'matplotlib', 'IPython', 'PyQt5', 'PyQt6',
              'PySide2', 'PySide6', 'gi', 'cefpython3'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SkiEO-Map-Creator',
    icon=str(ROOT / 'packaging' / 'icon.ico'),
    version=str(ROOT / 'packaging' / 'version_info.txt'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
