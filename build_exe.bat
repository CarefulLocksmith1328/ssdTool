@echo off
setlocal
cd /d "%~dp0"
set "PY=py -3"
where py >nul 2>nul || set "PY=python"

echo Installiere PyInstaller ...
%PY% -m pip install --upgrade pyinstaller || goto :error

echo Baue SSD-Datenretter.exe ...
%PY% -m PyInstaller --noconfirm --clean --onefile --console --uac-admin ^
  --name SSD-Datenretter ^
  --add-data "ssdtool\web;ssdtool\web" ^
  run.py || goto :error

echo.
echo Fertig: dist\SSD-Datenretter.exe
pause
exit /b 0

:error
echo Build fehlgeschlagen.
pause
exit /b 1
