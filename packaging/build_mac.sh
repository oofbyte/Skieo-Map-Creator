#!/bin/sh
# Builds "dist/SkiEO Map Creator.app" and dist/SkiEO-Map-Creator-macOS.zip (Apple silicon). Run from anywhere.
set -e
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "Creating the build environment..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python packaging/make_icon.py
.venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/SkiEO-Map-Creator.spec
rm -rf "dist/SkiEO-Map-Creator"                      # the bundle's contents, already in the .app
# ditto keeps the bundle's symlinks, permissions and signature intact (zip would not)
rm -f dist/SkiEO-Map-Creator-macOS.zip
ditto -c -k --keepParent "dist/SkiEO Map Creator.app" dist/SkiEO-Map-Creator-macOS.zip
echo
echo "Built dist/SkiEO Map Creator.app and dist/SkiEO-Map-Creator-macOS.zip"
