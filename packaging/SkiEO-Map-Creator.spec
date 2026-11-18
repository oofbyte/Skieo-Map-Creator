# -*- mode: python ; coding: utf-8 -*-
# build: packaging\build.bat (Windows: one-file exe in dist\) or packaging/build_mac.sh (macOS: dist/SkiEO Map Creator.app)
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
MAC = sys.platform == 'darwin'
VERSION = re.search(r"VERSION = '([^']+)'", (ROOT / 'skieo_creator' / '__init__.py').read_text()).group(1)
RETOC = 'retoc' if MAC else 'retoc.exe'

a = Analysis(
    [str(ROOT / 'run.py')],
    pathex=[str(ROOT)],
    binaries=[(str(ROOT / 'tools' / RETOC), 'tools')],
    datas=[(str(ROOT / 'web'), 'web'), (str(ROOT / 'data'), 'data'), (str(ROOT / 'tools' / 'retoc-LICENSE'), 'tools'),
           (str(ROOT / 'packaging' / 'THIRD-PARTY.txt'), '.')],
    hiddenimports=['webview.platforms.cocoa'] if MAC else
                  ['webview.platforms.edgechromium', 'webview.platforms.winforms', 'clr_loader', 'pythonnet'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'test', 'scipy', 'pyproj', 'matplotlib', 'IPython', 'PyQt5', 'PyQt6',
              'PySide2', 'PySide6', 'gi', 'cefpython3'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if not MAC:
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
else:
    # an app bundle (a folder: macOS apps are not single files); ad-hoc signed by PyInstaller, so it runs on Apple
    # silicon; not notarized, so the first start needs System Settings > Privacy & Security > Open Anyway
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='SkiEO-Map-Creator',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
        target_arch='arm64',          # the game runs on Apple silicon only
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='SkiEO-Map-Creator')
    app = BUNDLE(
        coll,
        name='SkiEO Map Creator.app',
        icon=str(ROOT / 'packaging' / 'icon.icns'),
        bundle_identifier='io.github.oofbyte.skieo-map-creator',
        version=VERSION,
        info_plist={
            'CFBundleDisplayName': 'Ski-E-O Map Creator',
            'CFBundleShortVersionString': VERSION,
            'CFBundleVersion': VERSION,
            'LSMinimumSystemVersion': '13.0',
            'NSHighResolutionCapable': True,
            'NSHumanReadableCopyright': 'MIT License. Fan-made, not affiliated with the developers of Ski-E-O!.',
        },
    )
