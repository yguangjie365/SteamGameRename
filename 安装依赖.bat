@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYCMD="
where python >nul 2>&1 && set "PYCMD=python"
if not defined PYCMD (
  where py >nul 2>&1 && set "PYCMD=py -3"
)
if not defined PYCMD (
  echo [!] Python was not found in PATH.
  echo     Install Python 3.11+ from https://www.python.org/downloads/
  echo     and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

echo Installing dependencies: typer rich httpx pydantic
%PYCMD% -m pip install --upgrade --index-url https://pypi.org/simple typer rich httpx pydantic
if errorlevel 1 (
  echo.
  echo [!] Installation failed. Check your network / proxy and try again.
  pause
  exit /b 1
)

echo.
echo Done. Now double-click "SteamGameRename.bat" to start.
pause
