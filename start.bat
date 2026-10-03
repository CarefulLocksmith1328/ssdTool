@echo off
setlocal
cd /d "%~dp0"

rem Python suchen (py-Launcher bevorzugt)
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.10 oder neuer wird benoetigt: https://www.python.org/downloads/
  pause
  exit /b 1
)

rem Startet die Oberflaeche im Browser. Fragt automatisch nach Administratorrechten.
%PY% -m ssdtool %*
if errorlevel 1 pause
