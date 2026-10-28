@echo off
rem Builds dist\SkiEO-Map-Creator.exe (one file). Run from anywhere.
setlocal
set ROOT=%~dp0..
cd /d "%ROOT%"
if not exist .venv\Scripts\python.exe (
  echo Creating the build environment...
  py -3.12 -m venv .venv || exit /b 1
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
)
.venv\Scripts\python.exe packaging\make_icon.py || exit /b 1
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\SkiEO-Map-Creator.spec || exit /b 1
echo.
echo Built dist\SkiEO-Map-Creator.exe
