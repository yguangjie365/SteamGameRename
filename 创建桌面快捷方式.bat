@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\create-shortcut.ps1"
if errorlevel 1 (
  echo.
  echo [!] Failed to create the shortcut.
  pause
  exit /b 1
)
echo.
echo Done. A shortcut named "SteamGameRename" is now on your Desktop.
pause
