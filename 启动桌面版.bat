@echo off
rem 12306 desktop launcher (Electron shell + Python JSON-RPC backend).
rem Keep this file ASCII-only: cmd decodes it in the OEM code page (936/GBK here),
rem and UTF-8 Chinese bytes turn into garbage that corrupts batch parsing.
setlocal

rem Drop RUN_AS_NODE inherited from an Electron parent, or electron.exe runs as
rem node and crashes with "Cannot read properties of undefined (reading handle)".
set ELECTRON_RUN_AS_NODE=

rem Preflight (2026-10-25 review): the shell used to open with a dead
rem backend when python was missing or api.py was moved; fail fast here
rem with a readable message instead of a blank window.
where python >nul 2>nul
if errorlevel 1 (
  echo [error] python not found in PATH. Install Python 3, then retry.
  pause
  exit /b 1
)
if not exist "%~dp0api.py" (
  echo [error] api.py not found next to this launcher: %~dp0api.py
  pause
  exit /b 1
)

set "APP=%~dp0electron"
set "EXE=%APP%\node_modules\electron\dist\electron.exe"

if not exist "%EXE%" (
  echo [first run] installing Electron into electron\node_modules ...
  rem npmjs.org TLS often drops here; use the npmmirror binary mirror.
  set "ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/"
  pushd "%APP%"
  call npm install
  popd
)

if not exist "%EXE%" (
  echo [error] Electron still missing: %EXE%
  echo Run "npm install" inside the electron folder, then retry.
  pause
  exit /b 1
)

start "" "%EXE%" "%APP%"
