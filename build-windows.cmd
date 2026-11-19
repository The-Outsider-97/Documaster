@echo off
rem Build the SLAI Documaster desktop installer (Windows, NSIS, per-user).
rem
rem   build-windows.cmd                                    build everything
rem   build-windows.cmd --trust-first-download             first build only: accept the python.org download and record its SHA-256
rem   build-windows.cmd --helper qpdf=C:\tools\qpdf        extra arguments go to tools\build_bundle.py (repeatable)
rem   set SLAI_ROOT=D:\SLAI  before running, if this folder is not two levels inside the SLAI checkout (the builder reads it)
rem
rem Needs: Node.js 20+, Python 3.11+ (only to run the build scripts; the app ships its own Python), Rust (cargo), internet for the
rem first run (npm, crates, python.org, PyPI wheels - torch alone is several hundred MB). See docs\DESKTOP_APP.md.
setlocal EnableExtensions
cd /d "%~dp0"

where node  >nul 2>nul || (echo [x] Node.js is not installed or not on PATH. & exit /b 1)
where npm   >nul 2>nul || (echo [x] npm is not on PATH. & exit /b 1)
where cargo >nul 2>nul || (echo [x] Rust is not installed. Get it from https://rustup.rs & exit /b 1)
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (echo [x] Python 3 is not installed or not on PATH. & exit /b 1)

echo [1/5] Web client
call npm ci || (echo [x] npm ci failed. & exit /b 1)
call npm run build || (echo [x] The web client did not build. & exit /b 1)

echo [2/5] Checks before bundling (the builder's own tests; they need only the standard library)
%PY% -m unittest discover -s tests_py -p "test_bundle.py" || (echo [x] Bundle tests failed. & exit /b 1)

echo [3/5] Engine bundle: Python + packages + SLAI + model into src-tauri\engine
%PY% tools\build_bundle.py %* || (echo [x] The engine bundle did not build. & exit /b 1)

echo [4/5] Does the bundle start and answer? (its own Python, offline; this loads torch and the model: it can take minutes)
%PY% tools\smoke_engine.py --bundle src-tauri\engine --agents-test || (echo [x] The bundled engine failed its smoke test. Not building an installer. & exit /b 1)

echo [5/5] Installer
call npx --yes @tauri-apps/cli@2 build --bundles nsis || (echo [x] Tauri build failed. & exit /b 1)

echo.
echo Done. The installer is in src-tauri\target\release\bundle\nsis
if exist "src-tauri\target\release\bundle\nsis" start "" "src-tauri\target\release\bundle\nsis"
exit /b 0
